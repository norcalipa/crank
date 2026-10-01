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
        assert SYSTEM_PROMPT_VERSION == 5

    def test_prompt_text_is_tied_to_its_version(self):
        """Changing prompt wording without bumping SYSTEM_PROMPT_VERSION fails here.

        Whichever of #484a/#473b merges second rebases onto the next integer
        and re-records its digests.
        """
        import hashlib

        digests = {
            5: {
                (False, True): "9cd200f8f2c479bac14230095f778c2de2c7172ac08395e39ee94b99dc603955",
                (True, True): "0634a17783fbe452928765d472acee34e283b6f4ef81b9e4e58db6f029ffe26e",
                (True, False): "7a18ff8c8b5a63c990d93f4c349065c0fc180f6451434016112db94889fed0ce",
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
        assert prompt_id() == "job_search_system_v5"
        assert prompt_id(1) == "job_search_system_v1"

    def test_untrusted_markdown_warning(self):
        text = build_system_prompt()
        assert "untrusted" in text.lower()

    def test_contains_availability_honesty_rule(self):
        """The prompt instructs honest availability reporting (issue #476)."""
        text = build_system_prompt()
        assert "AVAILABILITY HONESTY" in text
        assert "AVAILABILITY STATE" in text
        assert "never claim zero matches when listings have not finished" in text
        assert "no results meet your" in text
