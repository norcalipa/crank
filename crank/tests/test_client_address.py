# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Tests for the trusted client address and the gate on its switch (issue #538)."""
import datetime
import importlib
import json
import os
import re
import socket
import sys
import textwrap
import types
from pathlib import Path
from unittest.mock import patch

import yaml
from django.conf import settings
from django.core.cache import cache
from django.http import JsonResponse
from django.test import LiveServerTestCase, RequestFactory, SimpleTestCase, override_settings
from django.urls import path

from crank import client_address as module
from crank.client_address import ClientAddress, client_address, effective_source

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIGMAP_PATH = REPO_ROOT / "k8s" / "crank-configmap.yml"
RECORD_PATH = REPO_ROOT / "docs" / "client-address-verification.yaml"

LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
ENABLED = {"ENV": "prod", "CLIENT_IP_SOURCE": "cloudflare", "CACHES": LOCMEM}
HEADER = "HTTP_CF_CONNECTING_IP"
#: Headers and peer values that must never be read as a client address.
UNTRUSTED = {
    "HTTP_X_FORWARDED_FOR": "203.0.113.50, 203.0.113.51",
    "HTTP_X_REAL_IP": "203.0.113.52",
    "HTTP_TRUE_CLIENT_IP": "203.0.113.53",
    "HTTP_FORWARDED": "for=203.0.113.54",
    "REMOTE_ADDR": "203.0.113.55",
}


def _request(**meta):
    return RequestFactory().get("/", **meta)


class EffectiveSourceTests(SimpleTestCase):
    def test_base_default_is_off(self):
        self.assertEqual(settings.CLIENT_IP_SOURCE, "")
        self.assertEqual(effective_source(), "")

    @override_settings(ENV="prod", CLIENT_IP_SOURCE="cloudflare")
    def test_production_with_the_named_source_is_on(self):
        self.assertEqual(effective_source(), "cloudflare")

    @override_settings(ENV=" PROD ", CLIENT_IP_SOURCE="cloudflare")
    def test_env_is_trimmed_and_case_insensitive(self):
        self.assertEqual(effective_source(), "cloudflare")

    def test_off_outside_production(self):
        for env in ("dev", "staging", "", "production", None):
            with self.subTest(env=env), override_settings(ENV=env, CLIENT_IP_SOURCE="cloudflare"):
                self.assertEqual(effective_source(), "")
                self.assertIsNone(client_address(_request(**{HEADER: "198.51.100.7"})))

    def test_off_for_unrecognised_values(self):
        values = (
            "", "remote_addr", "x-forwarded-for", "HTTP_X_FORWARDED_FOR", "Cloudflare",
            "CLOUDFLARE", " cloudflare ", "cloudflare,x", None, True, 1, ["cloudflare"],
            ("cloudflare",), {"cloudflare": 1},
        )
        for value in values:
            with self.subTest(value=value), override_settings(ENV="prod", CLIENT_IP_SOURCE=value):
                self.assertEqual(effective_source(), "")
                self.assertIsNone(client_address(_request(**{HEADER: "198.51.100.7"})))

    @override_settings(ENV="prod")
    def test_off_when_the_setting_is_absent(self):
        with self.settings():
            del settings.CLIENT_IP_SOURCE
            self.assertEqual(effective_source(), "")

    def test_the_only_source_reads_one_header(self):
        self.assertEqual(module.TRUSTED_HEADERS, {"cloudflare": HEADER})


