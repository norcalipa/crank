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
    if not any(i > applied[-1] for i in waits):
        return ["no rollout status after the apply"]
    problems = []
    for i in waits:
        if i <= applied[-1]:
            continue
        if not re.fullmatch(r"if ! (?:k3s )?kubectl [^|;&]*rollout status deployment/crank --timeout=\d+s; then", lines[i]):
            problems.append(f"the rollout wait is not the condition of an `if !`: {lines[i]}")
            continue
        block = []
        for line in lines[i + 1:]:
            if line == "fi":
                break
            block.append(line)
        else:
            problems.append("the rollout wait block is not closed by `fi`")
            continue
        if not block or block[-1] != "exit 1":
            problems.append("a failed rollout wait does not end in `exit 1`")
    return problems


def ssh_heredoc(text):
    """The ``(ssh line, body lines, closed)`` of the single ``ssh ... <<EOF`` heredoc."""
    lines = logical_lines(text)
    starts = [i for i, line in enumerate(lines) if line.startswith("ssh ") and "<<" in line]
    if len(starts) != 1:
        return None, [], False
    body = []
    for line in lines[starts[0] + 1:]:
        if line == "EOF":
            return lines[starts[0]], body, True
        body.append(line)
    return lines[starts[0]], body, False


_WRITE_TARGET = re.compile(
    r"\bkubectl\b.*\b(?:apply|replace|create|patch)\b.*?\s(?:-f|-k|--filename|--kustomize)[ =]+(\S+)"
)


def secret_manifest_problems(text):
    """(f) Nothing in the SSH heredoc can apply the Secret manifest.

    Flags every logical line in the heredoc that names the manifest (a loop over
    manifests spans several lines, so the verb may be on another one), every
    apply from a directory or glob, and any non-``scp`` line elsewhere that
    writes from the manifest.
    """
    _, body, _ = ssh_heredoc(text)
    problems = []
    for line in logical_lines(text):
        named = re.search(r"crank-capability-secrets\.ya?ml", line)
        if line in body and named:
            problems.append(line)
        elif (
            named
            and re.search(r"\b(apply|replace|create|patch)\b", line)
            and not line.startswith("scp ")
        ):
            problems.append(line)
        elif line in body:
            target = _WRITE_TARGET.search(line)
            if target:
                path = target.group(1).strip("\"'")
                if path != "-" and (not re.search(r"\.ya?ml$", path) or "*" in path):
                    problems.append(line)
    return list(dict.fromkeys(problems))


def heredoc_problems(text):
    """(i) The heredoc fails fast and its exit status reaches the step."""
    head, body, closed = ssh_heredoc(text)
    if head is None:
        return ["there is not exactly one ssh heredoc"]
    problems = []
    if not head.endswith(" <<EOF"):
        problems.append("the ssh line does not end in <<EOF")
    if "||" in head or ";" in head:
        problems.append("the ssh line masks its exit status")
    if not body or body[0] != "set -euo pipefail":
        problems.append("the heredoc does not start with `set -euo pipefail`")
    if not closed:
        problems.append("the heredoc is not closed by EOF")
    return problems


def trigger_path_problems(workflow_text, build_text, manifests):
    """(g) A deploy/ CronJob edit triggers the deploy, and builds no image."""
    problems = []
    on = yaml.safe_load(workflow_text).get(True) or {}
    paths = ((on.get("push") or {}).get("paths")) or []
    if "deploy/**" not in paths:
        problems += [
            f"on.push.paths lacks deploy/{m}" for m in manifests if f"deploy/{m}" not in paths
        ]
    build = yaml.safe_load(build_text).get(True) or {}
    ignored = ((build.get("push") or {}).get("paths-ignore")) or []
    if "deploy/**" not in ignored:
        problems.append("build-image.yml does not ignore deploy/**")
    return problems


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


