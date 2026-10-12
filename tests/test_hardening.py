"""Regression tests for launch-review trust boundaries."""

from __future__ import annotations

import json
import socket
import subprocess
import threading
import time
from pathlib import Path

from vibekill.agents.quality import QualityAgent
from vibekill.airgap import AirgapGuard
from vibekill.pipeline import run_review
from vibekill.settings import Settings
from vibekill.sources import LocalGitSource


def _repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)
    (path / "tracked.py").write_text("value = 1\n")
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=path, check=True)


def test_working_tree_review_includes_untracked_text_files(tmp_path):
    _repo(tmp_path)
    (tmp_path / "new_feature.py").write_text("TOKEN = 'sk_live_1234567890'\n")

    settings = Settings(llm={"provider": "none"}, tools={"enabled": False})
    context = LocalGitSource(settings, tmp_path, include_uncommitted=True).fetch()

    assert "new_feature.py" in {file.path for file in context.files}
    assert context.file_by_path("new_feature.py").change_type == "added"


def test_changed_lines_exclude_unchanged_hunk_context():
    from vibekill.diffparse import parse_unified_diff

    diff = """diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -10,3 +10,3 @@
 unchanged = True
-old = 1
+new = 1
 trailing = True
"""
    file_diff = parse_unified_diff(diff)[0]
    assert file_diff.changed_line_numbers() == {11}


