# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
from crank.agents.job_search.system_prompt import (
    JOB_LISTING_DETAIL_TOOL_NAME,
    JOB_LISTING_SEARCH_TOOL_NAME,
    ORGANIZATION_TOOL_NAME,
    SCORE_SUMMARY_TOOL_NAME,
    SYSTEM_PROMPT_VERSION,
    build_system_prompt,
    prompt_id,
)


class TestSystemPrompt:
    def test_version_is_current(self):
        assert SYSTEM_PROMPT_VERSION == 6

    def test_prompt_text_is_tied_to_its_version(self):
        """Changing prompt wording without bumping SYSTEM_PROMPT_VERSION fails here.

        Whichever of #484a/#473b merges second rebases onto the next integer
        and re-records its digests. v6 has not merged (main is v5), so wording
        changed during its review re-records v6 rather than taking a new integer.
        """
        import hashlib

        digests = {
            6: {
                (False, True): "26c667cb525dcc4a7b16019e63952d2d844d82ca03e3ecfe50336564add8f3ef",
                (True, True): "8cc2e49c4498b1eb918592b01ae35d3f1bad75f85e8cb6cbd82515661a3b5686",
                (True, False): "3b73a5f864c6929f8c8c18290794a872277a7c10e84013fe761339f8ab5753ca",
            },
        }
        for (with_context, with_actions), expected in digests[SYSTEM_PROMPT_VERSION].items():
            text = build_system_prompt(
                include_page_context=with_context, include_actions=with_actions
            )
            assert hashlib.sha256(text.encode()).hexdigest() == expected

    def test_contains_hard_citation_constraint(self):
        text = build_system_prompt()
        assert "ORGANIZATION CATALOG" in text
        assert "Never invent, guess, or reuse" in text

    def test_forbids_sql_urls_and_hosts(self):
        text = build_system_prompt()
        assert "Never generate SQL" in text
        assert "URLs" in text
        assert "hostnames" in text

    def test_names_all_bounded_tools(self):
        text = build_system_prompt()
        assert ORGANIZATION_TOOL_NAME in text
        assert SCORE_SUMMARY_TOOL_NAME in text
        assert JOB_LISTING_SEARCH_TOOL_NAME in text
        assert JOB_LISTING_DETAIL_TOOL_NAME in text

    def test_render_limits_into_tool_descriptions(self):
        text = build_system_prompt(max_organizations=11, max_score_rows=3, max_job_listings=7)
        assert "up to 11 public organization IDs" in text
        assert "up to 3 average scores" in text
        assert "up to 7 listings" in text

    def test_custom_rules_are_appended(self):
        text = build_system_prompt(custom_rules=["do not mention pricing"])
        assert "- do not mention pricing" in text

    def test_prompt_id_is_stable(self):
        assert prompt_id() == "job_search_system_v6"
        assert prompt_id(1) == "job_search_system_v1"

    def test_untrusted_markdown_warning(self):
        text = build_system_prompt()
        assert "untrusted" in text.lower()

    def test_contains_evidence_honesty_rule(self):
        """v6 tells the model how to word stale and unverified facts (issue #473)."""
        text = build_system_prompt()
        assert "EVIDENCE HONESTY" in text
        assert "facts=verified:V,stale:S,unknown:U,newest_verified=<date|never>" in text
        # Counts are never written after the word a citation uses.
        assert "evidence=verified" not in text
        assert "These are counts of facts, not evidence ids." in text
        assert "say UTC (for example 'last verified 2025-08-15 UTC')" in text
        assert (
            "unconfirmed_reasons= are the matcher's reading of a source that may "
            "not say so in those words: give one only as sourced but not confirmed, "
            "never as a fact and never as verified." in text
        )
        assert "[evidence=<id>,stale,last_verified=<date|never>]" in text
        # A stale fact is dated from its own reference, never from the
        # organization's newest check.
        assert "'last verified <date>' using the date on that same reference" in text
        assert "never give it as the date a particular fact was verified" in text
        # Profile-backed outcomes have a rule of their own.
        assert "[source=organization.<field>,profile]" in text
        assert "call it profile data or not verified" in text
        assert "[evidence=<id>,under_review]" in text
        assert "never as plainly verified" in text
        assert (
            "Never call unknown, stale, unconfirmed, changed, profile or "
            "pending-review facts verified" in text
        )

    def test_contains_availability_honesty_rule(self):
        """The prompt instructs honest availability reporting (issue #476)."""
        text = build_system_prompt()
        assert "AVAILABILITY HONESTY" in text
        assert "AVAILABILITY STATE" in text
        assert "never claim zero matches when listings have not finished" in text
        assert "no results meet your" in text
