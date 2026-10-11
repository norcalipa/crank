# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Tests for the trusted client address and the gate on its switch (issue #538)."""
import datetime
import fnmatch
import functools
import importlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
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
CONFIGMAP_FILE = "k8s/crank-configmap.yml"
RECORD_FILE = "docs/client-address-verification.yaml"
CONFIGMAP_PATH = REPO_ROOT / CONFIGMAP_FILE
RECORD_PATH = REPO_ROOT / RECORD_FILE

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


# Everything a merge can change that reaches a container's environment.
SWITCH_CONFIGMAP = "crank-config"
MANIFEST_SUFFIXES = (".yml", ".yaml", ".json")
# The application code that reads the switch, its tests and its documents.
# Every other tracked file is refused if it names the switch at all.
SWITCH_READERS = frozenset(
    {
        "crank/checks.py",
        "crank/client_address.py",
        "crank/release.py",
        "crank/settings/base.py",
        "crank/settings/prod.py",
    }
)
SWITCH_READER_DIRS = ("crank/tests/", "docs/")
# Test data that is malformed on purpose. A file that cannot be parsed
# cannot be applied either, so here (and only here) it is not a problem.
MALFORMED_FIXTURE_DIR = "crank/tests/fixtures/"
# ``load_dotenv()`` reads a file with exactly this name, from the directory
# of ``manage.py`` upwards. It must never reach the image.
DOTENV_NAME = ".env"
DOCKERIGNORE_FILE = ".dockerignore"
DOCKERIGNORE_RULES = (".env", "**/.env")
# Tracked files named like an env file that the gate knowingly leaves unread.
# ``.env-prod`` reaches the image build as build arguments only, never as a
# file ``load_dotenv()`` looks for.
UNREAD_ENV_FILES = frozenset({".env-prod"})
# Secrets nothing in the repository creates or changes: the operator makes
# them in the cluster. A merge cannot put a key into one, so the gate accepts
# them by name only; any other Secret a container loads must be accounted for.
OUT_OF_BAND_SECRETS = frozenset({"db-connect-credentials"})
_CONTAINER_LISTS = ("containers", "initContainers", "ephemeralContainers")
_SECRET_COMMAND = re.compile(r"create\s+secret\s+generic\s+([A-Za-z0-9.-]+)((?:\\\n|[^\n|])*)")
_SECRET_LITERAL = re.compile(r"--from-literal=([A-Za-z0-9_.-]+)=")
_SECRET_FILE_NAME = re.compile(r"secret\S*\.ya?ml", re.IGNORECASE)
# A command continued on the next line: a trailing backslash or pipe.
_CONTINUATION = re.compile(r"(?:\\|\|)[ \t]*\n[ \t]*")


def is_secret_file(path):
    """Files that may hold secret values: the gate never opens them."""
    name = Path(path).name.lower()
    return "secret" in name or name.startswith(".env")


def may_name_switch(name):
    return name in SWITCH_READERS or name.startswith(SWITCH_READER_DIRS)


def tracked_files(root=REPO_ROOT):
    """Every path git tracks under ``root``; names only, nothing is opened."""
    listing = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z"], check=True, capture_output=True
    )
    return sorted(name for name in listing.stdout.decode("utf-8").split("\0") if name)


def read_deployment_sources(root=REPO_ROOT, tracked=None):
    """``(manifests, texts)`` by repository path, skipping secrets files unread.

    The whole tracked tree is walked, not a list of directories. Manifests are
    every YAML or JSON file wherever it lives; texts are every other file a
    merge could use to set the switch (workflows, composite actions, image
    files, scripts, anything else that is text). The few files that read or
    document the switch are left out of the texts.
    """
    root = Path(root)
    manifests, texts = {}, {}
    for name in tracked_files(root) if tracked is None else tracked:
        path = root / name
        if is_secret_file(path) or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue  # an image or a font: nothing a deployment reads as text
        if path.suffix.lower() in MANIFEST_SUFFIXES and not fnmatch.fnmatch(
            path.name, "docker-compose*"
        ):
            manifests[name] = text
            if not name.startswith(".github/"):
                continue
        if not may_name_switch(name):
            texts[name] = text
    return manifests, texts


def dotenv_problems(root=REPO_ROOT, tracked=None):
    """Why an env file could reach the image, from file names alone.

    ``manage.py`` and the settings call ``load_dotenv()`` and the image is
    built from the whole checkout, so a tracked ``.env`` would set any
    variable the ConfigMaps do not. No env file is opened to decide this.
    """
    problems = []
    for name in tracked_files(root) if tracked is None else tracked:
        base = Path(name).name
        if base == DOTENV_NAME:
            problems.append(f"{name} is tracked: load_dotenv() would read it in the image")
        elif base.lower().startswith(DOTENV_NAME) and name not in UNREAD_ENV_FILES:
            problems.append(f"{name} is a tracked env file this gate does not read")
    try:
        ignore = (Path(root) / DOCKERIGNORE_FILE).read_text(encoding="utf-8")
    except OSError:
        ignore = ""
    rules = [line.strip() for line in ignore.splitlines()]
    for rule in DOCKERIGNORE_RULES:
        if rule not in rules:
            problems.append(f"{DOCKERIGNORE_FILE} must keep {rule} out of the image")
    for rule in rules:
        if rule.startswith("!") and DOTENV_NAME in rule:
            problems.append(f"{DOCKERIGNORE_FILE} lets an env file back into the image: {rule}")
    return problems


