"""Step 2: grouping changed files into CORE / SUPPORTING / INCIDENTAL / UNTRACED.

One test per rule, plus the "no intent" and precision guards. Diffs are built inline so
no git repository is needed.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from vibekill.diffparse import parse_unified_diff
from vibekill.models import RepoMeta, ReviewContext
from vibekill.pipeline import run_review
from vibekill.scope.grouping import group_files
from vibekill.scope.intent import build_intent
from vibekill.settings import Settings


# --------------------------------------------------------------------------- #
# Builders
# --------------------------------------------------------------------------- #
def d_add(path: str, lines: list[str]) -> str:
    body = "\n".join(f"+{x}" for x in lines)
    return (
        f"diff --git a/{path} b/{path}\nnew file mode 100644\n--- /dev/null\n+++ b/{path}\n"
        f"@@ -0,0 +1,{len(lines)} @@\n{body}\n"
    )


def d_modify(path: str, old: list[str], new: list[str]) -> str:
    body = "\n".join([*(f"-{x}" for x in old), *(f"+{x}" for x in new)])
    return (
        f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n"
        f"@@ -1,{len(old)} +1,{len(new)} @@\n{body}\n"
    )


def d_delete(path: str, lines: list[str]) -> str:
    body = "\n".join(f"-{x}" for x in lines)
    return (
        f"diff --git a/{path} b/{path}\ndeleted file mode 100644\n--- a/{path}\n+++ /dev/null\n"
        f"@@ -1,{len(lines)} +0,0 @@\n{body}\n"
    )


def d_rename(old: str, new: str) -> str:
    return (
        f"diff --git a/{old} b/{new}\nsimilarity index 100%\nrename from {old}\nrename to {new}\n"
    )


def ctx(*diffs: str, excluded=(), contents: dict[str, str] | None = None) -> ReviewContext:
    files = parse_unified_diff("".join(diffs))
    for f in files:
        if contents and f.path in contents:
            f.new_content = contents[f.path]
    c = ReviewContext(meta=RepoMeta(root="/tmp/x"), files=files)
    c.excluded_files = list(excluded)
    return c


def run(context: ReviewContext, **intent_kwargs):
    intent = build_intent(**intent_kwargs)
    files, notes = group_files(context, intent)
    return {f.path: f for f in files}, notes


def only(groups, path):
    return groups[path].group, groups[path].reason


USERS = {"title": "Add GET /users endpoint"}
BODY = [f"    line_{i} = {i}" for i in range(6)]


# --------------------------------------------------------------------------- #
# Rule 1: INCIDENTAL
# --------------------------------------------------------------------------- #
def test_generated_or_lock_path_is_incidental():
    g, _ = run(ctx(excluded=["package-lock.json"]), **USERS)
    assert only(g, "package-lock.json") == ("INCIDENTAL", "incidental.generated_path")


def test_pure_rename_is_incidental_even_without_intent():
    g, _ = run(ctx(d_rename("a/old_name.py", "a/new_name.py")))
    assert only(g, "a/new_name.py") == ("INCIDENTAL", "incidental.pure_rename")
    assert g["a/new_name.py"].old_path == "a/old_name.py"


def test_rename_with_edits_is_not_a_pure_rename():
    diff = (
        "diff --git a/a/old.py b/a/new.py\nsimilarity index 80%\nrename from a/old.py\n"
        "rename to a/new.py\n--- a/a/old.py\n+++ b/a/new.py\n@@ -1,1 +1,1 @@\n-x = 1\n+x = 2\n"
    )
    g, _ = run(ctx(diff), **USERS)
    assert g["a/new.py"].reason != "incidental.pure_rename"


def test_whitespace_only_change_is_formatting_only():
    g, _ = run(ctx(d_modify("web/app.js", ["function a(){return 1}"], ["function a() { return 1 }"])), **USERS)
    assert only(g, "web/app.js") == ("INCIDENTAL", "incidental.formatting_only")


def test_python_spacing_around_operators_is_formatting_only():
    g, _ = run(ctx(d_modify("m/x.py", ["x=foo(1,2)"], ["x = foo(1, 2)"])), **USERS)
    assert only(g, "m/x.py") == ("INCIDENTAL", "incidental.formatting_only")


def test_trailing_comma_added_is_formatting_only():
    g, _ = run(ctx(d_modify("m/x.py", ["    b,", "    c"], ["    b,", "    c,"])), **USERS)
    assert only(g, "m/x.py") == ("INCIDENTAL", "incidental.formatting_only")


def test_blank_line_only_change_is_formatting_only():
    g, _ = run(ctx(d_modify("m/x.py", ["a = 1"], ["a = 1", "", ""])), **USERS)
    assert only(g, "m/x.py") == ("INCIDENTAL", "incidental.formatting_only")


def test_python_indentation_change_is_not_formatting():
    # Indentation decides which block a statement belongs to in Python.
    g, _ = run(ctx(d_modify("m/x.py", ["if a:", "    b()", "c()"], ["if a:", "    b()", "    c()"])), **USERS)
    assert g["m/x.py"].reason != "incidental.formatting_only"


def test_string_content_change_is_not_formatting():
    g, _ = run(ctx(d_modify("m/x.py", ['msg = "hello world"'], ['msg = "helloworld"'])), **USERS)
    assert g["m/x.py"].reason != "incidental.formatting_only"


def test_reordered_lines_are_not_formatting():
    g, _ = run(ctx(d_modify("m/x.py", ["a()", "b()"], ["b()", "a()"])), **USERS)
    assert g["m/x.py"].reason != "incidental.formatting_only"


def test_identifier_change_is_not_formatting():
    g, _ = run(ctx(d_modify("m/x.py", ["total = price"], ["total = prices"])), **USERS)
    assert g["m/x.py"].reason != "incidental.formatting_only"


def test_code_moved_verbatim_between_files_is_incidental_on_both_sides():
    block = ["def helper(a):", "    b = a + 1", "    c = b * 2", "    return c"]
    g, _ = run(
        ctx(
            d_modify("m/old.py", block, ["keep = 1"]),
            d_add("m/new.py", ["import os", *block]),
        ),
        **USERS,
    )
    assert only(g, "m/new.py") == ("INCIDENTAL", "incidental.moved_code")
    assert only(g, "m/old.py") == ("INCIDENTAL", "incidental.moved_code")


def test_partly_copied_code_is_not_a_move():
    block = ["def helper(a):", "    b = a + 1", "    c = b * 2", "    return c"]
    g, _ = run(
        ctx(
            d_modify("m/old.py", block, ["keep = 1"]),
            d_add("m/new.py", [*block, "def extra():", "    return 42", "    # new", "    pass"]),
        ),
        **USERS,
    )
    assert g["m/new.py"].reason != "incidental.moved_code"


def test_short_deletion_and_addition_are_not_a_move():
    g, _ = run(
        ctx(d_modify("m/a.py", ["x = 1", "y = 2"], ["z = 3"]), d_add("m/b.py", ["x = 1", "y = 2"])),
        **USERS,
    )
    assert g["m/b.py"].reason != "incidental.moved_code"


# --------------------------------------------------------------------------- #
# Rule 2: CORE
# --------------------------------------------------------------------------- #
def test_path_named_in_intent_is_core():
    g, _ = run(ctx(d_add("app/api/users.py", ["def f():", "    return 1"])),
               title="Add endpoint", body="Implemented in app/api/users.py.")
    assert only(g, "app/api/users.py") == ("CORE", "core.path_named")
    assert any("PR description" in e for e in g["app/api/users.py"].evidence)


def test_bare_filename_in_intent_matches_by_suffix():
    g, _ = run(ctx(d_add("app/api/users.py", ["x = 1"])), title="Change users.py")
    assert only(g, "app/api/users.py") == ("CORE", "core.path_named")


def test_symbol_named_in_intent_and_present_in_added_code_is_core():
    g, _ = run(ctx(d_add("app/handlers.py", ["def create_user():", "    return 1"])),
               title="Add endpoint", body="New handler `create_user`.")
    assert only(g, "app/handlers.py") == ("CORE", "core.symbol_named")


def test_symbol_named_but_absent_from_the_change_is_not_core():
    g, _ = run(ctx(d_add("app/other.py", ["x = 1"])), title="Add endpoint", body="New `create_user`.")
    assert g["app/other.py"].group == "UNTRACED"


def test_shared_term_with_path_is_core():
    g, _ = run(ctx(d_add("app/api/users.py", ["x = 1"])), **USERS)
    assert only(g, "app/api/users.py") == ("CORE", "core.term_match")


def test_shared_term_with_a_name_defined_in_the_change_is_core():
    g, _ = run(ctx(d_add("app/handlers.py", ["def export_invoice(x):", "    return x"])),
               title="Invoice export")
    assert only(g, "app/handlers.py") == ("CORE", "core.term_match")


def test_core_min_score_can_require_both_path_and_name_evidence():
    context = ctx(d_add("app/handlers.py", ["def export_invoice(x):", "    return x"]))
    files, _ = group_files(context, build_intent(title="Invoice export"), core_min_score=1.0)
    assert files[0].group == "UNTRACED"


def test_generic_path_words_do_not_link_a_file():
    g, _ = run(ctx(d_add("utils/date.py", ["x = 1"])), title="Add utils for the export job")
    assert g["utils/date.py"].group == "UNTRACED"


def test_terms_that_came_only_from_commit_subjects_do_not_link_files():
    g, _ = run(ctx(d_add("app/billing.py", ["x = 1"])), title="Add users endpoint",
               commit_subjects=["tweak billing"])
    assert g["app/billing.py"].group == "UNTRACED"


def test_a_test_file_is_never_core():
    g, _ = run(ctx(d_add("tests/test_users.py", ["def test_a():", "    pass"])),
               title="Update tests/test_users.py")
    assert g["tests/test_users.py"].group == "SUPPORTING"


def test_config_file_named_in_intent_is_core():
    g, _ = run(ctx(d_modify("config/limits.toml", ["a = 1"], ["a = 2"])),
               title="Raise limits", body="Edit config/limits.toml.")
    assert only(g, "config/limits.toml") == ("CORE", "core.path_named")


# --------------------------------------------------------------------------- #
# Rule 3: SUPPORTING
# --------------------------------------------------------------------------- #
def test_test_with_matching_name_supports_a_core_file():
    g, _ = run(ctx(d_add("app/api/users.py", ["x = 1"]), d_add("tests/test_users.py", ["def test_x():", "    pass"])),
               **USERS)
    assert only(g, "tests/test_users.py") == ("SUPPORTING", "supporting.test_of_core")


def test_test_importing_a_core_module_supports_it():
    contents = {"tests/test_api.py": "from app.api.users import create\n"}
    g, _ = run(
        ctx(d_add("app/api/users.py", ["def create():", "    pass"]),
            d_add("tests/test_api.py", ["from app.api.users import create"]), contents=contents),
        **USERS,
    )
    assert only(g, "tests/test_api.py") == ("SUPPORTING", "supporting.test_of_core")


def test_test_linked_to_intent_without_a_core_file():
    g, _ = run(ctx(d_add("tests/test_endpoint.py", ["def test_x():", "    pass"])), **USERS)
    assert only(g, "tests/test_endpoint.py") == ("SUPPORTING", "supporting.test_linked_to_intent")


def test_unrelated_test_is_untraced():
    g, _ = run(ctx(d_add("tests/test_billing.py", ["def test_x():", "    pass"])), **USERS)
    assert g["tests/test_billing.py"].group == "UNTRACED"


def test_config_packaging_ci_docs_and_init_are_supporting_by_type():
    g, _ = run(
        ctx(
            d_modify("pyproject.toml", ["a=1"], ["a=2"]),
            d_modify(".github/workflows/ci.yml", ["a: 1"], ["a: 2"]),
            d_modify("requirements.txt", ["x==1"], ["x==2"]),
            d_modify("docs/guide.md", ["old"], ["new text here"]),
            d_modify("app/__init__.py", ["a = 1"], ["a = 1", "from .x import y"]),
        ),
        **USERS,
    )
    assert only(g, "pyproject.toml") == ("SUPPORTING", "supporting.config")
    assert only(g, ".github/workflows/ci.yml") == ("SUPPORTING", "supporting.config")
    assert only(g, "requirements.txt") == ("SUPPORTING", "supporting.config")
    assert only(g, "docs/guide.md") == ("SUPPORTING", "supporting.docs")
    assert only(g, "app/__init__.py") == ("SUPPORTING", "supporting.wiring")


def test_python_file_importing_a_core_module_is_supporting():
    contents = {"app/routes.py": "from app.api import users\n"}
    g, _ = run(
        ctx(d_add("app/api/users.py", ["x = 1"]), d_add("app/routes.py", ["from app.api import users"]),
            contents=contents),
        **USERS,
    )
    assert only(g, "app/routes.py") == ("SUPPORTING", "supporting.imports_core")


def test_python_file_imported_by_a_core_file_is_supporting():
    contents = {"app/api/users.py": "from app.schemas import UserOut\n"}
    g, _ = run(
        ctx(d_add("app/api/users.py", ["from app.schemas import Payload"]),
            d_add("app/schemas.py", ["class Payload: ..."]), contents=contents),
        **USERS,
    )
    assert only(g, "app/schemas.py") == ("SUPPORTING", "supporting.imported_by_core")


def test_import_rule_needs_file_contents_and_degrades_to_untraced():
    g, _ = run(
        ctx(d_add("app/api/users.py", ["x = 1"]), d_add("app/routes.py", ["from app.api import users"])),
        **USERS,
    )
    assert g["app/routes.py"].group == "UNTRACED"


# --------------------------------------------------------------------------- #
# Rule 4 and the no-intent state
# --------------------------------------------------------------------------- #
def test_file_with_no_link_is_untraced_and_carries_a_reason():
    g, _ = run(ctx(d_add("utils/date.py", ["x = 1"])), **USERS)
    assert only(g, "utils/date.py") == ("UNTRACED", "untraced.no_link")


def test_without_intent_nothing_is_core_or_untraced():
    g, _ = run(
        ctx(d_add("app/api/users.py", ["x = 1"]), d_add("utils/date.py", ["y = 1"]),
            d_modify("pyproject.toml", ["a=1"], ["a=2"])),
    )
    assert only(g, "app/api/users.py") == ("UNCLASSIFIED", "unclassified.no_intent")
    assert only(g, "utils/date.py") == ("UNCLASSIFIED", "unclassified.no_intent")
    assert g["pyproject.toml"].group == "SUPPORTING"


def test_weak_intent_from_commit_subjects_is_treated_like_none():
    g, _ = run(ctx(d_add("app/api/users.py", ["x = 1"])), commit_subjects=["users endpoint"])
    assert g["app/api/users.py"].group == "UNCLASSIFIED"


def test_deleted_files_are_grouped_too():
    g, _ = run(ctx(d_delete("legacy/old.py", ["x = 1"])), **USERS)
    assert g["legacy/old.py"].group == "UNTRACED"


def test_first_matching_rule_wins_incidental_over_core():
    g, _ = run(ctx(d_modify("app/api/users.py", ["x=1"], ["x = 1"])), **USERS)
    assert g["app/api/users.py"].group == "INCIDENTAL"


def test_every_changed_file_gets_exactly_one_group():
    context = ctx(d_add("a.py", ["x = 1"]), d_add("b.py", ["y = 1"]), excluded=["uv.lock"])
    files, _ = group_files(context, build_intent(title="Add users endpoint"))
    assert sorted(f.path for f in files) == ["a.py", "b.py", "uv.lock"]


# --------------------------------------------------------------------------- #
# Honesty about coverage
# --------------------------------------------------------------------------- #
def test_non_python_files_are_noted_as_path_level_only():
    _, notes = run(ctx(d_add("web/app.ts", ["export const a = 1"]), d_add("m/x.py", ["x = 1"])), **USERS)
    assert any("path" in n and "typescript" in n for n in notes)


def test_config_and_docs_files_do_not_trigger_the_language_note():
    _, notes = run(ctx(d_modify("pyproject.toml", ["a=1"], ["a=2"]), d_modify("docs/a.md", ["x"], ["y z"])), **USERS)
    assert not any("path and extension" in n for n in notes)


def test_python_only_change_has_no_language_note():
    _, notes = run(ctx(d_add("m/x.py", ["x = 1"])), **USERS)
    assert not any("path and extension" in n for n in notes)


def test_missing_file_contents_are_noted():
    _, notes = run(ctx(d_add("m/x.py", ["x = 1"])), **USERS)
    assert any("file contents" in n for n in notes)


def test_no_intent_is_explained_in_notes():
    _, notes = run(ctx(d_add("m/x.py", ["x = 1"])))
    assert any("no declared intent" in n.lower() for n in notes)


# --------------------------------------------------------------------------- #
# Pipeline and JSON
# --------------------------------------------------------------------------- #
def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
                   cwd=repo, check=True, capture_output=True)


def test_pipeline_emits_groups_in_json(tmp_path):
    repo = tmp_path / "repo"
    (repo / "app/api").mkdir(parents=True)
    (repo / "utils").mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "README.md").write_text("hi\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "base")
    (repo / "app/api/users.py").write_text("def list_users():\n    return []\n")
    (repo / "utils/date.py").write_text("def today():\n    return 1\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "wip")
    settings = Settings.load(llm={"provider": "none"}, tools={"enabled": False})
    run_ = run_review(settings, repo_path=repo, base="HEAD~1", write=False, pr_title="Add users endpoint")
    payload = json.loads(json.dumps(run_.result.to_dict()))["comprehension"]
    by_path = {f["path"]: f for f in payload["files"]}
    assert by_path["app/api/users.py"]["group"] == "CORE"
    assert by_path["utils/date.py"]["group"] == "UNTRACED"
    assert payload["groups"]["CORE"] == 1 and payload["groups"]["UNTRACED"] == 1
    assert by_path["utils/date.py"]["reason"] == "untraced.no_link"