@override_settings(**ENABLED)
class ClientAddressTests(SimpleTestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

    def resolve(self, value, **meta):
        return client_address(_request(**{HEADER: value}, **meta))

    def test_ipv4(self):
        address = self.resolve("198.51.100.7")
        self.assertIsInstance(address, ClientAddress)
        self.assertEqual(address.bucket, "198.51.100.7")
        self.assertRegex(address.key, r"^[0-9a-f]{32}$")

    def test_surrounding_whitespace_is_trimmed(self):
        self.assertEqual(self.resolve("  198.51.100.7\t"), self.resolve("198.51.100.7"))

    def test_ipv4_mapped_ipv6_becomes_ipv4(self):
        self.assertEqual(self.resolve("::ffff:198.51.100.7"), self.resolve("198.51.100.7"))
        self.assertEqual(self.resolve("::FFFF:c633:6407").bucket, "198.51.100.7")

    def test_ipv6_is_bucketed_to_a_64(self):
        first = self.resolve("2001:db8:1:2::1")
        second = self.resolve("2001:db8:1:2:ffff:ffff:ffff:fffe")
        other = self.resolve("2001:db8:1:3::1")
        self.assertEqual(first.bucket, "2001:db8:1:2::/64")
        self.assertEqual(first, second)
        self.assertEqual(other.bucket, "2001:db8:1:3::/64")
        self.assertNotEqual(first.key, other.key)

    def test_ipv6_forms_normalise(self):
        expected = self.resolve("2001:db8:1:2::1")
        self.assertEqual(self.resolve("2001:DB8:1:2::ABCD"), expected)
        self.assertEqual(self.resolve("2001:0db8:0001:0002:0000:0000:0000:0001"), expected)
        self.assertEqual(self.resolve("2001:db8::5").bucket, "2001:db8::/64")

    def test_different_addresses_have_different_keys(self):
        self.assertNotEqual(self.resolve("198.51.100.7").key, self.resolve("198.51.100.8").key)

    def test_key_is_a_keyed_digest_without_the_address(self):
        for value in ("198.51.100.7", "2001:db8:1:2::1"):
            address = self.resolve(value)
            self.assertNotIn(value, address.key)
            self.assertNotIn(address.bucket, address.key)
        with override_settings(SECRET_KEY="another-secret-key-for-the-538-digest-test"):
            rekeyed = self.resolve("198.51.100.7")
        self.assertNotEqual(rekeyed.key, self.resolve("198.51.100.7").key)

    def test_rejected_forms_give_no_address(self):
        cases = {
            "": "missing",
            "   ": "missing",
            "198.51.100.7, 198.51.100.8": "multiple",
            "198.51.100.7,": "multiple",
            ",": "multiple",
            "fe80::1%eth0": "invalid",
            "198.51.100.7:443": "invalid",
            "[2001:db8::1]": "invalid",
            "[2001:db8::1]:443": "invalid",
            "198.51.100.0/24": "invalid",
            "2001:db8::/64": "invalid",
            "198.51.100": "invalid",
            "198.51.100.07": "invalid",
            "198.51.100.7 198.51.100.8": "invalid",
            "example.invalid": "invalid",
            "0000:0000:0000:0000:0000:ffff:198.51.100.107x": "invalid",
            "0" * 46: "invalid",
            "1" * 10000: "invalid",
        }
        for value, reason in cases.items():
            with self.subTest(value=value[:50]):
                self.assertEqual(module._bucket(value), (None, reason))
                self.assertIsNone(self.resolve(value))

    def test_a_45_character_literal_is_accepted(self):
        value = "2001:0db8:0000:0000:0000:0000:100.100.100.100"
        self.assertEqual(len(value), module.MAX_ADDRESS_LENGTH)
        self.assertEqual(self.resolve(value).bucket, "2001:db8::/64")

    def test_special_purpose_addresses_are_not_identified(self):
        """No visitor has one of these, so none may become a shared bucket."""
        cases = (
            "0.0.0.0", "0.1.2.3", "10.0.0.1", "10.255.255.255", "100.64.0.1",
            "100.127.255.254", "127.0.0.1", "127.255.255.254", "169.254.0.1",
            "172.16.0.1", "172.31.255.254", "192.168.0.1", "192.168.255.254",
            "224.0.0.1", "239.255.255.250", "240.0.0.1", "255.255.255.255",
            "::", "::1", "::2", "::10.0.0.1", "::198.51.100.7",
            "::ffff:127.0.0.1", "::ffff:10.0.0.1", "::ffff:192.168.1.1",
            "::ffff:0.0.0.0", "0:0:0:0:0:ffff:a9fe:1", "fc00::1", "fd12:3456::1",
            "fdff:ffff:ffff:ffff::1", "fe80::1", "febf::1", "ff02::1",
        )
        for value in cases:
            with self.subTest(value=value):
                self.assertEqual(module._bucket(value), (None, "special"))
                self.assertIsNone(self.resolve(value))

    def test_addresses_next_to_special_ranges_are_identified(self):
        cases = {
            "1.0.0.0": "1.0.0.0",
            "9.255.255.255": "9.255.255.255",
            "11.0.0.0": "11.0.0.0",
            "100.63.255.255": "100.63.255.255",
            "100.128.0.0": "100.128.0.0",
            "126.255.255.255": "126.255.255.255",
            "128.0.0.0": "128.0.0.0",
            "169.253.255.255": "169.253.255.255",
            "169.255.0.0": "169.255.0.0",
            "172.15.255.255": "172.15.255.255",
            "172.32.0.0": "172.32.0.0",
            "192.167.255.255": "192.167.255.255",
            "192.169.0.0": "192.169.0.0",
            "223.255.255.255": "223.255.255.255",
            "100::1": "100::/64",
            "fbff::1": "fbff::/64",
            "fe00::1": "fe00::/64",
            "fe7f::1": "fe7f::/64",
            "fec0::1": "fec0::/64",
            "feff::1": "feff::/64",
            # Documentation ranges stay usable in tests and captures.
            "192.0.2.1": "192.0.2.1",
            "203.0.113.9": "203.0.113.9",
            "2001:db8::1": "2001:db8::/64",
        }
        for value, bucket in cases.items():
            with self.subTest(value=value):
                self.assertEqual(module._bucket(value), (bucket, None))

    def test_special_purpose_networks_are_the_documented_list(self):
        self.assertEqual(
            [str(network) for network in module.SPECIAL_PURPOSE_NETWORKS],
            [
                "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8",
                "169.254.0.0/16", "172.16.0.0/12", "192.168.0.0/16", "224.0.0.0/4",
                "240.0.0.0/4", "::/8", "fc00::/7", "fe80::/10", "ff00::/8",
            ],
        )

    def test_non_string_values_are_missing(self):
        for value in (None, 5, b"198.51.100.7", ["198.51.100.7"]):
            with self.subTest(value=value):
                self.assertEqual(module._bucket(value), (None, "missing"))

    def test_other_headers_and_the_peer_change_nothing(self):
        expected = self.resolve("198.51.100.7")
        self.assertEqual(self.resolve("198.51.100.7", **UNTRUSTED), expected)
        for name, value in UNTRUSTED.items():
            with self.subTest(name=name):
                self.assertEqual(self.resolve("198.51.100.7", **{name: value}), expected)

    def test_other_headers_alone_give_no_address(self):
        self.assertIsNone(client_address(_request(**UNTRUSTED)))
        for name, value in UNTRUSTED.items():
            with self.subTest(name=name):
                self.assertIsNone(client_address(_request(**{name: "198.51.100.7"})))

    def test_warning_names_the_reason_and_never_the_value(self):
        cases = (
            ({}, "missing"),
            ({HEADER: "198.51.100.7, 198.51.100.8"}, "multiple"),
            ({HEADER: "not-an-address-538"}, "invalid"),
            ({HEADER: "127.0.0.1"}, "special"),
        )
        for meta, reason in cases:
            cache.clear()
            with self.subTest(reason=reason):
                with self.assertLogs("crank.client_address", level="WARNING") as logs:
                    self.assertIsNone(client_address(_request(**meta)))
                self.assertEqual(len(logs.records), 1)
                message = logs.records[0].getMessage()
                self.assertEqual(
                    message, f"client address unavailable: source=cloudflare reason={reason}"
                )
                for value in meta.values():
                    self.assertNotIn(value, message)
                    self.assertNotIn(value, repr(logs.records[0].args))

    def test_warning_is_not_repeated_inside_the_window(self):
        with self.assertLogs("crank.client_address", level="WARNING") as logs:
            client_address(_request())
        self.assertEqual(len(logs.records), 1)
        self.assertEqual(cache.get(module.WARNING_CACHE_KEY), 1)
        with self.assertNoLogs("crank.client_address", level="WARNING"):
            client_address(_request())
            client_address(_request(**{HEADER: "nope"}))
        cache.delete(module.WARNING_CACHE_KEY)
        with self.assertLogs("crank.client_address", level="WARNING"):
            client_address(_request())

    def test_warning_window_is_ten_minutes(self):
        with patch.object(cache, "add", return_value=False) as add:
            client_address(_request())
        add.assert_called_once_with("client-address-unavailable-warning", 1, 600)

    def test_an_identified_address_logs_and_caches_nothing(self):
        with self.assertNoLogs("crank.client_address", level="WARNING"):
            self.resolve("198.51.100.7")
        self.assertIsNone(cache.get(module.WARNING_CACHE_KEY))

    @override_settings(CLIENT_IP_SOURCE="")
    def test_off_reads_no_header_and_writes_nothing(self):
        request = _request(**{HEADER: "198.51.100.7"}, **UNTRUSTED)
        with patch.object(module, "_bucket") as bucket:
            self.assertIsNone(client_address(request))
        bucket.assert_not_called()
        self.assertEqual(cache._cache, {})


class SettingsModuleTests(SimpleTestCase):
    """Only the production settings module reads the switch from the environment."""

    def _reload_prod(self, environ):
        pool = types.ModuleType("dj_db_conn_pool")
        backends = types.ModuleType("dj_db_conn_pool.backends")
        mysql = types.ModuleType("dj_db_conn_pool.backends.mysql")
        pool.backends = backends
        backends.mysql = mysql
        stubs = {
            "dj_db_conn_pool": pool,
            "dj_db_conn_pool.backends": backends,
            "dj_db_conn_pool.backends.mysql": mysql,
        }
        with patch.dict(sys.modules, stubs), patch.dict(os.environ):
            os.environ.pop("CLIENT_IP_SOURCE", None)
            os.environ.update(environ)
            prod = importlib.import_module("crank.settings.prod")
            return importlib.reload(prod)

    def test_prod_reads_the_environment(self):
        self.addCleanup(self._reload_prod, {})
        self.assertEqual(self._reload_prod({}).CLIENT_IP_SOURCE, "")
        self.assertEqual(
            self._reload_prod({"CLIENT_IP_SOURCE": "cloudflare"}).CLIENT_IP_SOURCE, "cloudflare"
        )
        self.assertEqual(
            self._reload_prod({"CLIENT_IP_SOURCE": "  Cloudflare\n"}).CLIENT_IP_SOURCE,
            "cloudflare",
        )
        # A header name is not a source: it is carried as typed and then ignored.
        prod = self._reload_prod({"CLIENT_IP_SOURCE": "X-Forwarded-For"})
        self.assertEqual(prod.CLIENT_IP_SOURCE, "x-forwarded-for")
        with override_settings(ENV=prod.ENV, CLIENT_IP_SOURCE=prod.CLIENT_IP_SOURCE):
            self.assertEqual(effective_source(), "")

    def test_base_ignores_the_environment_and_other_modules_do_not_define_it(self):
        settings_dir = REPO_ROOT / "crank" / "settings"
        base = (settings_dir / "base.py").read_text(encoding="utf-8")
        self.assertIn('\nCLIENT_IP_SOURCE = ""\n', base)
        for name in sorted(p.name for p in settings_dir.glob("*.py")):
            source = (settings_dir / name).read_text(encoding="utf-8")
            reads_env = bool(re.search(r"environ[^\n]*CLIENT_IP_SOURCE", source))
            with self.subTest(module=name):
                self.assertEqual(reads_env, name == "prod.py")
                if name not in ("base.py", "prod.py"):
                    self.assertNotIn("CLIENT_IP_SOURCE", source)

    def test_declared_limits(self):
        self.assertEqual(settings.COMPANY_CORRECTION_RATE_LIMIT_PER_HOUR, 10)
        self.assertEqual(settings.COMPANY_CORRECTION_REJECTED_LIMIT_PER_HOUR, 60)
        self.assertEqual(settings.COMPANY_CORRECTION_IP_RATE_LIMIT_PER_HOUR, 20)
        self.assertEqual(settings.COMPANY_CORRECTION_IP_REJECTED_LIMIT_PER_HOUR, 120)


# --- The gate on the switch (acceptance criterion 17) ------------------------

SWITCH_KEY = "CLIENT_IP_SOURCE"
ALLOWED_SWITCH_VALUES = ("", "cloudflare")
RECORD_FIELDS = {
    "date": None,
    "verified_by": None,
    "g1_public_path": ("pass", "fail"),
    "g2_private_path": ("pass", "waived", "fail"),
    "g2_method": ("nodeport-addresses", "clusterip", "firewall", "waived"),
    "edge_header_checks": ("pass", "fail"),
}
# A name or handle only: no dots or colons, so no address or hostname fits.
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _'-]{0,63}$")


def _record_date(value):
    if isinstance(value, datetime.datetime):
        return None
    if isinstance(value, datetime.date):
        return value
    if isinstance(value, str):
        try:
            return datetime.date.fromisoformat(value)
        except ValueError:
            return None
    return None


def record_problems(record):
    """Why one verification record is malformed (it may still record a failure)."""
    if not isinstance(record, dict):
        return ["a record must be a mapping"]
    problems = []
    for name in sorted(set(record) | set(RECORD_FIELDS)):
        if name not in RECORD_FIELDS:
            problems.append(f"unexpected field {name!r}: records hold dates and pass/fail values only")
        elif name not in record:
            problems.append(f"missing field {name!r}")
        elif RECORD_FIELDS[name] and record[name] not in RECORD_FIELDS[name]:
            problems.append(f"{name} must be one of {', '.join(RECORD_FIELDS[name])}")
    if "date" in record and _record_date(record["date"]) is None:
        problems.append("date must be an ISO date")
    name = record.get("verified_by")
    if "verified_by" in record and not (isinstance(name, str) and _NAME.match(name)):
        problems.append("verified_by must be a short name")
    if (record.get("g2_private_path") == "waived") != (record.get("g2_method") == "waived"):
        problems.append("g2_method is 'waived' exactly when g2_private_path is")
    return problems


def switch_problems(configmap, document, today):
    """Why the checked-in switch and verification records may not merge."""
    data = configmap.get("data") if isinstance(configmap, dict) else None
    if not isinstance(data, dict):
        return ["the ConfigMap has no data mapping"]
    records = document.get("verifications") if isinstance(document, dict) else None
    if not isinstance(records, list):
        return ["verifications must be a list"]
    problems = [problem for record in records for problem in record_problems(record)]
    value = data.get(SWITCH_KEY, "")
    if value not in ALLOWED_SWITCH_VALUES:
        problems.append(f"{SWITCH_KEY} must be empty or 'cloudflare'")
    if value != "cloudflare" or problems:
        return problems
    if not records:
        return [f"{SWITCH_KEY} is on without a verification record"]
    last = records[-1]
    if last["g1_public_path"] != "pass":
        problems.append("the last record does not pass g1_public_path")
    if last["g2_private_path"] not in ("pass", "waived"):
        problems.append("the last record does not pass or waive g2_private_path")
    if last["edge_header_checks"] != "pass":
        problems.append("the last record does not pass edge_header_checks")
    if _record_date(last["date"]) > today:
        problems.append("the last record is dated in the future")
    return problems


class SwitchGateTests(SimpleTestCase):
    today = datetime.date(2026, 10, 12)

    def configmap(self, **data):
        return {"kind": "ConfigMap", "data": {"ENV": "prod", **data}}

    def record(self, **overrides):
        record = {
            "date": "2026-10-12",
            "verified_by": "Operator Name",
            "g1_public_path": "pass",
            "g2_private_path": "pass",
            "g2_method": "nodeport-addresses",
            "edge_header_checks": "pass",
        }
        record.update(overrides)
        return {key: value for key, value in record.items() if value is not ...}

    def problems(self, configmap, *records):
        return switch_problems(configmap, {"verifications": list(records)}, self.today)

    def test_checked_in_files_pass(self):
        configmap = yaml.safe_load(CONFIGMAP_PATH.read_text(encoding="utf-8"))
        document = yaml.safe_load(RECORD_PATH.read_text(encoding="utf-8"))
        self.assertEqual(configmap["kind"], "ConfigMap")
        self.assertEqual(
            switch_problems(configmap, document, datetime.date.today()), []
        )

    def test_538a_leaves_the_switch_off_with_no_records(self):
        configmap = yaml.safe_load(CONFIGMAP_PATH.read_text(encoding="utf-8"))
        document = yaml.safe_load(RECORD_PATH.read_text(encoding="utf-8"))
        self.assertNotIn(SWITCH_KEY, configmap["data"])
        self.assertEqual(document, {"verifications": []})

    def test_off_needs_no_record(self):
        self.assertEqual(self.problems(self.configmap()), [])
        self.assertEqual(self.problems(self.configmap(CLIENT_IP_SOURCE="")), [])
        failed = self.record(g1_public_path="fail", g2_private_path="fail")
        self.assertEqual(self.problems(self.configmap(), failed), [])

    def test_unknown_switch_values_are_refused(self):
        for value in ("Cloudflare", " cloudflare", "x-forwarded-for", "remote_addr", "true", True, 1):
            with self.subTest(value=value):
                self.assertEqual(
                    self.problems(self.configmap(CLIENT_IP_SOURCE=value), self.record()),
                    ["CLIENT_IP_SOURCE must be empty or 'cloudflare'"],
                )

    def test_on_with_a_passing_record(self):
        on = self.configmap(CLIENT_IP_SOURCE="cloudflare")
        self.assertEqual(self.problems(on, self.record()), [])
        self.assertEqual(self.problems(on, self.record(date=datetime.date(2026, 10, 1))), [])
        waived = self.record(g2_private_path="waived", g2_method="waived")
        self.assertEqual(self.problems(on, waived), [])
        for method in ("clusterip", "firewall"):
            self.assertEqual(self.problems(on, self.record(g2_method=method)), [])
        earlier_failure = self.record(date="2026-10-11", g1_public_path="fail")
        self.assertEqual(self.problems(on, earlier_failure, self.record()), [])

    def test_on_without_a_passing_last_record_is_refused(self):
        on = self.configmap(CLIENT_IP_SOURCE="cloudflare")
        self.assertEqual(
            self.problems(on), ["CLIENT_IP_SOURCE is on without a verification record"]
        )
        cases = {
            "g1_public_path": self.record(g1_public_path="fail"),
            "g2_private_path": self.record(g2_private_path="fail"),
            "edge_header_checks": self.record(edge_header_checks="fail"),
            "future": self.record(date="2026-10-13"),
        }
        for name, record in cases.items():
            with self.subTest(name=name):
                problems = self.problems(on, record)
                self.assertEqual(len(problems), 1)
                self.assertIn(name, problems[0])
        # Only the last record counts: an older pass does not carry a later failure.
        self.assertEqual(
            len(self.problems(on, self.record(), self.record(g1_public_path="fail"))), 1
        )
        everything = self.record(
            g1_public_path="fail", g2_private_path="fail", edge_header_checks="fail",
            date="2027-01-01",
        )
        self.assertEqual(len(self.problems(on, everything)), 4)

    def test_malformed_records_are_refused_whatever_the_switch(self):
        cases = {
            "must be a mapping": "not a record",
            "missing": self.record(verified_by=...),
            "unexpected": self.record(node_address="198.51.100.7"),
            "g1_public_path must": self.record(g1_public_path="waived"),
            "g2_private_path must": self.record(g2_private_path="yes"),
            "g2_method must": self.record(g2_method="ufw"),
            "edge_header_checks must": self.record(edge_header_checks=True),
            "ISO date": self.record(date="12 October 2026"),
            "date must": self.record(date=20261012),
            "date must be": self.record(date=datetime.datetime(2026, 10, 12, 9, 0)),
            "short name": self.record(verified_by=""),
            "verified_by must": self.record(verified_by="198.51.100.7"),
            "verified_by must be": self.record(verified_by="host.example"),
            "verified_by must be a": self.record(verified_by=7),
            "exactly when": self.record(g2_private_path="waived"),
            "'waived' exactly": self.record(g2_method="waived"),
        }
        for configmap in (self.configmap(), self.configmap(CLIENT_IP_SOURCE="cloudflare")):
            for expected, record in cases.items():
                with self.subTest(expected=expected):
                    problems = self.problems(configmap, record)
                    self.assertEqual(len(problems), 1, problems)
                    self.assertIn(expected, problems[0])

    def test_malformed_documents_are_refused(self):
        on = self.configmap(CLIENT_IP_SOURCE="cloudflare")
        for document in (None, [], {}, {"verifications": None}, {"verifications": {}}):
            with self.subTest(document=document):
                self.assertEqual(
                    switch_problems(on, document, self.today), ["verifications must be a list"]
                )
        for configmap in (None, [], {}, {"data": None}, {"data": []}):
            with self.subTest(configmap=configmap):
                self.assertEqual(
                    switch_problems(configmap, {"verifications": []}, self.today),
                    ["the ConfigMap has no data mapping"],
                )

    def test_the_runbook_example_record_is_well_formed(self):
        runbook = (REPO_ROOT / "docs" / "runbook-client-address.md").read_text(encoding="utf-8")
        match = re.search(r"```yaml\n( *verifications:\n.*?)```", runbook, re.DOTALL)
        self.assertIsNotNone(match)
        document = yaml.safe_load(textwrap.dedent(match.group(1)))
        self.assertEqual(len(document["verifications"]), 1)
        self.assertEqual(record_problems(document["verifications"][0]), [])


# --- Through the production server class (acceptance criterion 16) -----------


def _echo(request):
    address = client_address(request)
    return JsonResponse({"bucket": address.bucket if address else None})


urlpatterns = [path("client-address-echo/", _echo)]


@override_settings(ROOT_URLCONF=__name__, **ENABLED)
class UnderscoreTwinTests(LiveServerTestCase):
    """Django's WSGI request handler is the class production serves with."""

    def bucket(self, *headers):
        lines = ["GET /client-address-echo/ HTTP/1.1", f"Host: {self.server_thread.host}"]
        lines += [f"{name}: {value}" for name, value in headers]
        lines += ["Connection: close", "", ""]
        with socket.create_connection(
            (self.server_thread.host, self.server_thread.port), timeout=10
        ) as connection:
            connection.sendall("\r\n".join(lines).encode("ascii"))
            chunks = []
            while chunk := connection.recv(65536):
                chunks.append(chunk)
        head, _, body = b"".join(chunks).partition(b"\r\n\r\n")
        self.assertIn(b" 200 ", head.split(b"\r\n", 1)[0])
        return json.loads(body)["bucket"]

    def test_underscore_twin_and_duplicates(self):
        real = ("CF-Connecting-IP", "198.51.100.7")
        twin = ("CF_Connecting_IP", "203.0.113.9")
        cache.clear()
        self.addCleanup(cache.clear)
        self.assertEqual(self.bucket(real), "198.51.100.7")
        self.assertEqual(self.bucket(real, twin), "198.51.100.7")
        self.assertEqual(self.bucket(twin, real), "198.51.100.7")
        self.assertEqual(self.bucket(("cf-connecting-ip", "198.51.100.7")), "198.51.100.7")
        self.assertIsNone(self.bucket(twin))
        self.assertIsNone(self.bucket())
        self.assertIsNone(self.bucket(real, ("CF-Connecting-IP", "198.51.100.8")))
        self.assertIsNone(self.bucket(real, real))
        self.assertIsNone(self.bucket(("X-Forwarded-For", "198.51.100.7")))