def test_github_token_falls_back_to_standard_environment(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    settings = Settings(mode="github", llm={"provider": "none"})
    assert settings.github.token == "test-token"


def test_environment_overrides_repository_config(tmp_path, monkeypatch):
    config = tmp_path / "vibekill.toml"
    config.write_text('[llm]\nmodel = "from-file"\n')
    monkeypatch.setenv("VIBEKILL_LLM__MODEL", "from-environment")
    settings = Settings.load(config)
    assert settings.llm.model == "from-environment"


def test_explicit_values_override_environment(monkeypatch):
    monkeypatch.setenv("VIBEKILL_FAIL_ON", "minor")
    settings = Settings.load(fail_on="major")
    assert settings.fail_on.value == "major"


def test_github_mode_keeps_guard_and_narrow_allowlist():
    settings = Settings(
        mode="github",
        github={"api_url": "https://github.example/api"},
        llm={"provider": "openai", "base_url": "https://model.example"},
    )
    assert settings.enforce_airgap is True
    assert {"github.example", "model.example"} <= set(settings.allow_hosts)


def test_allowlisted_hostname_records_resolved_addresses(monkeypatch):
    real = socket.getaddrinfo

    def fake_getaddrinfo(host, port, *args, **kwargs):
        if host == "github.example":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.10", port))]
        return real(host, port, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    guard = AirgapGuard(allow_hosts={"github.example"}).engage()
    try:
        socket.getaddrinfo("github.example", 443)
        assert guard.host_allowed("192.0.2.10")
    finally:
        guard.release()


def test_reviews_are_serialized(monkeypatch):
    """The process-global socket patch must never overlap review lifecycles."""
    import vibekill.pipeline as pipeline

    active = 0
    maximum = 0
    lock = threading.Lock()

    def fake_review(*args, **kwargs):
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
        time.sleep(0.03)
        with lock:
            active -= 1
        return object()

    monkeypatch.setattr(pipeline, "_run_review_locked", fake_review)
    settings = Settings(llm={"provider": "none"})
    threads = [threading.Thread(target=run_review, args=(settings,)) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert maximum == 1


class RecordingClient:
    provider = "openai"
    model = "test"
    available = True

    def __init__(self):
        self.calls: list[tuple[str, str]] = []

    def chat(self, system: str, user: str, *, json_mode: bool = True) -> str:
        self.calls.append((system, user))
        return json.dumps({"summary": "ok", "findings": []})


def test_agent_prompts_are_hard_capped():
    from conftest import make_context

    lines = [f"value_{number} = {number}" for number in range(2_000)]
    context = make_context("large.py", lines)
    settings = Settings(
        llm={"provider": "openai", "context_chars": 8_000},
        tools={"enabled": False},
    )
    client = RecordingClient()

    QualityAgent(settings, client).run(context, [])

    assert client.calls
    assert all(len(system) + len(user) <= settings.llm.context_chars for system, user in client.calls)
    submitted = "\n".join(user for _, user in client.calls)
    assert "value_0" in submitted
    assert "value_1999" in submitted


def test_reports_expose_policy_coverage_and_stable_sarif_ids():
    from conftest import make_context
    from vibekill.aggregate import aggregate
    from vibekill.models import Category, Finding, Severity
    from vibekill.report import render_sarif

    context = make_context("app.py", ["print('debug')"])
    finding = Finding(
        title="Debug output",
        body="Remove it.",
        category=Category.QUALITY,
        severity=Severity.MINOR,
        path="app.py",
        line=1,
        evidence="print('debug')",
        source="heuristic",
        agent="heuristics",
        rule_id="vibekill.debug.leftover_print",
    )
    settings = Settings(llm={"provider": "none"}, fail_on="major")
    result = aggregate(context, [], [finding], settings)

    payload = result.to_dict()
    assert payload["policy"]["fail_on"] == "major"
    assert payload["coverage"]["reviewed_files"] == ["app.py"]

    sarif = json.loads(render_sarif(result))
    entry = sarif["runs"][0]["results"][0]
    assert entry["partialFingerprints"]["vibekillFindingId/v1"]
    assert sarif["runs"][0]["properties"]["verdict"] == result.verdict.value


def test_unborn_repository_working_tree_can_be_reviewed(tmp_path):
    """Pre-commit review must work before the repository's first commit."""
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    (tmp_path / "staged.py").write_text("value = 1\n")
    subprocess.run(["git", "add", "staged.py"], cwd=tmp_path, check=True)
    (tmp_path / "untracked.py").write_text("other = 2\n")

    settings = Settings(llm={"provider": "none"}, tools={"enabled": False})
    context = LocalGitSource(settings, tmp_path, include_uncommitted=True).fetch()

    assert context.meta.base_ref == "empty tree"
    assert {file.path for file in context.files} == {"staged.py", "untracked.py"}


def test_model_finding_on_unchanged_context_is_dropped():
    from vibekill.aggregate import aggregate
    from vibekill.diffparse import parse_unified_diff
    from vibekill.models import AgentResult, Category, Finding, RepoMeta, ReviewContext, Severity

    diff = """diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -1,2 +1,2 @@
 unchanged = dangerous(value)
-old = 1
+new = 1
"""
    context = ReviewContext(meta=RepoMeta(root="/tmp/repo"), files=parse_unified_diff(diff))
    finding = Finding(
        title="Problem on unchanged code",
        body="This line was not changed.",
        category=Category.SECURITY,
        severity=Severity.BLOCKER,
        path="app.py",
        line=1,
        evidence="unchanged = dangerous(value)",
        source="llm",
        agent="security",
        confidence=0.99,
    )
    result = aggregate(
        context,
        [AgentResult(name="security", findings=[finding])],
        [],
        Settings(llm={"provider": "none"}),
    )
    assert result.findings == []
    assert result.dropped_unverified == 1


def test_public_allowlist_is_not_reported_as_airgapped():
    from conftest import make_context
    from vibekill.aggregate import aggregate

    settings = Settings(
        allow_hosts=["127.0.0.1", "model.example"],
        llm={"provider": "none"},
    )
    result = aggregate(make_context("app.py", ["value = 1"]), [], [], settings)
    assert result.airgapped is False
    assert result.policy["network_guard_enabled"] is True


def test_github_mode_skips_filesystem_analyzers_cleanly():
    from conftest import make_context
    from vibekill.analysis.tools import run_tools

    context = make_context("app.py", ["value = 1"])
    context.mode = "github"
    settings = Settings(mode="github", llm={"provider": "none"})
    findings, outcomes = run_tools(context, settings)

    assert findings == []
    assert outcomes
    assert all("no local checkout" in outcome.skipped_reason for outcome in outcomes)
    assert all(not outcome.error for outcome in outcomes)
