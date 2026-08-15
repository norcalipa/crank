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

#: Reply tokens that merely acknowledge the user ("Sure, show me jobs" ->
#: "show me jobs") and must not count toward an echo verdict. Only leading
#: acknowledgments are stripped so genuine answers keep their intent words.
_ACK = frozenset(
    {
        "sure", "ok", "okay", "yes", "yeah", "yep", "right", "great",
        "absolutely", "certainly", "gotcha", "alright", "alrighty",
        "fine", "cool", "thanks", "thank", "perfect", "got", "it",
    }
)

#: A reply shorter than this is a clarification/augment, not a full
#: restatement of the user's turn. Single-keyword replies such as
#: ``salary`` -> ``Salary?`` are legitimate clarifications and must not be
#: flagged as echoes (issue #423).
_MIN_SUBSTANTIVE_TOKENS = 3


def _strip_ack(tokens):
    """Drop leading acknowledgment tokens from a reply's token list."""
    start = 0
    while start < len(tokens) and tokens[start] in _ACK:
        start += 1
    return tokens[start:]


def is_echo(user_prompt: str, message: str, *, tolerance: float = 0.8) -> bool:
    """Return True when ``message`` merely restates ``user_prompt``.

    A reply is an echo when at least ``tolerance`` (default 80%) of its
    meaningful tokens also appear in the user's message. Leading
    acknowledgment words (``Sure``, ``Got it``) are stripped so a reply that
    paraphrases the user after a filler prefix is still caught (e.g.
    ``show me jobs`` -> ``Sure, show me jobs``). Very short replies (a
    keyword or two, e.g. ``salary`` -> ``Salary?``) are clarifying questions
    rather than restatements and are not flagged. This catches the classic
    demo-provider failure mode where the assistant parrots the user's turn
    back verbatim without calling a tool or citing server data, while leaving
    genuine answers, refusals, and clarifying questions untouched.
    """
    up_tokens = _WORD.findall((user_prompt or "").lower())
    reply_tokens = _strip_ack(_WORD.findall((message or "").lower()))
    if not up_tokens or not reply_tokens:
        return False
    if len(reply_tokens) < _MIN_SUBSTANTIVE_TOKENS:
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
