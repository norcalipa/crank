# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Settings-backed default configuration for the score resolution pipeline.

Curated aliases and score-type mappings can be declared in settings as plain
lists of mappings and are validated here into the frozen typed forms. This
keeps policy in database/operator-controlled settings (never in untrusted
payloads) while the resolution pipeline stays deterministic and testable with
explicit :class:`~crank.agents.sources.types.ResolutionConfig` values.
"""
from decimal import Decimal

from django.conf import settings as dj_settings

from crank.agents.sources.types import build_resolution_config


def default_resolution_config(settings=None) -> "ResolutionConfig":
    """Assemble the default :class:`ResolutionConfig` from Django settings.

    The resolved config is only used to *describe* the pipeline's policy; none
    of these values are ever read from an untrusted external payload. Missing
    optional settings fall back to conservative, safe defaults.
    """
    s = settings or dj_settings

    def _get(name, default):
        return getattr(s, name, default)

    type_mappings = _get("SCORE_TYPE_MAPPINGS", ())
    target_aliases = _get("SCORE_TARGET_ALIASES", ())
    return build_resolution_config(
        version=str(_get("SCORE_RESOLUTION_VERSION", "1")),
        source_key=str(_get("SCORE_RESOLUTION_SOURCE_KEY", "default")),
        source_organization=str(
            _get("RATING_SOURCE_ORGANIZATION_NAME", "")).strip() or None,
        score_type_mappings=type_mappings,
        target_aliases=target_aliases,
        max_value_magnitude=Decimal(str(
            _get("SCORE_MAX_VALUE_MAGNITUDE", "1e15"))),
        max_decimal_significant_digits=int(
            _get("SCORE_MAX_DECIMAL_SIGNIFICANT_DIGITS", 30)),
        max_string_length=int(_get("SCORE_MAX_STRING_LENGTH", 512)),
        max_observations=int(_get("SCORE_MAX_OBSERVATIONS", 100000)),
    )


__all__ = ["default_resolution_config"]
