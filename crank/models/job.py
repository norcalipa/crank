# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Normalized external job-source policy and listing storage (issue #317)."""

from __future__ import annotations

from django.core.exceptions import ValidationError
from django.db import IntegrityError, models, transaction
from django.db.models import Q
from django.utils.translation import gettext_lazy as _
from django_extensions.db.models import TimeStampedModel

from crank.agents.jobs.base import (
    APPROVED_JOB_SOURCE_DOMAINS,
    MAX_CURRENCY,
    MAX_DESCRIPTION_EXCERPT,
    MAX_EMPLOYER_DOMAIN,
    MAX_EMPLOYER_NAME,
    MAX_EXTERNAL_ID,
    MAX_INTERVAL,
    MAX_LOCATION,
    MAX_TITLE,
    RawJobListing,
    validate_catalog_metadata,
    validate_job_url,
)


class JobSourceCatalog(TimeStampedModel):
    """Operator-controlled policy for a cataloged external job source.

    Adapter classes are code-registered; database fields are policy and
    provenance only. In particular, this model never stores credentials or an
    import path and cannot expand the code-owned URL allowlist.
    """

    class ApprovalState(models.TextChoices):
        PENDING = "pending", _("Pending")
        APPROVED = "approved", _("Approved")
        BLOCKED = "blocked", _("Blocked")

    name = models.CharField(max_length=100, unique=True)
    adapter_key = models.CharField(max_length=64, db_index=True)
    base_url = models.URLField(max_length=1024)
    approval_state = models.CharField(
        max_length=16,
        choices=ApprovalState.choices,
        default=ApprovalState.PENDING,
        db_index=True,
    )
    enabled = models.BooleanField(default=False, db_index=True)
    catalog_metadata = models.JSONField(
        default=dict,
        blank=True,
        validators=[validate_catalog_metadata],
    )

    class Meta:
        app_label = "crank"
        verbose_name = _("job source catalog")
        verbose_name_plural = _("job source catalog")
        indexes = [
            models.Index(fields=["adapter_key"], name="crank_jobsrc_adptkey_idx"),
            models.Index(
                fields=["approval_state", "enabled"],
                name="crank_jobsrc_policy_idx",
            ),
        ]

    def __str__(self):
        return self.name

    def clean(self):
        super().clean()
        if self.base_url:
            try:
                validate_job_url(self.base_url)
            except Exception as exc:
                raise ValidationError({"base_url": str(exc)}) from exc
        try:
            # Sanitize at the model boundary so direct saves and admin writes
            # cannot persist credentials or raw response bodies.
            self.catalog_metadata = validate_catalog_metadata(self.catalog_metadata)
        except Exception as exc:
            raise ValidationError({"catalog_metadata": str(exc)}) from exc

    def save(self, *args, **kwargs):
        # Django does not call full_clean() from save(); enforce the catalog
        # metadata contract for direct model saves as well as admin writes.
        self.full_clean()
        return super().save(*args, **kwargs)

    def allowed_hosts(self):
        """Return only code-owned hosts, never an operator-supplied allowlist."""
        return APPROVED_JOB_SOURCE_DOMAINS


class ActiveJobListingManager(models.Manager):
    """Default manager intentionally omits closed and expired listings."""

    def get_queryset(self):
        return super().get_queryset().filter(status=JobListing.Status.ACTIVE)


class JobListingQuerySet(models.QuerySet):
    def active(self):
        return self.filter(status=JobListing.Status.ACTIVE)

    def upsert_from_raw(self, source, raw: RawJobListing):
        """Create/update a listing safely under concurrent ingestion.

        Freshness is monotonic. Terminal states are never resurrected by an
        active observation; only an explicit terminal observation can change
        an active listing's status.
        """
        observed_at = raw.last_seen_at or raw.first_seen_at
        lookup = {"source": source, "external_id": raw.external_id}

        def find_existing():
            listing = None
            if raw.external_id:
                listing = self.model.all_objects.filter(**lookup).first()
            if listing is None:
                listing = self.model.all_objects.filter(
                    source=source, canonical_url=raw.canonical_url
                ).first()
            return listing

        with transaction.atomic():
            listing = find_existing()
            if listing is None:
                try:
                    # The savepoint lets us reconcile a concurrent insert while
                    # keeping the surrounding ingestion transaction usable.
                    with transaction.atomic():
                        return self.model.all_objects.create(
                            source=source,
                            external_id=raw.external_id,
                            first_seen_at=raw.first_seen_at or observed_at,
                            canonical_url=raw.canonical_url,
                            employer_name=raw.employer_name,
                            employer_domain=raw.employer_domain,
                            title=raw.title,
                            location_text=raw.location_text,
                            is_remote=raw.is_remote,
                            compensation_min=raw.compensation_min,
                            compensation_max=raw.compensation_max,
                            compensation_currency=raw.compensation_currency,
                            compensation_interval=raw.compensation_interval,
                            description_excerpt=raw.description_excerpt,
                            last_seen_at=observed_at,
                            status=raw.status,
                            source_metadata=dict(raw.source_metadata or {}),
                        )
                except IntegrityError:
                    # Another worker won the unique-key race. It is now safe
                    # to read and reconcile the committed row.
                    listing = find_existing()
                    if listing is None:
                        raise

            incoming_is_newer = observed_at >= listing.last_seen_at
            if listing.status in {
                self.model.Status.CLOSED,
                self.model.Status.EXPIRED,
            } and raw.status == self.model.Status.ACTIVE:
                # Terminal states are explicit and ingestion must never
                # resurrect them, even if a source reports a newer active row.
                status = listing.status
            elif incoming_is_newer:
                status = raw.status
            else:
                status = listing.status

            values = {
                "canonical_url": raw.canonical_url,
                "employer_name": raw.employer_name,
                "employer_domain": raw.employer_domain,
                "title": raw.title,
                "location_text": raw.location_text,
                "is_remote": raw.is_remote,
                "compensation_min": raw.compensation_min,
                "compensation_max": raw.compensation_max,
                "compensation_currency": raw.compensation_currency,
                "compensation_interval": raw.compensation_interval,
                "description_excerpt": raw.description_excerpt,
                "last_seen_at": max(listing.last_seen_at, observed_at),
                "status": status,
                "source_metadata": dict(raw.source_metadata or {}),
            }
            # first_seen_at is immutable provenance. A canonical-URL fallback
            # may fill a previously unavailable source ID.
            for field, value in values.items():
                setattr(listing, field, value)
            update_fields = [*values, "modified"]
            if not listing.external_id and raw.external_id:
                listing.external_id = raw.external_id
                update_fields.append("external_id")
            listing.save(update_fields=update_fields)
            return listing



