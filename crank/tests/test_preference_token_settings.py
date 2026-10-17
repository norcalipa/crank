# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""PREFERENCE_TOKEN_MAX_AGE_SECONDS parsing (issue #539)."""
import logging
import os
from unittest.mock import patch

import pytest
from django.conf import settings

from crank.settings import base as settings_base

NAME = "PREFERENCE_TOKEN_MAX_AGE_SECONDS"


def _parse(value):
    env = {NAME: value} if value is not None else {}
    with patch.dict("os.environ", env, clear=False):
        if value is None:
            os.environ.pop(NAME, None)
        return settings_base._env_positive_seconds(NAME, 3600)


def test_default_is_one_hour():
    assert settings.PREFERENCE_TOKEN_MAX_AGE_SECONDS == 3600


@pytest.mark.parametrize("raw,expected", [("90", 90), ("1", 1), (" 600 ", 600)])
def test_valid_values_are_used(raw, expected, caplog):
    with caplog.at_level(logging.WARNING):
        assert _parse(raw) == expected
    assert not caplog.records


@pytest.mark.parametrize("raw", ["0", "-5", "abc", "1.5", "  "])
def test_bad_or_negative_values_fall_back_with_a_warning(raw, caplog):
    with caplog.at_level(logging.WARNING, logger=settings_base.__name__):
        assert _parse(raw) == 3600
    assert any(NAME in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("raw", [None, ""])
def test_unset_or_empty_uses_the_default_quietly(raw, caplog):
    with caplog.at_level(logging.WARNING):
        assert _parse(raw) == 3600
    assert not caplog.records
