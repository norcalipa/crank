# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Tests for the production capability configuration contract (issue #440)."""

from django.conf import settings
from django.test import SimpleTestCase, override_settings

from crank.capability import (
    CONFIG_VERSION,
    CapabilityReport,
    CapabilityStatus,
    capability_issues,
    capability_report,
)


class CapabilityStatusTests(SimpleTestCase):
    def test_disabled_capability_has_no_issues(self):
        status = CapabilityStatus(name="test", enabled=False)
        self.assertTrue(status.ok)
        self.assertEqual(status.issues, [])

    def test_enabled_with_issues_not_ok(self):
        status = CapabilityStatus(name="test", enabled=True, issues=["missing"])
        self.assertFalse(status.ok)
        self.assertEqual(status.issues, ["missing"])

    def test_enabled_without_issues_ok(self):
        status = CapabilityStatus(name="test", enabled=True)
        self.assertTrue(status.ok)


class CapabilityReportTests(SimpleTestCase):
    def test_all_ok_when_no_issues(self):
        report = CapabilityReport(
            capabilities=[CapabilityStatus(name="a", enabled=False)],
            config_version="1",
        )
        self.assertTrue(report.all_ok)

    def test_not_all_ok_when_issues_exist(self):
        report = CapabilityReport(
            capabilities=[
                CapabilityStatus(name="a", enabled=True, issues=["x"]),
            ],
            config_version="1",
        )
        self.assertFalse(report.all_ok)

    def test_to_dict_never_includes_secrets(self):
        import json

        report = CapabilityReport(
            capabilities=[
                CapabilityStatus(name="a", enabled=True, issues=["missing key"]),
            ],
            config_version="1",
        )
        blob = json.dumps(report.to_dict())
        self.assertNotIn("api_key", blob.lower())
        self.assertNotIn("secret", blob.lower())
        self.assertIn("missing key", blob)
        self.assertIn("config_version", blob)
        self.assertIn("all_ok", blob)


class InteractiveAgentStatusTests(SimpleTestCase):
    @override_settings(INTERACTIVE_AGENT_ENABLED=False)
    def test_disabled_is_ok(self):
        report = capability_report()
        ia = next(c for c in report.capabilities if c.name == "interactive_agent")
        self.assertFalse(ia.enabled)
        self.assertTrue(ia.ok)

    @override_settings(
        INTERACTIVE_AGENT_ENABLED=True,
        LLM_PROVIDER="crank.agents.llm:FakeLLMProvider",
        LLM_MODEL="test-model",
        LLM_API_KEY="test-key",
    )
    def test_enabled_with_all_config_is_ok(self):
        report = capability_report()
        ia = next(c for c in report.capabilities if c.name == "interactive_agent")
        self.assertTrue(ia.enabled)
        self.assertTrue(ia.ok)
        self.assertEqual(ia.issues, [])

    @override_settings(
        INTERACTIVE_AGENT_ENABLED=True,
        LLM_PROVIDER="",
        LLM_MODEL="",
        LLM_API_KEY="",
    )
    def test_enabled_missing_all_config_reports_issues(self):
        report = capability_report()
        ia = next(c for c in report.capabilities if c.name == "interactive_agent")
        self.assertTrue(ia.enabled)
        self.assertFalse(ia.ok)
        self.assertEqual(len(ia.issues), 3)
        self.assertIn("LLM_PROVIDER is not set", ia.issues[0])
        self.assertIn("LLM_MODEL is not set", ia.issues[1])
        self.assertIn("LLM_API_KEY is missing", ia.issues[2])

    @override_settings(
        INTERACTIVE_AGENT_ENABLED=True,
        LLM_PROVIDER="crank.agents.llm:OpenAIChatAdapter",
        LLM_MODEL="",
        LLM_API_KEY="sk-test",
    )
    def test_enabled_missing_model_reports_issue(self):
        report = capability_report()
        ia = next(c for c in report.capabilities if c.name == "interactive_agent")
        self.assertTrue(ia.enabled)
        self.assertFalse(ia.ok)
        self.assertEqual(len(ia.issues), 1)
        self.assertIn("LLM_MODEL", ia.issues[0])

    @override_settings(
        INTERACTIVE_AGENT_ENABLED=True,
        LLM_PROVIDER="crank.agents.llm:OpenAIChatAdapter",
        LLM_MODEL="gpt-4",
        LLM_API_KEY="",
    )
    def test_enabled_missing_api_key_reports_issue(self):
        report = capability_report()
        ia = next(c for c in report.capabilities if c.name == "interactive_agent")
        self.assertTrue(ia.enabled)
        self.assertFalse(ia.ok)
        self.assertEqual(len(ia.issues), 1)
        self.assertIn("LLM_API_KEY is missing", ia.issues[0])


