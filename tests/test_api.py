"""API and end-to-end tests.

`test_full_review_on_a_real_repository` is the important one: it builds an actual
git repository, runs the whole pipeline with no model, and asserts the review
finds the defects it is supposed to find. If that passes, the plumbing works.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from vibekill.api import app
from vibekill.pipeline import run_review
from vibekill.settings import Settings

client = TestClient(app)


# Deliberately bad code, in the style AI assistants produce: tidy, plausible,
# and wrong in several ways at once.
BAD_MODULE = '''\
"""User lookup."""

import sqlite3

API_TOKEN = "sk_live_4f8a7b6c5d4e3f2a"


def find_user(name, locale="en", include_deleted=False):
    conn = sqlite3.connect("users.db")
    cur = conn.cursor()
    query = "SELECT * FROM users WHERE name = '" + name + "'"
    cur.execute(query)
    return cur.fetchall()


def load_profile(user_id):
    try:
        return find_user(str(user_id))
    except Exception:
        pass
'''

BAD_TEST = '''\
from app.users import find_user


def test_find_user_works():
    result = find_user("alice")
    assert result is not None
'''


@pytest.fixture(scope="module")
def repo(tmp_path_factory) -> Path:
    """A git repository whose HEAD commit introduces several known defects."""
    root = tmp_path_factory.mktemp("repo")

    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)

    git("init", "-q")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "Test")
    (root / "README.md").write_text("# demo\n")
    git("add", "-A")
    git("commit", "-qm", "initial")

    (root / "app").mkdir()
    (root / "tests").mkdir()
    (root / "app" / "users.py").write_text(BAD_MODULE)
    (root / "tests" / "test_users.py").write_text(BAD_TEST)
    git("add", "-A")
    git("commit", "-qm", "Add user lookup")
    return root


class TestEndToEnd:
    def test_full_review_on_a_real_repository(self, repo):
        settings = Settings(llm={"provider": "none"}, tools={"enabled": False})
        run = run_review(settings, repo_path=repo, base="HEAD~1", head="HEAD", write=False)
        result = run.result

        assert len(result.context.files) == 2
        rules = {f.rule_id for f in result.findings}

        # The defects a rules-only review must not miss.
        assert "vibekill.security.hardcoded_secret" in rules
        assert "vibekill.security.sql_injection" in rules
        assert "vibekill.correctness.swallowed_exception" in rules
        assert "vibekill.testing.weak_assertion" in rules
        assert "vibekill.redundancy.unused_parameter" in rules

        assert result.verdict.value == "request_changes"
        assert result.counts()["blocker"] >= 2

    def test_review_writes_all_report_formats(self, repo, tmp_path):
        settings = Settings(llm={"provider": "none"}, tools={"enabled": False})
        run = run_review(
            settings,
            repo_path=repo,
            base="HEAD~1",
            output_dir=tmp_path / "out",
            formats=["markdown", "html", "json", "sarif"],
        )
        assert set(run.reports) == {"markdown", "html", "json", "sarif"}
        for path in run.reports.values():
            assert path.is_file() and path.stat().st_size > 0

    def test_html_report_is_self_contained(self, repo, tmp_path):
        """It has to open on a machine with no network, so no external refs."""
        settings = Settings(llm={"provider": "none"}, tools={"enabled": False})
        run = run_review(
            settings, repo_path=repo, base="HEAD~1", output_dir=tmp_path, formats=["html"]
        )
        html = run.reports["html"].read_text()
        assert "<style>" in html
        assert "Findings by category" in html
        assert "Most affected files" in html
        assert 'role="status"' in html
        assert "http://" not in html.replace("http://127.0.0.1", "")
        assert "cdn." not in html

    def test_every_agent_is_accounted_for(self, repo):
        """A skipped agent must say so; silence would look like a clean result."""
        settings = Settings(llm={"provider": "none"}, tools={"enabled": False})
        run = run_review(settings, repo_path=repo, base="HEAD~1", write=False)
        names = {a.name for a in run.result.agent_results}
        assert names == {"security", "quality", "testing", "complexity"}
        for agent in run.result.agent_results:
            assert agent.skipped_reason == "no model available"

    def test_clean_commit_produces_no_blockers(self, repo):
        subprocess.run(
            ["git", "checkout", "-q", "-b", "clean"], cwd=repo, check=True, capture_output=True
        )
        (repo / "app" / "math_utils.py").write_text(
            '"""Arithmetic helpers."""\n\n\ndef add(left: int, right: int) -> int:\n'
            '    """Return the sum of two integers."""\n    return left + right\n'
        )
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-qm", "Add add()"], cwd=repo, check=True, capture_output=True
        )
        settings = Settings(llm={"provider": "none"}, tools={"enabled": False})
        run = run_review(settings, repo_path=repo, base="HEAD~1", head="HEAD", write=False)
        assert run.result.counts()["blocker"] == 0
        assert run.exit_code == 0
        subprocess.run(["git", "checkout", "-q", "-"], cwd=repo, check=True, capture_output=True)


class TestAPI:
    def test_health_reports_mode_and_model(self):
        response = client.get("/health")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ok"
        assert body["mode"] in ("airgapped", "github")
        assert "model_available" in body

    def test_airgap_endpoint_confirms_enforcement(self):
        body = client.get("/airgap").json()
        assert body["verified"] is True

    def test_rejects_a_nonexistent_repo_path(self):
        response = client.post("/reviews", json={"repo_path": "/nope/not/here"})
        assert response.status_code == 400

    def test_review_returns_a_full_result(self, repo):
        response = client.post(
            "/reviews",
            json={"repo_path": str(repo), "base": "HEAD~1", "provider": "none"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["schema"] == "vibekill/review/v1"
        assert body["verdict"] in ("approve", "comment", "request_changes")
        assert len(body["findings"]) > 0
        assert {a["name"] for a in body["agents"]} == {
            "security",
            "quality",
            "testing",
            "complexity",
        }

    def test_report_endpoints(self, repo):
        request = {"repo_path": str(repo), "base": "HEAD~1", "provider": "none"}

        html = client.post("/reviews/report", json=request)
        assert html.status_code == 200
        assert "<!doctype html>" in html.text.lower()

        sarif = client.post("/reviews/report.sarif", json=request).json()
        assert sarif["version"] == "2.1.0"
        assert sarif["runs"][0]["tool"]["driver"]["name"] == "vibekill"

        markdown = client.post("/reviews/report.md", json=request)
        assert markdown.status_code == 200
        assert "# Review:" in markdown.text


def test_api_rejects_request_controlled_model_egress():
    response = client.post(
        "/reviews",
        json={
            "mode": "github",
            "github_repo": "owner/repository",
            "pr_number": 1,
            "provider": "openai",
            "llm_base_url": "http://169.254.169.254/latest/meta-data",
        },
    )
    assert response.status_code == 400
    assert "not allowed by server policy" in response.json()["detail"]
