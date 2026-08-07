# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _
from django_extensions.db.models import TimeStampedModel


def default_preferences():
    """Return the canonical, schema-versioned default preference document.

    The JSON object is the source of truth for deterministic matching. It
    intentionally distinguishes the `required` criteria, `optional` criteria,
    `exclusions`, and free-form `notes` so that typed filtering never has to
    parse an unchecked markdown blob. `preferences_markdown` is only a
    server-generated, human-readable projection of this document.
    """
    return {
        "required": {},
        "optional": {},
        "exclusions": [],
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