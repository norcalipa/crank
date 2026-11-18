# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Lint for the production deploy workflows and the manifests they apply (#555).

The deploy workflows run only on a real deploy, so their text is the only thing
CI can check. Each rule below is a function over text or parsed manifests that
returns problems; each is run on the real files (no problems) and on an
in-memory mutated copy (problems), so a rule that stops detecting fails here.
"""

import re
from pathlib import Path

import yaml
from django.test import SimpleTestCase

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ("deploy-home.yml", "update-home-deployment.yml")
SECRET = "crank-capability-secrets"
CONFIG = "crank-agent-config"

_CONTINUATION = re.compile(r"\\\n\s*")
_CREATE_SECRET = re.compile(r"create secret generic (\S+)")
_PIPED_WRITE = re.compile(r"\|\s*(?:k3s\s+)?kubectl\b[^|]*\b(apply|replace|create\s+-f)\b")


def _workflow(name):
    return (REPO_ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8")


def logical_lines(text):
    """Command lines with comments dropped and continuation lines joined."""
    kept = "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )
    return [" ".join(line.split()) for line in _CONTINUATION.sub(" ", kept).splitlines()]


def secret_write_problems(text):
    """(a) A ``create secret`` with empty literals piped into a writing command."""
    return [
        line
        for line in logical_lines(text)
        if _CREATE_SECRET.search(line)
        and "--from-literal" in line
        and "=''" in line
        and _PIPED_WRITE.search(line)
    ]


def secret_guard_problems(text):
    """(b) Every ``create secret generic <name>`` follows an absence check."""
    problems, lines = [], logical_lines(text)
    for index, line in enumerate(lines):
        match = _CREATE_SECRET.search(line)
        if not match:
            continue
        name = match.group(1)
        guard = re.fullmatch(r'if \[ -z "\\?\$\{?(\w+)\}?" \]; then', lines[index - 1]) if index else None
        if not guard:
            problems.append(f"{name}: not directly inside an `if [ -z ... ]` guard")
            continue
        lookup = re.compile(
            rf"{guard.group(1)}=\\?\$\(\s*(?:k3s\s+)?kubectl\b[^)]*\bget secret {re.escape(name)}\b"
            r"[^)]*--ignore-not-found\s*\)"
        )
        if not any(lookup.fullmatch(prior) for prior in lines[:index - 1]):
            problems.append(f"{name}: no `get secret {name} ... --ignore-not-found` assignment")
    return problems


def rollout_problems(text):
    """(c) ``crank.yml`` is applied and a later line waits for the rollout."""
    lines = logical_lines(text)
    applied = [i for i, line in enumerate(lines) if "envsubst < /tmp/crank.yml" in line]
    if not applied:
        return ["crank.yml is not applied"]
    waits = [
        i
        for i, line in enumerate(lines)
        if re.search(r"rollout status deployment/crank --timeout=\d+s", line)
    ]
    return [] if any(i > applied[-1] for i in waits) else ["no rollout status after the apply"]


def deploy_cronjob_problems(manifests, texts):
    """(d) Every ``deploy/*.yaml`` CronJob is copied and applied by each workflow."""
    problems = []
    for manifest in manifests:
        for name, text in texts.items():
            if f"./deploy/{manifest}" not in text:
                problems.append(f"{name}: does not copy deploy/{manifest}")
            if not re.search(rf"envsubst < /tmp/{re.escape(manifest)} \| k3s kubectl apply", text):
                problems.append(f"{name}: does not apply deploy/{manifest}")
    return problems


def _pod_specs(document):
    spec = document.get("spec") or {}
    if document.get("kind") == "CronJob":
        spec = ((spec.get("jobTemplate") or {}).get("spec")) or {}
    return ((spec.get("template") or {}).get("spec")) or {}


def envfrom_problems(documents):
    """(e) A container that loads the agent ConfigMap also loads the Secret."""
    problems = []
    for document in documents:
        if not isinstance(document, dict):
            continue
        pod = _pod_specs(document)
        for container in (pod.get("containers") or []) + (pod.get("initContainers") or []):
            refs = container.get("envFrom") or []
            loads_config = any((r.get("configMapRef") or {}).get("name") == CONFIG for r in refs)
            loads_secret = any((r.get("secretRef") or {}).get("name") == SECRET for r in refs)
            if loads_config and not loads_secret:
                problems.append(
                    f"{(document.get('metadata') or {}).get('name')}/{container.get('name')}"
                )
    return problems


def _manifest_documents():
    documents = []
    for folder in ("k8s", "deploy"):
        for path in sorted((REPO_ROOT / folder).glob("*.y*ml")):
            documents += [d for d in yaml.safe_load_all(path.read_text(encoding="utf-8")) if d]
    return documents


def _deploy_cronjobs():
    return sorted(
        path.name
        for path in (REPO_ROOT / "deploy").glob("*.yaml")
        if "kind: CronJob" in path.read_text(encoding="utf-8")
    )


OLD_STEP = (
    "k3s kubectl -n crank create secret generic crank-capability-secrets \\\n"
    "  --from-literal=LLM_API_KEY='' \\\n"
    "  --from-literal=YELP_API_KEY='' \\\n"
    "  --dry-run=client -o yaml | k3s kubectl apply -f -\n"
)


class DeployWorkflowLintTests(SimpleTestCase):
    def test_real_workflows_pass_every_rule(self):
        texts = {name: _workflow(name) for name in WORKFLOWS}
        for name, text in texts.items():
            with self.subTest(workflow=name):
                self.assertEqual(secret_write_problems(text), [])
                self.assertEqual(secret_guard_problems(text), [])
                self.assertEqual(rollout_problems(text), [])
                self.assertIn("create secret generic crank-capability-secrets", text)
        self.assertEqual(deploy_cronjob_problems(_deploy_cronjobs(), texts), [])
        self.assertEqual(envfrom_problems(_manifest_documents()), [])

    def test_the_deploy_cronjobs_are_found(self):
        self.assertEqual(
            _deploy_cronjobs(),
            ["cronjob-job-pipeline.yaml", "cronjob-match-recompute.yaml"],
        )

    def test_logical_lines_drop_comments_and_join_continuations(self):
        self.assertEqual(logical_lines("# no\na \\\n  b\nc\n"), ["a b", "c"])

    # (a)
    def test_a_piped_create_of_an_empty_secret_is_flagged(self):
        self.assertTrue(secret_write_problems(OLD_STEP))
        for write in ("apply -f -", "replace -f -", "create -f -"):
            text = OLD_STEP.replace("apply -f -", write)
            with self.subTest(write=write):
                self.assertTrue(secret_write_problems(text))
        self.assertEqual(secret_write_problems(OLD_STEP.replace("| k3s kubectl apply -f -", "")), [])
        for name in WORKFLOWS:
            blanked = _workflow(name).replace(
                "--from-literal=FIRECRAWL_API_KEY=''\n",
                "--from-literal=FIRECRAWL_API_KEY='' \\\n  --dry-run=client -o yaml | k3s kubectl apply -f -\n",
            )
            with self.subTest(workflow=name):
                self.assertNotEqual(blanked, _workflow(name))
                self.assertTrue(secret_write_problems(blanked))

    # (b)
    def test_an_unguarded_create_is_flagged(self):
        for name in WORKFLOWS:
            text = _workflow(name)
            no_if = text.replace('if [ -z "\\${EXISTING}" ]; then', "true")
            no_get = text.replace("--ignore-not-found)", ")", 1)
            wrong_name = text.replace(
                "get secret crank-capability-secrets", "get secret other-secret"
            )
            for label, mutated in (("no if", no_if), ("no get", no_get), ("wrong name", wrong_name)):
                with self.subTest(workflow=name, mutation=label):
                    self.assertNotEqual(mutated, text)
                    self.assertTrue(secret_guard_problems(mutated))
        self.assertTrue(secret_guard_problems(OLD_STEP))

    # (c)
    def test_a_removed_rollout_wait_is_flagged(self):
        for name in WORKFLOWS:
            text = _workflow(name)
            stripped = text.replace(
                "rollout status deployment/crank --timeout=300s", "get deployment/crank"
            )
            with self.subTest(workflow=name):
                self.assertTrue(rollout_problems(stripped))
        self.assertEqual(rollout_problems("echo nothing\n"), ["crank.yml is not applied"])
        before = "k3s kubectl -n crank rollout status deployment/crank --timeout=1s\nenvsubst < /tmp/crank.yml | k3s kubectl apply -f -\n"
        self.assertTrue(rollout_problems(before))

    # (d)
    def test_a_deploy_cronjob_a_workflow_does_not_apply_is_flagged(self):
        texts = {name: _workflow(name) for name in WORKFLOWS}
        manifests = _deploy_cronjobs()
        for name in WORKFLOWS:
            for needle in (
                "./deploy/cronjob-job-pipeline.yaml",
                "envsubst < /tmp/cronjob-match-recompute.yaml",
            ):
                mutated = dict(texts, **{name: texts[name].replace(needle, "removed")})
                with self.subTest(workflow=name, removed=needle):
                    self.assertTrue(deploy_cronjob_problems(manifests, mutated))
        self.assertTrue(deploy_cronjob_problems(["cronjob-new.yaml"], texts))

    # (e)
    def test_a_container_without_the_secret_is_flagged(self):
        documents = _manifest_documents()
        self.assertTrue(documents)
        for document in documents:
            pod = _pod_specs(document) if isinstance(document, dict) else {}
            for container in pod.get("containers") or []:
                refs = container.get("envFrom") or []
                if any((r.get("secretRef") or {}).get("name") == SECRET for r in refs):
                    container["envFrom"] = [
                        r for r in refs if (r.get("secretRef") or {}).get("name") != SECRET
                    ]
        self.assertTrue(envfrom_problems(documents))
        self.assertEqual(envfrom_problems([None, {"kind": "Namespace"}]), [])
        solo = {
            "kind": "Deployment",
            "metadata": {"name": "x"},
            "spec": {"template": {"spec": {"containers": [
                {"name": "c", "envFrom": [{"configMapRef": {"name": CONFIG}}]}
            ]}}},
        }
        self.assertEqual(envfrom_problems([solo]), ["x/c"])