def _containers(node):
    """Every container definition anywhere inside a manifest document."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key in _CONTAINER_LISTS and isinstance(value, list):
                yield from (item for item in value if isinstance(item, dict))
            yield from _containers(value)
    elif isinstance(node, list):
        for item in node:
            yield from _containers(item)


def _resources(document):
    """A manifest document and, for a ``kind: List`` wrapper, what it wraps."""
    if not isinstance(document, dict):
        return
    yield document
    items = document.get("items")
    if str(document.get("kind", "")).endswith("List") and isinstance(items, list):
        for item in items:
            yield from _resources(item)


@functools.lru_cache(maxsize=None)
def _documents(text):
    """The parsed documents of one manifest, or None when it cannot be parsed."""
    try:
        return (json.loads(text),)
    except ValueError:
        pass
    try:
        return tuple(yaml.safe_load_all(text))
    except yaml.YAMLError:
        return None


def _logical_lines(text):
    """Command lines with comments dropped and continuation lines joined."""
    kept = "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )
    return _CONTINUATION.sub(" ", kept).splitlines()


@functools.lru_cache(maxsize=None)
def _mentions_switch(text):
    return SWITCH_KEY in text and any(
        SWITCH_KEY in line for line in text.splitlines() if not line.lstrip().startswith("#")
    )


@functools.lru_cache(maxsize=None)
def _applies_secret_file(text):
    """A command that hands a secrets file to ``kubectl apply``, piped or named."""
    return "kubectl" in text and any(
        _SECRET_FILE_NAME.search(line) and "kubectl" in line and "apply" in line
        for line in _logical_lines(text)
    )


@functools.lru_cache(maxsize=None)
def _secret_commands(text):
    return tuple(_SECRET_COMMAND.findall(text)) if "generic" in text else ()


def workflow_secret_keys(texts):
    """``{secret name: keys}`` for Secrets a workflow creates from literals.

    A Secret created from a file or an env file maps to None: its keys cannot
    be read from the workflow text.
    """
    found = {}
    for text in texts.values():
        for name, arguments in _secret_commands(text):
            if "--from-file" in arguments or "--from-env-file" in arguments:
                found[name] = None
            elif found.get(name, set()) is not None:
                found.setdefault(name, set()).update(_SECRET_LITERAL.findall(arguments))
    return found


def deployment_problems(manifests, texts):
    """``(crank-config, problems)``: every other way the switch could arrive.

    The switch may be set in exactly one place, the ``data`` of the ConfigMap
    ``crank-config``, which is what ``switch_problems`` then holds to the
    verification record. Anything else that could put it into a container's
    environment is a problem whatever the record says, and so is any source
    of environment this gate cannot read.
    """
    problems = []
    configmaps = {}
    containers = []
    secrets_defined = {}
    deployments = 0
    for name, text in sorted(manifests.items()):
        documents = _documents(text)
        if documents is None and name.startswith(MALFORMED_FIXTURE_DIR):
            continue
        if documents is None:
            problems.append(f"{name} cannot be parsed, so it cannot be checked for {SWITCH_KEY}")
            continue
        for document in (item for top in documents for item in _resources(top)):
            kind = document.get("kind")
            metadata = document.get("metadata")
            title = metadata.get("name") if isinstance(metadata, dict) else None
            if kind == "ConfigMap":
                data = document.get("data")
                configmaps.setdefault(title, []).append(
                    (name, document, data if isinstance(data, dict) else {})
                )
            elif kind == "Secret":
                keys = set()
                for field in ("data", "stringData"):
                    if isinstance(document.get(field), dict):
                        keys.update(document[field])
                secrets_defined.setdefault(title, set()).update(keys)
            deployments += kind == "Deployment"
            if str(kind).endswith("List"):
                continue  # its items are visited on their own
            for container in _containers(document):
                containers.append((f"{name} {kind}/{title}", container))
    if not deployments:
        problems.append("no Deployment was found, so nothing was checked")
    for title, entries in sorted(configmaps.items(), key=lambda item: str(item[0])):
        for name, _document, data in entries:
            if title != SWITCH_CONFIGMAP and SWITCH_KEY in data:
                problems.append(
                    f"{SWITCH_KEY} is set in ConfigMap {title} ({name}): "
                    f"it may only be set in {SWITCH_CONFIGMAP}"
                )
    canonical = configmaps.get(SWITCH_CONFIGMAP, [])
    if len(canonical) != 1:
        problems.append(
            f"ConfigMap {SWITCH_CONFIGMAP} must be defined exactly once "
            f"(found {len(canonical)})"
        )

    secret_names = set()
    for where, container in containers:
        env = container.get("env")
        for entry in env if isinstance(env, list) else []:
            if isinstance(entry, dict) and entry.get("name") == SWITCH_KEY:
                problems.append(
                    f"{SWITCH_KEY} is a container env entry in {where}: "
                    f"it may only be set in ConfigMap {SWITCH_CONFIGMAP}"
                )
        sources = container.get("envFrom")
        for source in sources if isinstance(sources, list) else []:
            source = source if isinstance(source, dict) else {}
            config_ref, secret_ref = source.get("configMapRef"), source.get("secretRef")
            if isinstance(config_ref, dict) and set(source) == {"configMapRef"}:
                if config_ref.get("name") not in configmaps:
                    problems.append(
                        f"{where} loads ConfigMap {config_ref.get('name')}, "
                        "which this gate cannot read"
                    )
            elif isinstance(secret_ref, dict) and set(source) == {"secretRef"}:
                secret_names.add((secret_ref.get("name"), where))
            else:
                problems.append(f"{where} has an envFrom source this gate cannot read")

    created = workflow_secret_keys(texts)
    for title, where in sorted(secret_names, key=str):
        keys = set(secrets_defined.get(title, ()))
        if title in created:
            if created[title] is None:
                problems.append(
                    f"Secret {title} ({where}) is created from a file this gate does not read"
                )
                continue
            keys |= created[title]
        elif title not in secrets_defined and title not in OUT_OF_BAND_SECRETS:
            problems.append(
                f"{where} loads Secret {title}, and this gate cannot show that it "
                f"does not carry {SWITCH_KEY}"
            )
        if SWITCH_KEY in keys:
            problems.append(f"{SWITCH_KEY} is a key of Secret {title}: it is not a secret")

    canonical_files = {name for name, _document, _data in canonical}
    for name, text in sorted({**manifests, **texts}.items()):
        if (
            name not in canonical_files
            and not may_name_switch(name)
            and _mentions_switch(text)
        ):
            problems.append(
                f"{name} mentions {SWITCH_KEY}: it may only be set in ConfigMap "
                f"{SWITCH_CONFIGMAP}"
            )
        if _applies_secret_file(text):
            problems.append(
                f"{name} applies a secrets file, which this gate does not read"
            )
    return (canonical[0][1] if len(canonical) == 1 else None), problems


def gate_problems(manifests, texts, document, today):
    """Why the checked-in deployment and verification records may not merge."""
    configmap, problems = deployment_problems(manifests, texts)
    return problems + switch_problems(configmap, document, today)


def repository_problems(root=REPO_ROOT, today=None, tracked=None):
    """The whole gate over one checkout: the single rule the repository obeys.

    The switch is on only if the last record passes; otherwise it must be
    off, and nothing but ``crank-config`` may set it. The rule reads the
    state it finds, so it holds before and after the switch is enabled.
    """
    tracked = tracked_files(root) if tracked is None else tracked
    manifests, texts = read_deployment_sources(root, tracked)
    try:
        document = yaml.safe_load((Path(root) / RECORD_FILE).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        document = None
    return dotenv_problems(root, tracked) + gate_problems(
        manifests, texts, document, today or datetime.date.today()
    )


def switch_off(documents):
    """Take the switch out of every ConfigMap in one manifest's documents."""
    for document in documents:
        data = document.get("data") if isinstance(document, dict) else None
        if isinstance(data, dict):
            data.pop(SWITCH_KEY, None)