class JobPipelineStatusTests(SimpleTestCase):
    @override_settings(JOB_PIPELINE_ENABLED=False)
    def test_disabled_is_ok(self):
        report = capability_report()
        jp = next(c for c in report.capabilities if c.name == "job_pipeline")
        self.assertFalse(jp.enabled)
        self.assertTrue(jp.ok)

    @override_settings(
        JOB_PIPELINE_ENABLED=True,
        AGENT_RUN_ENABLED=True,
    )
    def test_enabled_with_master_switch_is_ok(self):
        report = capability_report()
        jp = next(c for c in report.capabilities if c.name == "job_pipeline")
        self.assertTrue(jp.enabled)
        self.assertTrue(jp.ok)

    @override_settings(
        JOB_PIPELINE_ENABLED=True,
        AGENT_RUN_ENABLED=False,
    )
    def test_enabled_without_master_switch_reports_issue(self):
        report = capability_report()
        jp = next(c for c in report.capabilities if c.name == "job_pipeline")
        self.assertTrue(jp.enabled)
        self.assertFalse(jp.ok)
        self.assertEqual(len(jp.issues), 1)
        self.assertIn("AGENT_RUN_ENABLED is false", jp.issues[0])


class CrawlStatusTests(SimpleTestCase):
    @override_settings(CRAWL_CRON_ENABLED=False)
    def test_disabled_is_ok(self):
        report = capability_report()
        crawl = next(c for c in report.capabilities if c.name == "crawl")
        self.assertFalse(crawl.enabled)
        self.assertTrue(crawl.ok)

    @override_settings(
        CRAWL_CRON_ENABLED=True,
        AGENT_RUN_ENABLED=True,
    )
    def test_enabled_with_master_switch_is_ok(self):
        report = capability_report()
        crawl = next(c for c in report.capabilities if c.name == "crawl")
        self.assertTrue(crawl.enabled)
        self.assertTrue(crawl.ok)

    @override_settings(
        CRAWL_CRON_ENABLED=True,
        AGENT_RUN_ENABLED=False,
    )
    def test_enabled_without_master_switch_reports_issue(self):
        report = capability_report()
        crawl = next(c for c in report.capabilities if c.name == "crawl")
        self.assertTrue(crawl.enabled)
        self.assertFalse(crawl.ok)
        self.assertEqual(len(crawl.issues), 1)
        self.assertIn("AGENT_RUN_ENABLED is false", crawl.issues[0])


class CapabilityReportAggregateTests(SimpleTestCase):
    def test_config_version_is_string(self):
        self.assertIsInstance(CONFIG_VERSION, str)
        self.assertTrue(CONFIG_VERSION)

    @override_settings(
        INTERACTIVE_AGENT_ENABLED=False,
        JOB_PIPELINE_ENABLED=False,
        CRAWL_CRON_ENABLED=False,
    )
    def test_all_disabled_all_ok(self):
        report = capability_report()
        self.assertTrue(report.all_ok)
        self.assertEqual(len(report.capabilities), 3)

    @override_settings(
        INTERACTIVE_AGENT_ENABLED=True,
        LLM_PROVIDER="",
        LLM_MODEL="",
        LLM_API_KEY="",
        JOB_PIPELINE_ENABLED=False,
        CRAWL_CRON_ENABLED=False,
    )
    def test_one_broken_capability_makes_not_all_ok(self):
        report = capability_report()
        self.assertFalse(report.all_ok)


class CapabilityIssuesTests(SimpleTestCase):
    @override_settings(
        INTERACTIVE_AGENT_ENABLED=False,
        JOB_PIPELINE_ENABLED=False,
        CRAWL_CRON_ENABLED=False,
    )
    def test_empty_when_all_disabled(self):
        self.assertEqual(capability_issues(), [])

    @override_settings(
        INTERACTIVE_AGENT_ENABLED=True,
        LLM_PROVIDER="",
        LLM_MODEL="",
        LLM_API_KEY="",
        JOB_PIPELINE_ENABLED=True,
        AGENT_RUN_ENABLED=False,
        CRAWL_CRON_ENABLED=True,
    )
    def test_flat_list_of_issues(self):
        issues = capability_issues()
        self.assertTrue(len(issues) >= 3)
        self.assertTrue(any("interactive_agent" in i for i in issues))
        self.assertTrue(any("job_pipeline" in i for i in issues))
        self.assertTrue(any("crawl" in i for i in issues))
        # Issue strings reference setting names, not secret values.
        # Verify no actual secret values are present.
        for issue in issues:
            self.assertNotIn("sk-", issue)
            self.assertNotIn("password", issue.lower())
            # Issues are non-secret description strings, not key values.
            self.assertTrue(len(issue) < 200)  # bounded, not a key/credential
