# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Assistant quality guardrails (issue #397).

Executable checks that keep the assistant from regressing into a useless echo
of the user's message, plus the helpfulness-gap signal used to size how many
conversations end a turn without ever producing a result card.

This module is deliberately free of provider, prompt, and model dependencies so
the golden conversation tests can exercise it fully offline.
"""
from __future__ import annotations

import re

#: Minimum number of assistant turns a conversation must have before we start
#: counting it toward the helpfulness-gap signal. This filters out cold-open
#: turns that legitimately elicit preferences before any result card is due.
MIN_HELPFUL_TURNS = 3

_WORD = re.compile(r"[a-z0-9]+")


def is_echo(user_prompt: str, message: str, *, tolerance: float = 0.8) -> bool:
    """Return True when ``message`` merely restates ``user_prompt``.

    A reply is an echo when at least ``tolerance`` (default 80%) of its
    meaningful tokens also appear in the user's message. This catches the
    classic demo-provider failure mode where the assistant parrots the user's
    turn back verbatim without calling a tool or citing server data, while
    leaving genuine answers, refusals, and clarifying questions (which share
    only a few tokens) untouched.
    """
    up_tokens = _WORD.findall((user_prompt or "").lower())
    reply_tokens = _WORD.findall((message or "").lower())
    if not up_tokens or not reply_tokens:
        return False
    user_set = set(up_tokens)
    overlap = sum(1 for token in reply_tokens if token in user_set)
    return overlap >= tolerance * len(reply_tokens)


def has_helpfulness_gap(*, assistant_turns: int, result_cards: int) -> bool:
    """Return True when a conversation is a helpfulness-gap risk.

    A conversation that has accumulated several assistant turns and produced
    no result card is the "chat is useless" signal we want to alert on: the
    assistant has been engaged repeatedly without ever surfacing a citation.
    """
    if assistant_turns < MIN_HELPFUL_TURNS:
        return False
    return result_cards == 0


__all__ = [
    "MIN_HELPFUL_TURNS",
    "has_helpfulness_gap",
    "is_echo",
]