def switch_on(documents):
    documents[0]["data"][SWITCH_KEY] = "cloudflare"


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


class DeploymentGateTests(SimpleTestCase):
    """The switch is refused wherever else a merge could put it.

    Each case edits an in-memory copy of the checked-in manifests and
    workflows, so the tests follow the real deployment rather than a list of
    file names. The copy always starts with the switch off, whatever is
    checked in: these cases must not change when the switch is enabled.
    """

    today = datetime.date(2026, 10, 12)
    agent_config = "k8s/crank-agent-config.yml"
    web = "k8s/crank.yml"
    cronjob = "deploy/cronjob-job-pipeline.yaml"
    workflow = ".github/workflows/deploy-home.yml"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.checked_in = read_deployment_sources()

    def setUp(self):
        self.manifests, self.texts = (dict(group) for group in self.checked_in)
        self.edit(CONFIGMAP_FILE, switch_off)
        # The record is passed to each case explicitly, never read from disk.
        del self.manifests[RECORD_FILE]

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
        return record

    def test_every_case_starts_from_off_whatever_is_checked_in(self):
        for text in (
            'kind: ConfigMap\nmetadata: {name: crank-config}\ndata: {ENV: prod}\n',
            'kind: ConfigMap\nmetadata: {name: crank-config}\n'
            'data: {ENV: prod, CLIENT_IP_SOURCE: "cloudflare"}\n',
        ):
            with self.subTest(on=SWITCH_KEY in text):
                self.manifests[CONFIGMAP_FILE] = text
                self.edit(CONFIGMAP_FILE, switch_off)
                self.assertNotIn(SWITCH_KEY, self.manifests[CONFIGMAP_FILE])
                self.assertEqual(self.problems(), [])
        documents = ["text", {"kind": "ConfigMap", "data": None}]
        switch_off(documents)
        self.assertEqual(documents, ["text", {"kind": "ConfigMap", "data": None}])

    def problems(self, *records):
        return gate_problems(
            self.manifests, self.texts, {"verifications": list(records)}, self.today
        )

    def edit(self, name, change):
        """Apply ``change`` to the parsed documents of one manifest."""
        documents = list(yaml.safe_load_all(self.manifests[name]))
        change(documents)
        self.manifests[name] = yaml.safe_dump_all(documents)

    def first_container(self, documents, kind):
        document = next(d for d in documents if isinstance(d, dict) and d.get("kind") == kind)
        return next(_containers(document))

    def assert_refused(self, expected):
        """Refused with no record, a failed record and even a passing one."""
        failed = self.record(g1_public_path="fail", g2_private_path="fail")
        for records in ((), (failed,), (self.record(),)):
            with self.subTest(records=len(records), failed=records == (failed,)):
                problems = self.problems(*records)
                self.assertTrue(
                    any(expected in problem for problem in problems), problems
                )

    def test_checked_in_deployment_passes(self):
        document = yaml.safe_load(RECORD_PATH.read_text(encoding="utf-8"))
        manifests, texts = self.checked_in
        self.assertEqual(gate_problems(manifests, texts, document, datetime.date.today()), [])
        self.assertEqual(self.problems(), [])

    def test_the_scan_covers_the_real_deployment(self):
        manifests, texts = self.checked_in
        self.assertIn("k8s/crank-configmap.yml", manifests)
        self.assertIn(self.agent_config, manifests)
        self.assertIn(self.web, manifests)
        self.assertIn(self.cronjob, manifests)
        self.assertIn(self.workflow, texts)
        self.assertIn(".github/workflows/update-home-deployment.yml", texts)
        self.assertIn("Dockerfile", texts)
        self.assertIn("Base.Dockerfile", texts)
        # .env-prod is passed to the build as arguments only: with no ARG in
        # an image file, none of them can become environment.
        for name in ("Dockerfile", "Base.Dockerfile"):
            self.assertNotRegex(texts[name], r"(?im)^\s*ARG\b", name)
        self.assertIn("docker-compose.yml", texts)
        self.assertNotIn("docker-compose.yml", manifests)
        self.assertIn(self.workflow, manifests)
        self.assertIn("run_test_with_timeout.sh", texts)
        self.assertIn("manage.py", texts)
        self.assertIn(RECORD_FILE, manifests)
        configmap, problems = deployment_problems(manifests, texts)
        self.assertEqual(problems, [])
        self.assertEqual(
            configmap, yaml.safe_load(CONFIGMAP_PATH.read_text(encoding="utf-8"))
        )
        # The web container's environment sources are all accounted for.
        web = list(yaml.safe_load_all(manifests[self.web]))
        sources = self.first_container(web, "Deployment")["envFrom"]
        self.assertEqual(
            [next(iter(source.values()))["name"] for source in sources],
            [
                "crank-config",
                "crank-agent-config",
                "db-connect-credentials",
                "crank-capability-secrets",
            ],
        )
        self.assertEqual(
            workflow_secret_keys(texts)["crank-capability-secrets"],
            {
                "LLM_API_KEY",
                "YELP_API_KEY",
                "USAJOBS_AUTH_KEY",
                "USAJOBS_USER_AGENT_EMAIL",
                "FIRECRAWL_API_KEY",
            },
        )

    def test_secrets_files_are_never_opened(self):
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            names = (
                "k8s/app.yml",
                "k8s/app-secrets.yml",
                "k8s/nested/Secret-values.yaml",
                "deploy/.env",
                "deploy/.env.production",
                ".env",
                ".env-prod",
                "ops/anywhere.json",
                "ops/my_secrets.json",
                ".github/workflows/deploy.yml",
                ".github/workflows/secrets.yml",
                "Dockerfile",
                "scripts/deploy.sh",
                "scripts/secret-rotate.sh",
            )
            for name in names:
                (root / name).parent.mkdir(parents=True, exist_ok=True)
                (root / name).write_text("kind: List\n", encoding="utf-8")
            (root / "k8s" / "directory").mkdir()
            (root / "logo.png").write_bytes(b"\x89PNG\xff\xfe")
            tracked = [*names, "k8s/directory", "logo.png", "removed/in-the-worktree.yml"]
            opened = []
            real = Path.read_text

            def read_text(path, *args, **kwargs):
                opened.append(path.relative_to(root).as_posix())
                return real(path, *args, **kwargs)

            with patch.object(Path, "read_text", read_text):
                manifests, texts = read_deployment_sources(root, tracked)
                problems = dotenv_problems(root, tracked)
        self.assertEqual(
            sorted(manifests),
            [".github/workflows/deploy.yml", "k8s/app.yml", "ops/anywhere.json"],
        )
        self.assertEqual(
            sorted(texts), [".github/workflows/deploy.yml", "Dockerfile", "scripts/deploy.sh"]
        )
        # Only names were used for the env files; the one file opened beyond
        # the sources is .dockerignore (absent here), and no image is read as text.
        self.assertEqual(
            sorted(opened),
            [
                ".dockerignore",
                ".github/workflows/deploy.yml",
                "Dockerfile",
                "k8s/app.yml",
                "logo.png",
                "ops/anywhere.json",
                "scripts/deploy.sh",
            ],
        )
        self.assertEqual(len(problems), 5, problems)
        for name in (*self.checked_in[0], *self.checked_in[1]):
            self.assertFalse(is_secret_file(name), name)
        self.assertTrue(is_secret_file("k8s/crank-capability-secrets.yml"))
        self.assertTrue(is_secret_file(".env"))
        self.assertTrue(is_secret_file(".env-prod"))

    def test_on_in_the_one_allowed_place_follows_the_record(self):
        self.edit(CONFIGMAP_FILE, switch_on)
        self.assertEqual(self.problems(self.record()), [])
        self.assertEqual(self.problems(), [f"{SWITCH_KEY} is on without a verification record"])
        failed = self.record(g1_public_path="fail")
        self.assertEqual(
            self.problems(failed), ["the last record does not pass g1_public_path"]
        )

    def test_another_configmap_is_refused(self):
        def add(documents):
            documents[0]["data"][SWITCH_KEY] = "cloudflare"

        self.edit(self.agent_config, add)
        self.assert_refused(f"{SWITCH_KEY} is set in ConfigMap crank-agent-config")

    def test_a_new_configmap_in_a_new_manifest_is_refused(self):
        self.manifests["k8s/extra.yml"] = yaml.safe_dump(
            {
                "kind": "ConfigMap",
                "metadata": {"name": "crank-extra"},
                "data": {SWITCH_KEY: "cloudflare"},
            }
        )
        self.assert_refused(f"{SWITCH_KEY} is set in ConfigMap crank-extra (k8s/extra.yml)")

    def test_a_second_definition_of_the_configmap_is_refused(self):
        self.manifests["deploy/shadow.yaml"] = yaml.safe_dump(
            {
                "kind": "ConfigMap",
                "metadata": {"name": SWITCH_CONFIGMAP},
                "data": {SWITCH_KEY: "cloudflare"},
            }
        )
        self.assert_refused("ConfigMap crank-config must be defined exactly once (found 2)")

    def test_a_missing_configmap_is_refused(self):
        del self.manifests["k8s/crank-configmap.yml"]
        self.assert_refused("ConfigMap crank-config must be defined exactly once (found 0)")
        self.assert_refused("loads ConfigMap crank-config, which this gate cannot read")
        self.assert_refused("the ConfigMap has no data mapping")

    def test_a_container_env_entry_is_refused(self):
        for name, kind in ((self.web, "Deployment"), (self.cronjob, "CronJob")):
            self.setUp()
            for entry in (
                {"name": SWITCH_KEY, "value": "cloudflare"},
                {"name": SWITCH_KEY, "valueFrom": {"configMapKeyRef": {"name": "x", "key": "y"}}},
            ):
                def add(documents, kind=kind, entry=entry):
                    self.first_container(documents, kind)["env"] = [
                        "ignored", {"name": "OTHER", "value": "1"}, entry,
                    ]

                with self.subTest(name=name, entry=sorted(entry)):
                    self.edit(name, add)
                    self.assert_refused(f"{SWITCH_KEY} is a container env entry in {name} {kind}/")

    def test_an_init_container_env_entry_is_refused(self):
        def add(documents):
            pod = next(d for d in documents if d.get("kind") == "Deployment")["spec"]["template"]
            pod["spec"]["initContainers"] = [
                {"name": "init", "env": [{"name": SWITCH_KEY, "value": "cloudflare"}]}
            ]

        self.edit(self.web, add)
        self.assert_refused(f"{SWITCH_KEY} is a container env entry in {self.web} Deployment/")

    def test_a_command_line_that_sets_it_is_refused(self):
        def add(documents):
            self.first_container(documents, "CronJob")["command"] = [
                "env", f"{SWITCH_KEY}=cloudflare", "python", "manage.py", "check",
            ]

        self.edit(self.cronjob, add)
        self.assert_refused(f"{self.cronjob} mentions {SWITCH_KEY}")

    def test_a_workflow_literal_is_refused(self):
        cases = (
            f"          k3s kubectl -n crank set env deployment/crank {SWITCH_KEY}=cloudflare\n",
            f"        env:\n          {SWITCH_KEY}: cloudflare\n",
        )
        for addition in cases:
            self.setUp()
            with self.subTest(addition=addition.strip()[:40]):
                self.texts[self.workflow] += addition
                self.assert_refused(f"{self.workflow} mentions {SWITCH_KEY}")

    def test_a_secret_carrying_the_switch_is_refused(self):
        text = self.texts[self.workflow]
        literal = "--from-literal=LLM_API_KEY='' \\\n"
        self.assertIn(literal, text)
        self.texts[self.workflow] = text.replace(
            literal, literal + f"            --from-literal={SWITCH_KEY}=cloudflare \\\n"
        )
        self.assert_refused(f"{SWITCH_KEY} is a key of Secret crank-capability-secrets")
        self.setUp()
        self.manifests["k8s/inline.yml"] = yaml.safe_dump(
            {
                "kind": "Secret",
                "metadata": {"name": "db-connect-credentials"},
                "stringData": {SWITCH_KEY: "cloudflare"},
            }
        )
        self.assert_refused(f"{SWITCH_KEY} is a key of Secret db-connect-credentials")

    def test_an_image_default_is_refused(self):
        for name in ("Dockerfile", "Base.Dockerfile", "docker-compose.yml"):
            self.setUp()
            with self.subTest(name=name):
                self.texts[name] += f"\nENV {SWITCH_KEY}=cloudflare\n"
                self.assert_refused(f"{name} mentions {SWITCH_KEY}")

    def test_a_comment_naming_the_switch_is_allowed(self):
        self.manifests[self.agent_config] += f"  # {SWITCH_KEY} lives in crank-config.\n"
        self.texts[self.workflow] += f"# {SWITCH_KEY} is never set here.\n"
        self.assertEqual(self.problems(), [])

    def test_environment_sources_the_gate_cannot_read_are_refused(self):
        cases = (
            ({"configMapRef": {"name": "elsewhere"}}, "loads ConfigMap elsewhere, which this gate cannot read"),
            ({"secretRef": {"name": "new-secret"}}, "loads Secret new-secret, and this gate cannot show"),
            ({"configMapRef": {"name": "crank-config"}, "prefix": "X_"}, "has an envFrom source this gate cannot read"),
            ({"somethingElse": {"name": "x"}}, "has an envFrom source this gate cannot read"),
            ("crank-config", "has an envFrom source this gate cannot read"),
        )
        for source, expected in cases:
            self.setUp()

            def add(documents, source=source):
                self.first_container(documents, "Deployment")["envFrom"].append(source)

            with self.subTest(expected=expected):
                self.edit(self.web, add)
                self.assert_refused(expected)

    def test_a_secret_defined_in_a_readable_manifest_is_accounted_for(self):
        self.manifests["k8s/inline.yml"] = yaml.safe_dump(
            {"kind": "Secret", "metadata": {"name": "new-secret"}, "data": {"A": "", "B": ""}}
        )

        def add(documents):
            self.first_container(documents, "Deployment")["envFrom"].append(
                {"secretRef": {"name": "new-secret"}}
            )

        self.edit(self.web, add)
        self.assertEqual(self.problems(), [])

    def test_a_secret_made_from_a_file_is_refused(self):
        for flag in ("--from-env-file=/tmp/values.env", "--from-file=/tmp/values"):
            self.setUp()
            with self.subTest(flag=flag):
                self.texts[self.workflow] += (
                    "          k3s kubectl -n crank create secret generic crank-capability-secrets \\\n"
                    f"            {flag} \\\n"
                    "            --dry-run=client -o yaml | k3s kubectl apply -f -\n"
                    "          k3s kubectl -n crank create secret generic crank-capability-secrets \\\n"
                    "            --from-literal=LATER='' | k3s kubectl apply -f -\n"
                )
                self.assert_refused(
                    "Secret crank-capability-secrets (k8s/crank-cronjob.yml CronJob/"
                )
                self.assert_refused("is created from a file this gate does not read")

    def test_a_workflow_that_applies_a_secrets_file_is_refused(self):
        for line in (
            "envsubst < /tmp/crank-capability-secrets.yml | k3s kubectl apply -f -",
            "k3s kubectl apply -f /tmp/crank-capability-secrets.yml",
            "kubectl apply --filename=k8s/Crank-Secrets.yaml",
        ):
            self.setUp()
            with self.subTest(line=line):
                self.texts[self.workflow] += f"          {line}\n"
                self.assert_refused(
                    f"{self.workflow} applies a secrets file, which this gate does not read"
                )

    def test_copying_a_secrets_file_without_applying_it_is_allowed(self):
        # Today's workflows copy the file to the node and never apply it.
        self.assertRegex(self.texts[self.workflow], r"scp [^\n]*crank-capability-secrets\.yml")
        self.texts[self.workflow] += (
            "          # kubectl apply -f /tmp/crank-capability-secrets.yml\n"
        )
        self.assertEqual(self.problems(), [])

    def test_an_apply_written_over_several_lines_is_refused(self):
        for lines in (
            ("envsubst < /tmp/crank-capability-secrets.yml \\", "  | k3s kubectl apply -f -"),
            ("cat /tmp/crank-capability-secrets.yml |", "  k3s kubectl apply -f -"),
            ("k3s kubectl apply \\", "  # the next line names the file", "  -f /tmp/app-secrets.yaml"),
        ):
            self.setUp()
            with self.subTest(lines=lines):
                self.texts[self.workflow] += "".join(f"          {line}\n" for line in lines)
                self.assert_refused(
                    f"{self.workflow} applies a secrets file, which this gate does not read"
                )

    def test_a_script_that_applies_a_secrets_file_is_refused(self):
        self.texts["scripts/deploy.sh"] = "kubectl apply -f k8s/crank-capability-secrets.yml\n"
        self.assert_refused("scripts/deploy.sh applies a secrets file")

    def test_a_list_wrapper_is_looked_inside(self):
        extra = {
            "kind": "ConfigMap",
            "metadata": {"name": "crank-extra"},
            "data": {SWITCH_KEY: "cloudflare"},
        }
        shadow = {**extra, "metadata": {"name": SWITCH_CONFIGMAP}}
        pod = {
            "kind": "Pod",
            "metadata": {"name": "p"},
            "spec": {"containers": [{"env": [{"name": SWITCH_KEY, "value": "cloudflare"}]}]},
        }
        secret = {"kind": "Secret", "metadata": {"name": "db-connect-credentials"},
                  "stringData": {SWITCH_KEY: "cloudflare"}}
        cases = (
            (extra, f"{SWITCH_KEY} is set in ConfigMap crank-extra"),
            (shadow, "ConfigMap crank-config must be defined exactly once (found 2)"),
            (pod, f"{SWITCH_KEY} is a container env entry in {CONFIGMAP_FILE} Pod/p"),
            (secret, f"{SWITCH_KEY} is a key of Secret db-connect-credentials"),
            ({"kind": "ConfigMapList", "items": [{"kind": "List", "items": [extra]}]},
             f"{SWITCH_KEY} is set in ConfigMap crank-extra"),
        )
        for item, expected in cases:
            self.setUp()

            # Inside the crank-config file itself, where the text is not scanned.
            def wrap(documents, item=item):
                documents.append({"kind": "List", "items": ["text", item]})

            with self.subTest(expected=expected):
                self.edit(CONFIGMAP_FILE, wrap)
                self.assert_refused(expected)
        self.setUp()
        self.edit(CONFIGMAP_FILE, lambda documents: documents.append({"kind": "List", "items": None}))
        self.assertEqual(self.problems(), [])

    def test_a_manifest_anywhere_in_the_tree_is_read(self):
        manifest = {
            "kind": "ConfigMap",
            "metadata": {"name": "crank-extra"},
            "data": {SWITCH_KEY: "cloudflare"},
        }
        for name, text in (
            ("ops/extra.yml", yaml.safe_dump(manifest)),
            ("ops/extra.json", json.dumps(manifest)),
            ("docs/extra.yaml", yaml.safe_dump(manifest)),
            ("crank/tests/fixtures/extra.json", json.dumps(manifest)),
        ):
            self.setUp()
            with self.subTest(name=name):
                self.manifests[name] = text
                self.assert_refused(f"{SWITCH_KEY} is set in ConfigMap crank-extra ({name})")

    def test_any_other_file_naming_the_switch_is_refused(self):
        for name in (
            "scripts/deploy.sh",
            ".github/actions/deploy/action.yml",
            "run_test_with_timeout.sh",
            "crank/views/job_search.py",
            "crank/settings/dev.py",
            "README.md",
        ):
            self.setUp()
            with self.subTest(name=name):
                self.texts[name] = (
                    f"k3s kubectl -n crank set env deployment/crank {SWITCH_KEY}=cloudflare\n"
                )
                self.assert_refused(f"{name} mentions {SWITCH_KEY}")

    def test_the_files_that_read_or_document_the_switch_may_name_it(self):
        for name in (*sorted(SWITCH_READERS), "crank/tests/test_checks.py", "docs/notes.yaml"):
            with self.subTest(name=name):
                self.assertTrue(may_name_switch(name))
                self.manifests[name] = f"note: {SWITCH_KEY} is read in production\n"
                self.assertEqual(self.problems(), [])
        for name in ("crank/settings/dev.py", "crank/views/job_search.py", "docs.yml", "k8s/x.yml"):
            self.assertFalse(may_name_switch(name), name)
        # The allow-list is exactly what names the switch in the checkout today.
        manifests, texts = self.checked_in
        self.assertEqual([name for name in texts if may_name_switch(name)], [])
        self.assertTrue(set(SWITCH_READERS) <= set(tracked_files()))

    def test_malformed_test_data_is_the_only_unparsed_file_tolerated(self):
        self.manifests["crank/tests/fixtures/broken.json"] = "{ not json"
        self.assertEqual(self.problems(), [])
        self.manifests["crank/tests/broken.json"] = "{ not json"
        self.assert_refused("crank/tests/broken.json cannot be parsed")

    def test_unreadable_and_empty_inputs_are_refused(self):
        self.manifests["k8s/broken.yml"] = "kind: [unclosed\n"
        self.manifests["k8s/scalar.yml"] = "just text\n---\n- a\n- b\n"
        self.assert_refused(f"k8s/broken.yml cannot be parsed, so it cannot be checked for {SWITCH_KEY}")
        self.manifests, self.texts = {}, {}
        self.assert_refused("no Deployment was found, so nothing was checked")

    def test_documents_without_the_usual_shape_are_tolerated(self):
        self.manifests["k8s/odd.yml"] = yaml.safe_dump_all(
            [
                {"kind": "ConfigMap", "metadata": "name", "data": ["not", "a", "mapping"]},
                {"kind": "Pod", "spec": {"containers": ["text", {"name": "c", "envFrom": "x"}]}},
            ]
        )
        self.assertEqual(self.problems(), [])


