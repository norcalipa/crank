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
_ACK_SINGLE = frozenset(
    {
        "sure", "ok", "okay", "yes", "yeah", "yep", "right", "great",
        "absolutely", "certainly", "gotcha", "alright", "alrighty",
        "fine", "cool", "thanks", "thank", "perfect", "got", "it",
    }
)

#: Multi-word acknowledgment phrases that must be stripped as a unit before
#: falling back to single-token stripping. These are common conversational
#: openers ("of course", "sure thing") whose individual tokens ("of",
#: "thing", "i", "understand") are not ack tokens on their own.
_ACK_PHRASES = (
    "of course", "sure thing", "i understand", "i see",
    "i got it", "no problem", "no worries", "you got it",
    "for sure", "all right",
)

#: Trailing politeness tokens that inflate the reply length and dilute the
#: overlap ratio below the echo threshold ("show me jobs" -> "show me jobs
#: please" drops from 100% to 75%). Stripping them before the overlap
#: calculation catches the echo without false-positiving genuine answers.
_TRAILING_POLITE = frozenset({"please", "thanks", "thank", "you"})

#: A reply shorter than this is a clarification/augment, not a full
#: restatement of the user's turn. Single-keyword replies such as
#: ``salary`` -> ``Salary?`` are legitimate clarifications and must not be
#: flagged as echoes (issue #423).
_MIN_SUBSTANTIVE_TOKENS = 3

#: Minimum fraction of the reply's meaningful tokens that must appear in the
#: user's message (after ack/politeness stripping) to count as an echo.
_ECHO_TOLERANCE = 0.8


def _is_question(message: str) -> bool:
    """Return True when ``message`` is phrased as a clarifying question.

    A short reply that restates the prompt with a trailing ``?`` (``remote
    jobs`` -> ``Remote jobs?``) is a clarification, not a verbatim
    restatement (issue #423 MINOR).
    """
    return (message or "").strip().endswith("?")


def _strip_ack(tokens):
    """Drop leading acknowledgment tokens from a reply's token list.

    Multi-word acknowledgment phrases ("of course", "sure thing", "i
    understand") are matched first as a single unit at a token boundary;
    only remaining leading single-word acks are then stripped one-by-one.
    """
    # Try multi-word ack phrases first. Matching is token-boundary aware: a
    # phrase must either consume the whole reply or be followed by a space, so
    # ``"i see"`` cannot fire on ``"i seeker..."`` (issue #423 NIT-2).
    text = " ".join(tokens)
    for phrase in _ACK_PHRASES:
        if text == phrase:
            tokens = []
            break
        if text.startswith(phrase + " "):
            tokens = text[len(phrase):].lstrip().split()
            break
    # Also strip leading single-token acks (covers "got it" which is already
    # in _ACK_SINGLE as individual tokens, plus single-word acks like "sure").
    start = 0
    while start < len(tokens) and tokens[start] in _ACK_SINGLE:
        start += 1
    return tokens[start:]


def is_echo(
    user_prompt: str, message: str, *, tolerance: float = _ECHO_TOLERANCE
) -> bool:
    """Return True when ``message`` merely restates ``user_prompt``.

    A reply is an echo when at least 80% (``_ECHO_TOLERANCE``) of its
    meaningful tokens also appear in the user's message. Leading
    acknowledgment words and phrases (``Sure``, ``Got it``, ``Of course``)
    are stripped so a reply that paraphrases the user after a filler prefix
    is still caught (e.g. ``show me jobs`` -> ``Sure, show me jobs``).
    Trailing politeness tokens (``please``, ``thanks``) are also stripped so
    they cannot dilute the overlap ratio (e.g. ``show me jobs`` -> ``show me
    jobs please``). A keyword ``tolerance`` (default 0.8, ``_ECHO_TOLERANCE``)
    sets the minimum overlap fraction required for an echo verdict. A
    verbatim restatement of the prompt is always an echo, except for short
    clarifying questions that add punctuation (``"remote jobs"`` ->
    ``"Remote jobs?"`` and ``"salary"`` -> ``"Salary?"``). Very short replies (a
    keyword or two, e.g. ``salary`` -> ``Salary?``) are clarifying questions
    rather than restatements and are not flagged. This catches the classic
    demo-provider failure mode where the assistant parrots the user's turn
    back verbatim without calling a tool or citing server data, while leaving
    genuine answers, refusals, and clarifying questions untouched.

    ``tolerance`` must be in ``(0, 1]``; anything else raises ``ValueError``.
    """
    if not 0.0 < tolerance <= 1.0:
        raise ValueError("tolerance must be in the interval (0, 1]")
    up_tokens = _WORD.findall((user_prompt or "").lower())
    reply_tokens = _strip_ack(_WORD.findall((message or "").lower()))
    # Strip trailing politeness tokens ("please", "thanks") that dilute the
    # overlap ratio below the echo threshold (NIT-1).
    while reply_tokens and reply_tokens[-1] in _TRAILING_POLITE:
        reply_tokens = reply_tokens[:-1]
    if not up_tokens or not reply_tokens:
        return False
    # MAJOR-1: a verbatim restatement is always an echo regardless of length.
    # The ``_MIN_SUBSTANTIVE_TOKENS`` exemption is for genuine short
    # clarifications (``salary`` -> ``Salary?``), not for parroting back a
    # 1- or 2-token prompt.
    #
    # For 2+ token prompts, exact token equality is always an echo.
    # For single-token prompts, distinguish verbatim echo (``jobs`` -> ``jobs``)
    # from a clarifying question (``salary`` -> ``Salary?``) by checking raw
    # string equality: punctuation like ``?`` makes it a question, not a
    # restatement.
    if reply_tokens == up_tokens:
        if len(up_tokens) >= 2:
            # A short multi-token reply phrased as a question (``remote
            # jobs`` -> ``Remote jobs?``) is a clarification, not a verbatim
            # restatement (MINOR-1). Longer exact restatements stay echoes.
            if _is_question(message) and len(reply_tokens) < _MIN_SUBSTANTIVE_TOKENS:
                return False
            return True
        if (message or "").strip().lower() == (user_prompt or "").strip().lower():
            return True
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