class JobListing(TimeStampedModel):
    """A normalized, source-owned job posting with mutable freshness state."""

    class Status(models.TextChoices):
        ACTIVE = "active", _("Active")
        CLOSED = "closed", _("Closed")
        EXPIRED = "expired", _("Expired")

    source = models.ForeignKey(
        JobSourceCatalog,
        on_delete=models.RESTRICT,
        related_name="listings",
    )
    external_id = models.CharField(max_length=MAX_EXTERNAL_ID, blank=True, default="")
    canonical_url = models.URLField(max_length=1024)
    employer_name = models.CharField(max_length=MAX_EMPLOYER_NAME)
    employer_domain = models.CharField(
        max_length=MAX_EMPLOYER_DOMAIN, blank=True, default=""
    )
    title = models.CharField(max_length=MAX_TITLE)
    location_text = models.CharField(max_length=MAX_LOCATION, blank=True, default="")
    is_remote = models.BooleanField(default=False)
    compensation_min = models.DecimalField(
        max_digits=14, decimal_places=2, null=True, blank=True
    )
    compensation_max = models.DecimalField(
        max_digits=14, decimal_places=2, null=True, blank=True
    )
    compensation_currency = models.CharField(max_length=MAX_CURRENCY, blank=True, default="")
    compensation_interval = models.CharField(max_length=MAX_INTERVAL, blank=True, default="")
    description_excerpt = models.TextField(max_length=MAX_DESCRIPTION_EXCERPT, blank=True, default="")
    first_seen_at = models.DateTimeField()
    last_seen_at = models.DateTimeField()
    status = models.CharField(max_length=8, choices=Status.choices, default=Status.ACTIVE, db_index=True)
    source_metadata = models.JSONField(default=dict, blank=True)

    all_objects = JobListingQuerySet.as_manager()
    objects = ActiveJobListingManager.from_queryset(JobListingQuerySet)()

    class Meta:
        app_label = "crank"
        ordering = ["-last_seen_at", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["source", "external_id"],
                condition=~Q(external_id=""),
                name="unique_job_source_external_id",
            ),
            models.UniqueConstraint(
                fields=["source", "canonical_url"],
                name="unique_job_source_canonical_url",
            ),
        ]
        indexes = [
            models.Index(
                fields=["source", "status", "last_seen_at"],
                name="crank_joblist_fresh_idx",
            ),
            models.Index(
                fields=["employer_domain", "status"],
                name="crank_joblist_employer_idx",
            ),
        ]

    def __str__(self):
        return f"{self.title} ({self.employer_name})"

    def clean(self):
        super().clean()
        try:
            validate_job_url(self.canonical_url)
        except Exception as exc:
            raise ValidationError({"canonical_url": str(exc)}) from exc
        if (
            self.first_seen_at
            and self.last_seen_at
            and self.first_seen_at > self.last_seen_at
        ):
            raise ValidationError({"last_seen_at": "last_seen_at must not precede first_seen_at."})
        if self.compensation_min is not None and self.compensation_min < 0:
            raise ValidationError({"compensation_min": "Compensation cannot be negative."})
        if self.compensation_max is not None and self.compensation_max < 0:
            raise ValidationError({"compensation_max": "Compensation cannot be negative."})
        if (
            self.compensation_min is not None
            and self.compensation_max is not None
            and self.compensation_min > self.compensation_max
        ):
            raise ValidationError({"compensation_max": "Maximum cannot be below minimum."})

    @classmethod
    def ingest(cls, source, raw: RawJobListing):
        return cls.all_objects.get_queryset().upsert_from_raw(source, raw)

    @property
    def is_presentable(self):
        return self.status == self.Status.ACTIVE


__all__ = ["JobSourceCatalog", "JobListing"]