class DotenvGateTests(SimpleTestCase):
    """No env file can reach the image; decided from names, never contents."""

    rules = "# comment\n.env\n**/.env\n"

    def problems(self, tracked, ignore=rules):
        with tempfile.TemporaryDirectory() as directory:
            if ignore is not None:
                (Path(directory) / DOCKERIGNORE_FILE).write_text(ignore, encoding="utf-8")
            return dotenv_problems(directory, tracked)

    def test_the_checkout_is_clean(self):
        tracked = tracked_files()
        self.assertEqual(dotenv_problems(), [])
        self.assertEqual(dotenv_problems(REPO_ROOT, tracked), [])
        self.assertIn("manage.py", tracked)
        self.assertIn(DOCKERIGNORE_FILE, tracked)
        self.assertEqual(tracked, sorted(set(tracked)))
        self.assertEqual(
            [name for name in tracked if Path(name).name.startswith(".env")],
            sorted(UNREAD_ENV_FILES),
        )
        # The one tracked env-like file is not a name load_dotenv() looks for.
        self.assertNotIn(DOTENV_NAME, UNREAD_ENV_FILES)

    def test_a_tracked_dotenv_file_is_refused_anywhere(self):
        for name in (".env", "crank/.env", "deploy/nested/.env"):
            with self.subTest(name=name):
                self.assertEqual(
                    self.problems(["manage.py", name]),
                    [f"{name} is tracked: load_dotenv() would read it in the image"],
                )

    def test_other_env_files_must_be_known(self):
        self.assertEqual(self.problems(["manage.py", ".env-prod"]), [])
        for name in (".env.production", ".ENV.local", "k8s/.env-prod", ".envrc"):
            with self.subTest(name=name):
                self.assertEqual(
                    self.problems([name]),
                    [f"{name} is a tracked env file this gate does not read"],
                )
        self.assertEqual(self.problems(["environment.yml", "crank/env.py"]), [])

    def test_the_image_build_must_ignore_dotenv_files(self):
        both = [f".dockerignore must keep {rule} out of the image" for rule in DOCKERIGNORE_RULES]
        self.assertEqual(self.problems([], None), both)
        self.assertEqual(self.problems([], ""), both)
        self.assertEqual(self.problems([], "# .env\n**/.env\n"), both[:1])
        self.assertEqual(self.problems([], "  .env  \n"), both[1:])
        self.assertEqual(
            self.problems([], self.rules + "!.env\n"),
            [".dockerignore lets an env file back into the image: !.env"],
        )
        self.assertEqual(self.problems([], self.rules + "!README.md\n*.pyc\n"), [])

    def test_tracked_files_needs_a_git_checkout(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(subprocess.CalledProcessError):
                tracked_files(directory)


class RepositoryStateTests(SimpleTestCase):
    """One rule for the checkout, whatever state it is in.

    The switch is on only if the last record passes; otherwise it must be
    off. The rule is proved in both directions on a copy of the checked-in
    files, so enabling the switch later (one ConfigMap value plus one record,
    as the runbook describes) needs no change to any test in this file.
    """

    passing = {
        "date": datetime.date.today().isoformat(),
        "verified_by": "Operator Name",
        "g1_public_path": "pass",
        "g2_private_path": "pass",
        "g2_method": "nodeport-addresses",
        "edge_header_checks": "pass",
    }

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tracked = tracked_files()
        directory = tempfile.mkdtemp()
        cls.addClassCleanup(shutil.rmtree, directory)
        cls.root = Path(directory)
        # Secrets files are never copied; only their names stay in the list.
        manifests, texts = read_deployment_sources(REPO_ROOT, cls.tracked)
        for name in {*manifests, *texts, RECORD_FILE, DOCKERIGNORE_FILE}:
            (cls.root / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(REPO_ROOT / name, cls.root / name)
        cls.configmap = (cls.root / CONFIGMAP_FILE).read_text(encoding="utf-8")

    def setUp(self):
        self.addCleanup(self.set_state, False, [])
        self.extra = []

    def write(self, name, text):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        self.addCleanup(path.unlink)
        self.extra.append(name)

    def set_state(self, on, records):
        """Write the two files the enabling pull request changes."""
        documents = list(yaml.safe_load_all(self.configmap))
        (switch_on if on else switch_off)(documents)
        (self.root / CONFIGMAP_FILE).write_text(yaml.safe_dump_all(documents), encoding="utf-8")
        text = records if isinstance(records, str) else yaml.safe_dump({"verifications": records})
        (self.root / RECORD_FILE).write_text(text, encoding="utf-8")

    def problems(self, on, records):
        self.set_state(on, records)
        return repository_problems(self.root, tracked=[*self.tracked, *self.extra])

    def test_the_checked_in_repository_obeys_the_rule(self):
        """The only assertion here that depends on the checked-in state."""
        self.assertEqual(repository_problems(), [])
        self.assertEqual(repository_problems(REPO_ROOT, datetime.date.today()), [])

    def test_off_passes_with_any_well_formed_record(self):
        failed = {**self.passing, "g1_public_path": "fail"}
        for records in ([], [failed], [self.passing], [self.passing, failed]):
            with self.subTest(records=len(records)):
                self.assertEqual(self.problems(False, records), [])

    def test_on_with_a_passing_last_record_passes(self):
        """The runbook's "Record, then enable", exactly: a record and the value."""
        failed = {**self.passing, "g1_public_path": "fail"}
        waived = {**self.passing, "g2_private_path": "waived", "g2_method": "waived"}
        for records in ([self.passing], [failed, self.passing], [waived]):
            with self.subTest(records=len(records)):
                self.assertEqual(self.problems(True, records), [])
        text = (self.root / CONFIGMAP_FILE).read_text(encoding="utf-8")
        self.assertEqual(yaml.safe_load(text)["data"][SWITCH_KEY], "cloudflare")

    def test_on_without_a_passing_last_record_fails(self):
        tomorrow = (datetime.date.today() + datetime.timedelta(days=1)).isoformat()
        cases = {
            "empty": ([], f"{SWITCH_KEY} is on without a verification record"),
            "failed": (
                [{**self.passing, "g1_public_path": "fail"}],
                "the last record does not pass g1_public_path",
            ),
            "failed last": (
                [self.passing, {**self.passing, "edge_header_checks": "fail"}],
                "the last record does not pass edge_header_checks",
            ),
            "future": ([{**self.passing, "date": tomorrow}], "the last record is dated in the future"),
            "malformed record": ([{"date": "yesterday"}], "date must be an ISO date"),
            "not a list": ("verifications: yes\n", "verifications must be a list"),
            "not yaml": ("verifications: [unclosed\n", "verifications must be a list"),
            "blank": ("", "verifications must be a list"),
        }
        for name, (records, expected) in cases.items():
            with self.subTest(name=name):
                self.assertIn(expected, self.problems(True, records))

    def test_a_missing_record_file_fails_only_as_malformed(self):
        self.set_state(True, [self.passing])
        record = self.root / RECORD_FILE
        text = record.read_text(encoding="utf-8")
        record.unlink()
        self.addCleanup(record.write_text, text, encoding="utf-8")
        self.assertEqual(
            repository_problems(self.root, tracked=self.tracked), ["verifications must be a list"]
        )

    def test_a_passing_record_does_not_excuse_another_source(self):
        manifest = yaml.safe_dump(
            {"kind": "ConfigMap", "metadata": {"name": "crank-extra"},
             "data": {SWITCH_KEY: "cloudflare"}}
        )
        command = f"k3s kubectl -n crank set env deployment/crank {SWITCH_KEY}=cloudflare\n"
        cases = (
            ("ops/extra.yml", manifest, f"{SWITCH_KEY} is set in ConfigMap crank-extra (ops/extra.yml)"),
            ("scripts/deploy.sh", command, f"scripts/deploy.sh mentions {SWITCH_KEY}"),
            (".github/actions/deploy/action.yml", f"runs:\n  steps:\n    - run: {command}",
             f".github/actions/deploy/action.yml mentions {SWITCH_KEY}"),
        )
        for name, text, expected in cases:
            for on in (False, True):
                with self.subTest(name=name, on=on):
                    self.write(name, text)
                    problems = self.problems(on, [self.passing])
                    self.assertTrue(any(expected in problem for problem in problems), problems)
                    self.doCleanups()
                    self.setUp()

    def test_a_tracked_dotenv_file_fails_whatever_the_record(self):
        # Named in the tracked list only: no such file is created or read.
        for on in (False, True):
            self.set_state(on, [self.passing])
            self.assertEqual(
                repository_problems(self.root, tracked=[*self.tracked, ".env"]),
                [".env is tracked: load_dotenv() would read it in the image"],
            )
        self.assertFalse((self.root / ".env").exists())

    def test_the_copy_holds_no_secrets_file(self):
        copied = [path for path in self.root.rglob("*") if path.is_file()]
        self.assertTrue(copied)
        self.assertEqual([path for path in copied if is_secret_file(path)], [])
        self.assertTrue(any(is_secret_file(name) for name in self.tracked))


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
