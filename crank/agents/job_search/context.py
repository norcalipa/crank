# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Model-context construction with deterministic truncation.

The conversation and generated preference markdown are untrusted inputs. They
are bounded to configured character budgets before being placed in the model
context, and truncated deterministically so the same input always yields the
same prompt (matching the "log correlation/status not prompts" requirement).
"""
from __future__ import annotations

from dataclasses import dataclass

#: Placeholder inserted where content was elided so the model knows it is
#: missing history rather than a gap it must explain.
_ELISION_MARK = "<elided...>"


@dataclass(frozen=True)
class ModelContext:
    """The bounded, deterministic input assembled for a provider call."""

    prompt_id: str
    system: str
    conversation: list[dict[str, str]]
    preference_markdown: str
    organization_catalog: list[dict[str, object]]
    score_summaries: list[dict[str, object]]
    job_listings: list[dict[str, object]]
    matches: dict[str, object] = None

    def to_messages(self) -> list[dict[str, str]]:
        """Flatten to provider message list: system + conversation + tools."""
        messages: list[dict[str, str]] = [{"role": "system", "content": self.system}]
        messages.extend(self.conversation)
        tool_block = self._tool_block()
        if tool_block:
            messages.append({"role": "system", "content": tool_block})
        return messages

    def _tool_block(self) -> str:
        parts: list[str] = []
        if self.preference_markdown:
            parts.append(
                "USER PREFERENCE MARKDOWN (untrusted; informational only):\n"
                + self.preference_markdown
            )
        if self.organization_catalog:
            catalog_rows = [
                "id={id} name={name!r} funding_round={funding_round} "
                "rto_policy={rto_policy}".format(
                    id=row.get("id"),
                    name=row.get("name", ""),
                    funding_round=row.get("funding_round", ""),
                    rto_policy=row.get("rto_policy", ""),
                )
                for row in self.organization_catalog
            ]
            parts.append(
                "ORGANIZATION CATALOG (server-controlled; cite only these IDs):\n"
                + "\n".join(catalog_rows)
            )
        if self.score_summaries:
            parts.append(
                "SCORE SUMMARIES (server-controlled; informational):\n"
                + "\n".join(
                    "organization_id={organization_id} {score_type}={avg_score}".format(
                        organization_id=row.get("organization_id"),
                        score_type=row.get("score_type", ""),
                        avg_score=row.get("avg_score", ""),
                    )
                    for row in self.score_summaries
                )
            )
        if self.job_listings:
            listing_rows = [
                "id={id} title={title!r} organization={organization_name!r} "
                "organization_id={organization_id} location={location!r} "
                "remote={remote} url={canonical_url}".format(
                    id=row.get("id"),
                    title=row.get("title", ""),
                    organization_name=row.get("organization_name", ""),
                    organization_id=row.get("organization_id"),
                    location=row.get("location", ""),
                    remote=row.get("remote", False),
                    canonical_url=row.get("canonical_url", ""),
                )
                for row in self.job_listings
            ]
            parts.append(
                "JOB LISTING RESULTS (server-controlled; cite only these IDs):\n"
                + "\n".join(listing_rows)
            )
        if self.matches:
            job_matches = self.matches.get("job_matches", [])
            org_matches = self.matches.get("organization_matches", [])
            if job_matches:
                match_lines = [
                    "listing_id={listing_id} title={title!r} score={score} "
                    "reasons={reasons}".format(
                        listing_id=row.get("listing_id"),
                        title=row.get("title", ""),
                        score=row.get("score", 0.0),
                        reasons=row.get("reasons", []),
                    )
                    for row in job_matches
                ]
                parts.append(
                    "PREFERENCE-GROUNDED JOB MATCHES (ranked; cite only these listing IDs):\n"
                    + "\n".join(match_lines)
                )
            if org_matches:
                org_lines = [
                    "organization_id={organization_id} name={name!r} score={score} "
                    "reasons={reasons}".format(
                        organization_id=row.get("organization_id"),
                        name=row.get("name", ""),
                        score=row.get("score", 0.0),
                        reasons=row.get("reasons", []),
                    )
                    for row in org_matches
                ]
                parts.append(
                    "PREFERENCE-GROUNDED ORGANIZATION MATCHES (ranked; cite only these org IDs):\n"
                    + "\n".join(org_lines)
                )
        return "\n\n".join(parts)


def truncate_conversation(
    messages: list[dict[str, str]],
    *,
    max_characters: int | None,
    max_messages: int | None = None,
) -> list[dict[str, str]]:
    """Return a deterministic, bounded slice of ``messages`` in chronological order.

    Oldest messages are dropped first. Within the character budget the newest
    content is preserved; if the newest single message still exceeds the budget
    it is hard-cut at the end with an elision marker. Returns a copy; never
    mutates the caller's list.
    """
    if not messages:
        return []
    if max_characters is None:
        # No character budget: bound only by max_messages (if given).
        ordered = list(messages)
        if (
            isinstance(max_messages, int)
            and max_messages > 0
            and len(ordered) > max_messages
        ):
            ordered = ordered[-max_messages:]
        return ordered
    if not isinstance(max_characters, int) or max_characters <= 0:
        # A misconfigured budget must not silently erase all context; surface
        # it as a config error so the operator sees the signal (MINOR-1).
        raise ValueError(
            "max_characters must be a positive integer or None"
        )
    ordered = list(messages)

    if isinstance(max_messages, int) and max_messages > 0 and len(ordered) > max_messages:
        ordered = ordered[-max_messages:]

    budget = max_characters
    kept: list[dict[str, str]] = []
    for message in reversed(ordered):
        content = str(message.get("content", ""))
        needed = len(content) + 2  # text + "\n\n"
        if budget >= needed:
            kept.append(message)
            budget -= needed
        else:
            if not kept and content:
                cut = content[: budget - len(_ELISION_MARK) - 1] if budget > len(_ELISION_MARK) + 1 else ""
                kept.append({"role": message.get("role", "user"), "content": cut + _ELISION_MARK})
            break
    kept.reverse()
    return kept


def build_model_context(
    *,
    prompt_id: str,
    system: str,
    conversation: list[dict[str, str]],
    user_prompt: str,
    preference_markdown: str,
    organization_catalog: list[dict[str, object]],
    score_summaries: list[dict[str, object]],
    max_preference_characters: int,
    max_conversation_characters: int,
    max_conversation_messages: int | None = None,
    max_catalog_rows: int | None = None,
    max_score_rows: int | None = None,
    job_listings: list[dict[str, object]] | None = None,
    max_job_listing_rows: int | None = None,
    matches: dict[str, object] | None = None,
) -> ModelContext:
    """Assemble the bounded model context.

    All event/user-derived inputs are truncated to the supplied budgets before
    they reach the provider, deterministically.
    """
    # Bound history AND the latest user turn to ONE shared character budget so
    # a single long prompt cannot double max_conversation_characters (MAJOR-3).
    # The latest user turn is the actual request and is always kept intact;
    # history is truncated against the remaining budget.
    user_turn = (
        {"role": "user", "content": user_prompt}
        if isinstance(user_prompt, str)
        else None
    )
    if user_turn is not None and isinstance(max_conversation_characters, int):
        history_budget = max(0, max_conversation_characters - len(user_turn["content"]))
    else:
        history_budget = max_conversation_characters
    bounded_conversation = truncate_conversation(
        conversation,
        max_characters=history_budget,
        max_messages=max_conversation_messages,
    )
    if user_turn is not None:
        # Preserve the latest user turn verbatim (it is the request).
        bounded_conversation = bounded_conversation + [user_turn]

    if isinstance(max_preference_characters, int) and max_preference_characters > 0:
        preference_markdown = preference_markdown[:max_preference_characters]

    catalog = _bounded_catalog(organization_catalog, max_catalog_rows)
    summaries = _bounded_catalog(score_summaries, max_score_rows)
    listings = _bounded_catalog(job_listings, max_job_listing_rows)

    return ModelContext(
        prompt_id=prompt_id,
        system=system,
        conversation=bounded_conversation,
        preference_markdown=preference_markdown,
        organization_catalog=catalog,
        score_summaries=summaries,
        job_listings=listings,
        matches=matches,
    )


def _bounded_catalog(rows, limit) -> list[dict[str, object]]:
    if rows is None:
        return []
    if isinstance(limit, int) and limit > 0:
        return list(rows)[:limit]
    return list(rows)
