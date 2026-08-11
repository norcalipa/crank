# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
from types import SimpleNamespace

from django.test import TestCase

from crank.agents.jobs.base import JobSourceAdapter, JobSourceQuery, JobSourceResult, RawJobListing
from crank.agents.jobs.errors import (
    JobSourceBlocked,
    JobSourceDisabled,
    JobSourceNotApproved,
    UnknownJobAdapter,
)
from crank.agents.jobs.registry import JobAdapterRegistry, REGISTRY, build_job_adapter, register_job_adapter
from crank.models.job import JobSourceCatalog


class FakeAdapter(JobSourceAdapter):
    key = "fake.jobs.v1"
    version = "1"

    def fetch(self, query):
        return JobSourceResult(listings=())


class JobAdapterContractTests(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if REGISTRY.get(FakeAdapter.key) is None:
            register_job_adapter(FakeAdapter)

    def source(self, **kwargs):
        values = {
            "name": "Source",
            "adapter_key": FakeAdapter.key,
            "base_url": "https://jobs.example.test",
            "catalog_metadata": {},
        }
        values.update(kwargs)
        return JobSourceCatalog.objects.create(**values)

    def test_registered_adapter_requires_approval_and_enablement(self):
        with self.assertRaises(JobSourceNotApproved):
            build_job_adapter(self.source())
        with self.assertRaises(JobSourceDisabled):
            build_job_adapter(self.source(name="approved", approval_state="approved", enabled=False))
        adapter = build_job_adapter(
            self.source(name="enabled", approval_state="approved", enabled=True)
        )
        self.assertIsInstance(adapter, FakeAdapter)

    def test_blocked_and_unknown_sources_are_rejected(self):
        with self.assertRaises(JobSourceBlocked):
            build_job_adapter(
                self.source(approval_state="blocked", enabled=True)
            )
        with self.assertRaises(UnknownJobAdapter):
            build_job_adapter(
                self.source(name="unknown", adapter_key="missing", approval_state="approved", enabled=True)
            )

    def test_registry_rejects_duplicate_keys_and_invalid_keys(self):
        with self.assertRaises(ValueError):
            register_job_adapter(FakeAdapter)
        registry = JobAdapterRegistry()
        with self.assertRaises(ValueError):
            registry.register(type("NoKey", (), {}))
        with self.assertRaises(ValueError):
            registry.register(type("BlankKey", (), {"key": "  "}))
        self.assertEqual(registry.keys(), [])
        registry.register(FakeAdapter)
        self.assertEqual(registry.keys(), [FakeAdapter.key])
        self.assertIs(registry.get(FakeAdapter.key.upper()), FakeAdapter)

    def test_rejects_unapproved_and_malformed_base_urls(self):
        with self.assertRaises(Exception):
            build_job_adapter(
                self.source(name="evil", approval_state="approved", enabled=True, base_url="https://evil.example/jobs")
            )
        with self.assertRaises(Exception):
            build_job_adapter(
                self.source(name="malformed", approval_state="approved", enabled=True, base_url="http://jobs.example.test/jobs")
            )
        with self.assertRaises(Exception):
            build_job_adapter(
                self.source(name="port", approval_state="approved", enabled=True, base_url="https://jobs.example.test:443/jobs")
            )

        def unsaved_source(**kwargs):
            values = {
                "name": "Unsaved source",
                "adapter_key": FakeAdapter.key,
                "base_url": "https://jobs.example.test",
                "approval_state": "approved",
                "enabled": True,
                "allowed_hosts": lambda: {"jobs.example.test"},
            }
            values.update(kwargs)
            return SimpleNamespace(**values)

        with self.assertRaises(Exception):
            build_job_adapter(unsaved_source(base_url="https://evil.example/jobs"))
        with self.assertRaises(Exception):
            build_job_adapter(unsaved_source(base_url="http://jobs.example.test/jobs"))

    def test_fetch_contract(self):
        adapter = build_job_adapter(
            self.source(approval_state="approved", enabled=True)
        )
        result = adapter.fetch(JobSourceQuery(keyword="python"))
        self.assertIsInstance(result, JobSourceResult)
        self.assertEqual(result.listings, ())
