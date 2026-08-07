# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _
from django_extensions.db.models import TimeStampedModel

# The canonical schema version used by the preference lifecycle services in
# ``crank.services.preferences``. Bump this (and add a forward migration that
# maps old documents to the new shape) whenever the JSON schema changes.
SCHEMA_VERSION = 1


def default_preferences():
    """Return a fresh, schema-valid empty preferences document.

    This is the canonical version-1 document shape defined by the preference
    lifecycle services (issue #306): typed sections for compensation, culture,
    work location, geography, industry, funding stage, vesting, exclusions,
    priorities, and notes. The JSON object is the source of truth for
    deterministic matching; ``preferences_markdown`` is only a server-generated,
    human-readable projection of this document.
    """
    return {
        "compensation": {
            "minimum_salary": None,
            "currency": "USD",
            "equity_minimum_percent": None,
        },
        "culture": [],
        "work_location": {"modes": [], "countries": [], "require_onsite": None},
        "geography": {"regions": [], "remote_friendly": None},
        "industry": [],
        "funding_stage": [],
        "vesting": {
            "max_cliff_months": None,
            "max_vesting_months": None,
            "prefer_accelerated": None,
        },
        "exclusions": {"companies": [], "titles": [], "industries": [], "locations": []},
        "priorities": {},
        "notes": "",
    }


class UserPreference(TimeStampedModel):
    """Canonical, versioned preferences for a single authenticated user.

    - Preferences are created on first agent interaction, not at OAuth login.
    - The `preferences` JSONField is the canonical (versioned) source of truth.
    - `preferences_markdown` is a server-generated projection for prompts and
      user review; it is never trusted as an input source.
    - `schema_version` tracks the JSON document schema for migrations/validation.

    Retention note: preference rows are owned by the user and are cascade
    deleted with the owning user. No provider reasoning, hidden prompts, API
    credentials, or provider request payloads are ever stored here.
    """

    SCHEMA_VERSION = 1

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="preferences",
        verbose_name=_("user"),
    )
    preferences = models.JSONField(
        default=default_preferences,
        verbose_name=_("preferences"),
        help_text=_(
            "Versioned canonical preference document. Server-controlled source of truth."
        ),
    )
    preferences_markdown = models.TextField(
        default="",
        blank=True,
        verbose_name=_("preferences markdown"),
        help_text=_(
            "Server-generated markdown projection of the canonical preferences."
        ),
    )
    schema_version = models.PositiveIntegerField(
        default=SCHEMA_VERSION,
        verbose_name=_("schema version"),
        help_text=_("Version of the preferences JSON document schema."),
    )

    def __str__(self):
        # Never expose preference contents in the model representation.
        return _("Preferences for %(username)s") % {"username": self.user.username}

    class Meta:
        app_label = "crank"
        verbose_name = _("user preference")
        verbose_name_plural = _("user preferences")
        indexes = [
            models.Index(fields=["user"], name="crank_userpref_user_idx"),
        ]


class UserPreferenceAudit(TimeStampedModel):
    """Minimal, contents-free audit trail for preference lifecycle actions.

    Stores only metadata about a change -- who, when, which action, and how many
    fields changed -- and never duplicates sensitive preference values.
    """

    class Action(models.TextChoices):
        CREATED = "created", "Created"
        PATCHED = "patched", "Patched"
        RESET = "reset", "Reset"
        EXPORTED = "exported", "Exported"
        DELETED = "deleted", "Deleted"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="preference_audits",
    )
    action = models.CharField(max_length=16, choices=Action.choices)
    schema_version = models.PositiveIntegerField(default=SCHEMA_VERSION)
    change_count = models.PositiveIntegerField(default=0)

    class Meta:
        app_label = "crank"
        get_latest_by = "created"
        ordering = ["-created"]
