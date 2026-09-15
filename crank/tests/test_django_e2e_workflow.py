# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""CI gate integrity for the Django-backed Playwright workflow (issue #491).

The ``django-e2e`` job is a merge gate for the surfaces its journeys exercise,
so its ``push`` and ``pull_request`` triggers must stay in exact path parity,
and both must cover the system under test (the application files the journeys
render), not only the harness. A drift in either direction silently ungates
PRs: a workflow that only lists harness paths never starts for UI-only
regressions, and divergent trigger filters gate pushes but not PRs (or vice
versa).
"""

from pathlib import Path

import yaml

from django.test import TestCase

WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "playwright-django.yml"

#: The tier's own files (config, specs, seeder, outage hook, workflow).
HARNESS_PATHS = {
    ".github/workflows/playwright-django.yml",
    "crank/agents/job_search/demo.py",
    "crank/management/commands/seed_e2e.py",
    "e2e/django/**",
    "playwright.django.config.ts",
}

#: The application surfaces the journeys render and assert against.
APPLICATION_PATHS = {
    "crank/views/**",
    "package-lock.json",
    "static/css/**",
    "static/js/**",
    "templates/**",
    "webpack.config.js",
    "webpack.e2e.config.js",
}


def _trigger_paths() -> dict:
    document = yaml.safe_load(WORKFLOW.read_text())
    # YAML 1.1 parses the bare `on:` key as the boolean True.
    triggers = document.get(True) or document.get("on")
    assert triggers is not None, "playwright-django.yml has no trigger map"
    return triggers


class DjangoE2EWorkflowPathTests(TestCase):
    """Trigger path parity and system-under-test coverage."""

    def test_push_and_pull_request_paths_are_in_exact_parity(self):
        triggers = _trigger_paths()
        self.assertIn("push", triggers)
        self.assertIn("pull_request", triggers)
        push_paths = triggers["push"].get("paths")
        pr_paths = triggers["pull_request"].get("paths")
        self.assertIsNotNone(push_paths, "push trigger must filter paths")
        self.assertIsNotNone(pr_paths, "pull_request trigger must filter paths")
        # A trigger whose filter lists harness-only paths gates merges on the
        # application; a divergent filter gates one event but not the other.
        self.assertEqual(set(push_paths), set(pr_paths))
        self.assertEqual(len(push_paths), len(pr_paths), "duplicate path entries")

    def test_triggers_cover_the_harness_files(self):
        paths = set(_trigger_paths()["push"]["paths"])
        missing = HARNESS_PATHS - paths
        self.assertEqual(missing, set(), f"harness paths missing from the filter: {missing}")

    def test_triggers_cover_the_system_under_test(self):
        paths = set(_trigger_paths()["push"]["paths"])
        missing = APPLICATION_PATHS - paths
        self.assertEqual(missing, set(), f"application paths missing from the filter: {missing}")