def hostalias_problems(documents):
    """(h) A pod that loads the ConfigMap with DB_HOST=fats maps fats to the k3s host.

    It also needs ``dnsPolicy: ClusterFirst`` and ``dnsConfig`` with the public
    resolvers, because in-cluster CoreDNS cannot resolve external names.
    """
    problems = []
    for document in documents:
        if not isinstance(document, dict) or document.get("kind") not in ("CronJob", "Job"):
            continue
        pod = _pod_specs(document)
        loads = any(
            (r.get("configMapRef") or {}).get("name") == "crank-config"
            for c in (pod.get("containers") or [])
            for r in (c.get("envFrom") or [])
        )
        aliases = {
            host: alias.get("ip")
            for alias in (pod.get("hostAliases") or [])
            for host in (alias.get("hostnames") or [])
        }
        nameservers = (pod.get("dnsConfig") or {}).get("nameservers") or []
        dns_ok = pod.get("dnsPolicy") == "ClusterFirst" and {"8.8.8.8", "1.1.1.1"} <= set(nameservers)
        if loads and (aliases.get("fats") != "192.168.0.10" or not dns_ok):
            problems.append((document.get("metadata") or {}).get("name"))
    return problems


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


# Pre-existing k8s/ CronJobs that load crank-config without the fats hostAliases.
# Not changed by #555; tracked in https://github.com/norcalipa/crank/issues/615.
# Remove a name when its manifest is fixed.
HOSTALIASES_FOLLOW_UP = {"crank-crawl-organizations", "crank-healthcheck", "crank-gather-scores"}


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
                self.assertEqual(heredoc_problems(text), [])
                self.assertEqual(secret_guard_problems(text), [])
                self.assertEqual(rollout_problems(text), [])
                self.assertIn("create secret generic crank-capability-secrets", text)
        self.assertEqual(deploy_cronjob_problems(_deploy_cronjobs(), texts), [])
        self.assertEqual(envfrom_problems(_manifest_documents()), [])
        build = _workflow("build-image.yml")
        for name, text in texts.items():
            with self.subTest(workflow=name, rule="secret manifest"):
                self.assertEqual(secret_manifest_problems(text), [])
        self.assertEqual(
            trigger_path_problems(texts["update-home-deployment.yml"], build, _deploy_cronjobs()), []
        )
        self.assertEqual(
            set(hostalias_problems(_manifest_documents())), HOSTALIASES_FOLLOW_UP
        )

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
        for name in WORKFLOWS:
            text = _workflow(name)
            ignored = text.replace(
                "if ! k3s kubectl -n crank rollout status deployment/crank --timeout=300s; then",
                "k3s kubectl -n crank rollout status deployment/crank --timeout=300s || true\nif false; then",
            )
            no_exit = text.replace("            exit 1\n          fi\n          EOF", "            true\n          fi\n          EOF")
            for label, mutated in (("|| true", ignored), ("no exit 1", no_exit)):
                with self.subTest(workflow=name, mutation=label):
                    self.assertNotEqual(mutated, text)
                    self.assertTrue(rollout_problems(mutated))
        unclosed = "envsubst < /tmp/crank.yml | k3s kubectl apply -f -\nif ! k3s kubectl rollout status deployment/crank --timeout=1s; then\nexit 1\n"
        self.assertEqual(rollout_problems(unclosed), ["the rollout wait block is not closed by `fi`"])
        early_and_late = (
            "if ! k3s kubectl rollout status deployment/crank --timeout=1s; then\nexit 1\nfi\n"
            "envsubst < /tmp/crank.yml | k3s kubectl apply -f -\n"
            "if ! k3s kubectl rollout status deployment/crank --timeout=1s; then\nexit 1\nfi\n"
        )
        self.assertEqual(rollout_problems(early_and_late), [])
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

    # (f)
    def test_applying_the_secret_manifest_is_flagged(self):
        for name in WORKFLOWS:
            text = _workflow(name)
            for line in (
                "envsubst < /tmp/crank-capability-secrets.yml | k3s kubectl apply -f -",
                "k3s kubectl apply -f /tmp/crank-capability-secrets.yml",
                "k3s kubectl replace -f k8s/crank-capability-secrets.yml",
            ):
                with self.subTest(workflow=name, line=line):
                    self.assertTrue(secret_manifest_problems(text + "\n" + line + "\n"))
            marker = "          k3s kubectl create namespace crank"
            for label, extra in (
                ("apply -f dir", "k3s kubectl apply -f /tmp/\n"),
                ("apply -f k8s/", "k3s kubectl apply -f k8s/\n"),
                ("apply -k", "k3s kubectl apply -k /tmp\n"),
                ("apply glob", "k3s kubectl apply -f '/tmp/crank-*.yml'\n"),
                (
                    "multi-line loop",
                    "for f in crank-configmap.yml crank-capability-secrets.yml; do\n"
                    "  envsubst < /tmp/$f | k3s kubectl apply -f -\n"
                    "done\n",
                ),
            ):
                mutated = text.replace(marker, "          " + extra.replace("\n", "\n          ") + marker.lstrip(), 1)
                with self.subTest(workflow=name, mutation=label):
                    self.assertNotEqual(mutated, text)
                    self.assertTrue(secret_manifest_problems(mutated))
        self.assertEqual(secret_manifest_problems("echo hi\n"), [])

    # (i)
    def test_an_unpinned_heredoc_is_flagged(self):
        for name in WORKFLOWS:
            text = _workflow(name)
            ssh_end = "${{ secrets.SSH_HOST }} <<EOF"
            mutations = {
                "no pipefail": text.replace("          set -euo pipefail\n", "", 1),
                "pipefail not first": text.replace(
                    "          set -euo pipefail\n", "          echo hi\n          set -euo pipefail\n", 1
                ),
                "|| true after heredoc": text.replace(ssh_end, ssh_end + " || true", 1),
                "|| true before heredoc": text.replace(ssh_end, "${{ secrets.SSH_HOST }} || true <<EOF", 1),
                "unclosed": text.replace("          EOF\n", "", 1),
            }
            for label, mutated in mutations.items():
                with self.subTest(workflow=name, mutation=label):
                    self.assertNotEqual(mutated, text)
                    self.assertTrue(heredoc_problems(mutated))
        self.assertEqual(heredoc_problems("echo\n"), ["there is not exactly one ssh heredoc"])

    # (g)
    def test_a_dropped_trigger_path_is_flagged(self):
        text = _workflow("update-home-deployment.yml")
        build = _workflow("build-image.yml")
        manifests = _deploy_cronjobs()
        for manifest in manifests:
            mutated = text.replace(f"      - 'deploy/{manifest}'\n", "")
            with self.subTest(removed=manifest):
                self.assertNotEqual(mutated, text)
                self.assertTrue(trigger_path_problems(mutated, build, manifests))
        mutated = build.replace("      - 'deploy/**'\n", "")
        self.assertNotEqual(mutated, build)
        self.assertEqual(
            trigger_path_problems(text, mutated, manifests), ["build-image.yml does not ignore deploy/**"]
        )
        wildcard = text.replace("      - 'deploy/cronjob-job-pipeline.yaml'\n", "      - 'deploy/**'\n")
        self.assertEqual(trigger_path_problems(wildcard, build, manifests), [])

    # (h)
    def test_a_cronjob_without_the_fats_host_alias_is_flagged(self):
        documents = _manifest_documents()
        for document in documents:
            pod = _pod_specs(document) if isinstance(document, dict) else {}
            if document.get("kind") in ("CronJob", "Job") and pod.get("hostAliases"):
                pod["hostAliases"][0]["ip"] = "10.0.0.10"
        self.assertTrue(
            {"crank-job-pipeline", "crank-match-recompute"} <= set(hostalias_problems(documents))
        )
        self.assertEqual(hostalias_problems([None, {"kind": "Namespace"}]), [])

    def test_a_cronjob_without_the_dns_config_is_flagged(self):
        for label, mutate in (
            ("no dnsConfig", lambda pod: pod.pop("dnsConfig")),
            ("no dnsPolicy", lambda pod: pod.pop("dnsPolicy")),
            ("wrong dnsPolicy", lambda pod: pod.update(dnsPolicy="Default")),
            ("one nameserver", lambda pod: pod["dnsConfig"].update(nameservers=["8.8.8.8"])),
        ):
            documents = _manifest_documents()
            for document in documents:
                pod = _pod_specs(document) if isinstance(document, dict) else {}
                if document.get("metadata", {}).get("name") in ("crank-job-pipeline", "crank-match-recompute"):
                    mutate(pod)
            with self.subTest(mutation=label):
                self.assertTrue(
                    {"crank-job-pipeline", "crank-match-recompute"} <= set(hostalias_problems(documents))
                )
