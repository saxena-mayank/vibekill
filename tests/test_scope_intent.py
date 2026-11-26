"""Step 1: intent extraction, its "no intent" state, and how it reaches the report."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from typer.testing import CliRunner

from vibekill.cli import app
from vibekill.pipeline import run_review
from vibekill.scope.intent import build_intent, read_intent_files
from vibekill.settings import Settings

TASKS_MD = """# Implementation plan

- [x] 1. Add the users endpoint
  - Create `create_user` in `app/api/users.py`
  - _Requirements: 1.1_
- [ ] 2. Validate the email field
  - Update `app/models/user.py`
"""


# --------------------------------------------------------------------------- #
# build_intent
# --------------------------------------------------------------------------- #
def test_nothing_supplied_is_the_none_state():
    intent = build_intent()
    assert intent.status == "none"
    assert intent.sources == []
    assert intent.terms == {} and intent.paths == {} and intent.symbols == {}


def test_title_and_body_are_found():
    intent = build_intent(
        title="Add GET /users endpoint",
        body="Returns the user list. Implemented in `list_users` inside app/api/users.py.",
    )
    assert intent.status == "found"
    assert "endpoint" in intent.terms
    assert intent.terms["endpoint"] == "title"
    assert intent.paths["app/api/users.py"] == "description"
    assert intent.symbols["list_users"] == "description"
    assert {s.kind for s in intent.sources} == {"title", "description"}


def test_plural_and_case_are_normalised():
    intent = build_intent(title="Endpoints for Users")
    assert "endpoint" in intent.terms
    assert "user" in intent.terms


def test_generic_words_do_not_count_as_intent():
    intent = build_intent(title="fix stuff", body="misc updates and changes")
    assert intent.status == "none"
    assert intent.terms == {}


def test_commit_subjects_alone_are_weak():
    intent = build_intent(commit_subjects=["add retry to the payment client"])
    assert intent.status == "weak"
    assert intent.terms["retry"] == "commits"


def test_commit_subjects_do_not_upgrade_a_title():
    intent = build_intent(title="Retry payments", commit_subjects=["retry payments", "wip"])
    assert intent.status == "found"
    assert intent.terms["retry"] == "title"


def test_html_comments_from_pr_templates_are_ignored():
    body = (
        "## What changed\n<!-- One concern per pull request. Explain the behavior. -->\n"
        "Adds invoice export.\n"
    )
    intent = build_intent(body=body)
    assert "invoice" in intent.terms
    assert "concern" not in intent.terms
    assert "behavior" not in intent.terms


def test_fenced_code_is_ignored():
    body = "Adds export.\n```\nrm -rf secret_directory\n```\n"
    intent = build_intent(body=body)
    assert "export" in intent.terms
    assert "secret" not in intent.terms


def test_urls_are_not_treated_as_repo_paths():
    intent = build_intent(body="See https://example.com/docs/guide.md for background. Edits `a/b.py`.")
    assert "a/b.py" in intent.paths
    assert not any("example.com" in p for p in intent.paths)


def test_trailing_punctuation_is_removed_from_paths():
    intent = build_intent(body="Touches app/api/users.py, and also utils/date.py.")
    assert "app/api/users.py" in intent.paths
    assert "utils/date.py" in intent.paths


def test_file_source_outranks_description_for_the_same_path():
    intent = build_intent(
        body="Edits app/api/users.py.",
        intent_texts=[("tasks.md", TASKS_MD)],
    )
    assert intent.paths["app/api/users.py"] == "file"


def test_spec_markdown_is_read_as_plain_markdown():
    intent = build_intent(intent_texts=[("tasks.md", TASKS_MD)])
    assert intent.status == "found"
    assert intent.symbols["create_user"] == "file"
    assert intent.paths["app/models/user.py"] == "file"
    assert intent.tasks == ["1. Add the users endpoint", "2. Validate the email field"]
    assert [s.label for s in intent.sources] == ["tasks.md"]


def test_oversized_text_is_truncated_and_noted():
    intent = build_intent(body="invoice " + ("x " * 100_000))
    assert "invoice" in intent.terms
    assert any("truncated" in n for n in intent.notes)


def test_term_count_is_capped():
    words = " ".join(f"word{i}abc" for i in range(2000))
    intent = build_intent(title=words)
    assert len(intent.terms) <= 500


def test_to_dict_is_json_serialisable():
    intent = build_intent(title="Add export", intent_texts=[("t.md", TASKS_MD)])
    payload = json.loads(json.dumps(intent.to_dict()))
    assert payload["status"] == "found"
    assert payload["sources"][0]["kind"] in {"title", "file"}


# --------------------------------------------------------------------------- #
# read_intent_files
# --------------------------------------------------------------------------- #
def test_read_intent_files_reads_markdown(tmp_path):
    f = tmp_path / "tasks.md"
    f.write_text(TASKS_MD)
    texts, notes = read_intent_files([f])
    assert texts == [(str(f), TASKS_MD)]
    assert notes == []


def test_missing_intent_file_is_a_note_not_an_error(tmp_path):
    texts, notes = read_intent_files([tmp_path / "nope.md"])
    assert texts == []
    assert len(notes) == 1 and "nope.md" in notes[0]


def test_binary_intent_file_does_not_raise(tmp_path):
    f = tmp_path / "bad.md"
    f.write_bytes(b"\xff\xfe\x00 invoice export \xff")
    texts, _ = read_intent_files([f])
    assert len(texts) == 1


def test_oversized_intent_file_is_skipped_with_a_note(tmp_path):
    f = tmp_path / "big.md"
    f.write_bytes(b"a" * 300_000)
    texts, notes = read_intent_files([f])
    assert texts == []
    assert any("big.md" in n and "too large" in n for n in notes)


def test_directory_is_not_a_valid_intent_file(tmp_path):
    texts, notes = read_intent_files([tmp_path])
    assert texts == [] and notes


# --------------------------------------------------------------------------- #
# pipeline and CLI
# --------------------------------------------------------------------------- #
def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=repo, check=True, capture_output=True,
    )


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "app.py").write_text("x = 1\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "base")
    (repo / "app.py").write_text("x = 2\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "tweak the invoice export")
    return repo


def _settings(**extra):
    return Settings.load(llm={"provider": "none"}, tools={"enabled": False}, **extra)


def test_pipeline_uses_supplied_title_and_body(tmp_path):
    repo = _repo(tmp_path)
    run = run_review(
        _settings(), repo_path=repo, base="HEAD~1", write=False,
        pr_title="Add invoice export", pr_body="Touches `export_invoice`.",
    )
    intent = run.result.to_dict()["comprehension"]["intent"]
    assert intent["status"] == "found"
    assert "invoice" in intent["terms"]
    assert "export_invoice" in intent["symbols"]


def test_local_commit_subject_alone_is_weak_not_found(tmp_path):
    repo = _repo(tmp_path)
    run = run_review(_settings(), repo_path=repo, base="HEAD~1", write=False)
    intent = run.result.to_dict()["comprehension"]["intent"]
    assert intent["status"] == "weak"
    assert intent["terms"]["invoice"] == "commits"


def test_unreadable_intent_file_becomes_a_review_warning(tmp_path):
    repo = _repo(tmp_path)
    run = run_review(
        _settings(), repo_path=repo, base="HEAD~1", write=False,
        intent_files=[tmp_path / "missing.md"],
    )
    assert any("missing.md" in w for w in run.result.warnings)


def test_scope_can_be_disabled(tmp_path):
    repo = _repo(tmp_path)
    run = run_review(_settings(scope={"enabled": False}), repo_path=repo, base="HEAD~1", write=False)
    assert "comprehension" not in run.result.to_dict()


def test_existing_json_keys_are_unchanged(tmp_path):
    repo = _repo(tmp_path)
    payload = run_review(_settings(), repo_path=repo, base="HEAD~1", write=False).result.to_dict()
    assert payload["schema"] == "vibekill/review/v1"
    for key in ("verdict", "repo", "stats", "findings", "coverage", "agents", "warnings"):
        assert key in payload


def _cli_json(tmp_path: Path, *args: str, input: str | None = None, env: dict | None = None):
    repo = _repo(tmp_path)
    out = tmp_path / "out"
    result = CliRunner().invoke(
        app,
        ["review", "--repo", str(repo), "--base", "HEAD~1", "--provider", "none",
         "-q", "-f", "json", "-o", str(out), *args],
        input=input, env=env,
    )
    assert result.exit_code in (0, 1), result.output
    return json.loads((out / "review.json").read_text())["comprehension"]["intent"]


def test_cli_flags_supply_intent(tmp_path):
    body = tmp_path / "body.md"
    body.write_text("Exports invoices as CSV.")
    spec = tmp_path / "tasks.md"
    spec.write_text(TASKS_MD)
    intent = _cli_json(
        tmp_path, "--pr-title", "Add invoice export", "--pr-body-file", str(body),
        "--intent-file", str(spec),
    )
    assert intent["status"] == "found"
    assert intent["terms"]["csv"] == "description"
    assert intent["symbols"]["create_user"] == "file"


def test_cli_reads_body_from_stdin(tmp_path):
    intent = _cli_json(tmp_path, "--pr-body-file", "-", input="Exports invoices as CSV.")
    assert intent["terms"]["csv"] == "description"


def test_cli_environment_fallback(tmp_path):
    intent = _cli_json(
        tmp_path, env={"VIBEKILL_PR_TITLE": "Add invoice export", "VIBEKILL_PR_BODY": "Uses CSV."}
    )
    assert intent["terms"]["csv"] == "description"
    assert intent["terms"]["export"] == "title"


def test_cli_flag_beats_environment(tmp_path):
    intent = _cli_json(
        tmp_path, "--pr-title", "Add refund flow", env={"VIBEKILL_PR_TITLE": "Add invoice export"}
    )
    assert "refund" in intent["terms"]
    assert "export" not in intent["terms"] or intent["terms"]["export"] == "commits"


def test_cli_no_scope_omits_the_section(tmp_path):
    repo = _repo(tmp_path)
    out = tmp_path / "out"
    result = CliRunner().invoke(
        app,
        ["review", "--repo", str(repo), "--base", "HEAD~1", "--provider", "none",
         "-q", "-f", "json", "-o", str(out), "--no-scope"],
    )
    assert result.exit_code in (0, 1), result.output
    assert "comprehension" not in json.loads((out / "review.json").read_text())
