# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Operator-managed capability switches and credentials-safe change audit."""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django_extensions.db.models import TimeStampedModel


ALLOWED_CAPABILITY_KEYS = frozenset(
    {
        "interactive_agent",
        "job_pipeline",
        "gather_scores",
        "agent_noop",
    }
)


class CapabilitySwitch(TimeStampedModel):
    """A small, database-backed kill switch for an existing capability."""

    key = models.CharField(max_length=64, unique=True)
    enabled = models.BooleanField(default=True, db_index=True)
    note = models.CharField(max_length=200, blank=True, default="")

    class Meta:
        app_label = "crank"
        ordering = ["key"]

    def clean(self):
        super().clean()
        from django.core.exceptions import ValidationError

        if self.key not in ALLOWED_CAPABILITY_KEYS:
            raise ValidationError({"key": "Capability key is not registered."})

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.key} [{'on' if self.enabled else 'off'}]"


class OperationalChangeAudit(TimeStampedModel):
    """Actor/timestamp/target/old/new audit for operational admin changes."""

    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="operational_change_audits",
    )
    target_type = models.CharField(max_length=32)
    target_id = models.CharField(max_length=64)
    action = models.CharField(max_length=32)
    old_value = models.JSONField(default=dict, blank=True)
    new_value = models.JSONField(default=dict, blank=True)
    confirmed = models.BooleanField(default=False)

    class Meta:
        app_label = "crank"
        ordering = ["-created", "-id"]
        indexes = [
            models.Index(fields=["target_type", "target_id"], name="crank_opaudit_target_idx"),
            models.Index(fields=["action"], name="crank_opaudit_action_idx"),
        ]

    def __str__(self):
        return f"{self.action}:{self.target_type}:{self.target_id}"

    @staticmethod
    def _safe_value(value):
        """Keep audit JSON bounded and free of raw content or credentials."""
        if isinstance(value, dict):
            result = {}
            for key, item in value.items():
                key_text = str(key)[:64]
                if any(word in key_text.lower() for word in ("secret", "token", "password", "body", "prompt", "response")):
                    result[key_text] = "<redacted>"
                else:
                    result[key_text] = OperationalChangeAudit._safe_value(item)
            return result
        if isinstance(value, (list, tuple)):
            return [OperationalChangeAudit._safe_value(item) for item in value[:20]]
        if value is None or isinstance(value, (bool, int, float)):
            return value
        return str(value)[:200]

    @classmethod
    def record(cls, *, actor, target_type, target_id, action, old_value=None, new_value=None, confirmed=False):
        return cls.objects.create(
            actor=actor if actor and actor.is_authenticated else None,
            target_type=str(target_type)[:32],
            target_id=str(target_id)[:64],
            action=str(action)[:32],
            old_value=cls._safe_value(old_value or {}),
            new_value=cls._safe_value(new_value or {}),
            confirmed=bool(confirmed),
        )


__all__ = ["ALLOWED_CAPABILITY_KEYS", "CapabilitySwitch", "OperationalChangeAudit"]
