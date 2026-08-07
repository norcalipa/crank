# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
from crank.agents.job_search.context import build_model_context, truncate_conversation


def _msg(role, content):
    return {"role": role, "content": content}


class TestTruncateConversation:
    def test_empty_input_returns_empty(self):
        assert truncate_conversation([], max_characters=100) == []

    def test_invalid_budget_raises(self):
        import pytest

        for bad in (0, -5, 3.5, "10"):
            with pytest.raises(ValueError):
                truncate_conversation([_msg("user", "hello")], max_characters=bad)

    def test_none_budget_is_unbounded_by_characters(self):
        history = [_msg("user", "a" * 500), _msg("assistant", "b" * 500)]
        result = truncate_conversation(
            history, max_characters=None, max_messages=1
        )
        assert len(result) == 1

    def test_keeps_newest_under_character_budget(self):
        history = [
            _msg("user", "first question"),
            _msg("assistant", "short answer"),
            _msg("user", "second question much longer than the rest"),
        ]
        result = truncate_conversation(history, max_characters=55)
        # Build up from the newest while <= budget; the older pair no longer fits.
        assert result == [_msg("user", "second question much longer than the rest")]

    def test_drops_oldest_first(self):
        history = [{"role": "user", "content": "a" * 200}] * 3
        result = truncate_conversation(
            history, max_characters=1000, max_messages=2
        )
        assert len(result) == 2

    def test_hard_cut_newest_with_elision_marker_when_over_budget(self):
        history = [_msg("user", "x" * 500)]
        result = truncate_conversation(history, max_characters=20)
        assert len(result) == 1
        assert "<elided...>" in result[0]["content"]
        assert len(result[0]["content"]) <= 20

    def test_does_not_mutate_input(self):
        history = [_msg("user", "hello"), _msg("assistant", "hi")]
        snapshot = list(history)
        truncate_conversation(history, max_characters=50, max_messages=1)
        assert history == snapshot

    def test_deterministic(self):
        history = [_msg("u", "one"), _msg("a", "two"), _msg("u", "three")]
        r1 = truncate_conversation(history, max_characters=30, max_messages=2)
        r2 = truncate_conversation(history, max_characters=30, max_messages=2)
        assert r1 == r2


class TestBuildModelContext:
    def test_appends_user_prompt_and_builds_messages(self):
        history = [_msg("user", "earlier preferences discussion")]
        context = build_model_context(
            prompt_id="job_search_system_v1",
            system="SYS",
            conversation=history,
            user_prompt="now recommend somewhere remote",
            preference_markdown="**budget** moderate",
            organization_catalog=[{"id": 7, "name": "Acme"}],
            score_summaries=[{"organization_id": 7, "score_type": "culture", "avg_score": 4.0}],
            max_preference_characters=100,
            max_conversation_characters=1000,
        )
        messages = context.to_messages()
        assert messages[0] == {"role": "system", "content": "SYS"}
        assert messages[1] == _msg("user", "earlier preferences discussion")
        assert messages[2] == _msg("user", "now recommend somewhere remote")
        rendered = messages[2]["content"] if len(messages) > 2 else ""
        assert "ORGANIZATION CATALOG" in " ".join(m["content"] for m in messages)
        assert "Acme" in " ".join(m["content"] for m in messages)

    def test_conversation_bounded(self):
        history = [_msg("user", "a" * 500)] * 4
        context = build_model_context(
            prompt_id="p",
            system="s",
            conversation=history,
            user_prompt="q",
            preference_markdown="",
            organization_catalog=[],
            score_summaries=[],
            max_preference_characters=100,
            max_conversation_characters=100,
            max_conversation_messages=2,
        )
        total = sum(len(m["content"]) for m in context.to_messages())
        # MAJOR-3: history + user turn share ONE budget; the total must not
        # approach 2x max_conversation_characters.
        assert total <= 100

    def test_long_user_prompt_does_not_double_budget(self):
        history = [_msg("user", "a" * 700)] * 10
        context = build_model_context(
            prompt_id="p",
            system="s",
            conversation=history,
            user_prompt="b" * 600,
            preference_markdown="",
            organization_catalog=[],
            score_summaries=[],
            max_preference_characters=100,
            max_conversation_characters=1000,
        )
        contents = [m["content"] for m in context.to_messages()]
        total = sum(len(c) for c in contents)
        assert total <= 1000
        # The latest user turn (the request) is preserved intact.
        assert contents[-1] == "b" * 600

    def test_score_summary_missing_keys_renders_without_keyerror(self):
        context = build_model_context(
            prompt_id="p",
            system="s",
            conversation=[],
            user_prompt="q",
            preference_markdown="",
            organization_catalog=[],
            score_summaries=[{"organization_id": 7, "score_type": "culture"}],
            max_preference_characters=100,
            max_conversation_characters=1000,
        )
        # NIT-2/MAJOR-4: rendering must not raise a bare KeyError on a
        # row missing avg_score.
        block = " ".join(m["content"] for m in context.to_messages())
        assert "organization_id=7" in block

    def test_preference_markdown_bounded(self):
        context = build_model_context(
            prompt_id="p",
            system="s",
            conversation=[],
            user_prompt="q",
            preference_markdown="p" * 500,
            organization_catalog=[],
            score_summaries=[],
            max_preference_characters=50,
            max_conversation_characters=1000,
        )
        assert len(context.preference_markdown) <= 50

    def test_catalog_bounded(self):
        catalog = [{"id": i, "name": f"org{i}"} for i in range(10)]
        context = build_model_context(
            prompt_id="p",
            system="s",
            conversation=[],
            user_prompt="q",
            preference_markdown="",
            organization_catalog=catalog,
            score_summaries=[{"organization_id": i} for i in range(10)],
            max_preference_characters=100,
            max_conversation_characters=1000,
            max_catalog_rows=5,
            max_score_rows=2,
        )
        assert len(context.organization_catalog) == 5
        assert len(context.score_summaries) == 2

class TestBuildModelContextEdge:
    def test_non_string_user_prompt_uses_full_history_budget(self):
        context = build_model_context(
            prompt_id="p", system="s",
            conversation=[{"role": "user", "content": "hi"}],
            user_prompt=123,  # not a string; user turn is skipped
            preference_markdown="", organization_catalog=[], score_summaries=[],
            max_preference_characters=100, max_conversation_characters=50,
        )
        contents = [m["content"] for m in context.to_messages()]
        assert "hi" in contents

    def test_none_catalog_rows_yield_empty(self):
        context = build_model_context(
            prompt_id="p", system="s",
            conversation=[], user_prompt="q",
            preference_markdown="", organization_catalog=None, score_summaries=None,
            max_preference_characters=100, max_conversation_characters=100,
        )
        assert context.organization_catalog == []
        assert context.score_summaries == []
