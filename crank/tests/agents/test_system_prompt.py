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
        assert SYSTEM_PROMPT_VERSION == 4

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
        assert prompt_id() == "job_search_system_v4"
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
