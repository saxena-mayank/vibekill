"""Step 3: deterministic scope signals.

Every signal is advisory. Each test pairs a case that must fire with one on reasonable
code that must not.
"""
from __future__ import annotations

import json

from diffs import ctx, d_add, d_modify, d_rename
from vibekill.models import Signal
from vibekill.pipeline import run_review
from vibekill.scope.grouping import group_files
from vibekill.scope.intent import build_intent
from vibekill.scope.signals import compute_signals
from vibekill.settings import Settings


def signals(context, *, title="Add users endpoint", max_files=10, max_lines=400) -> list[Signal]:
    intent = build_intent(title=title)
    groups, _ = group_files(context, intent)
    return compute_signals(context, groups, max_files=max_files, max_lines=max_lines)


def codes(found: list[Signal], path: str | None = None) -> list[str]:
    return sorted(s.code for s in found if path is None or s.path == path)


USERS = ["def list_users():", "    return []"]
DATE = ["def today():", "    return 1"]


# --------------------------------------------------------------------------- #
# Carried over from grouping
# --------------------------------------------------------------------------- #
def test_incidental_reasons_become_signals():
    found = signals(
        ctx(
            d_modify("a/fmt.py", ["x=1"], ["x = 1"]),
            d_rename("a/old.py", "a/new.py"),
        )
    )
    assert codes(found, "a/fmt.py") == ["formatting_only"]
    assert codes(found, "a/new.py") == ["pure_rename"]


def test_moved_code_becomes_a_signal():
    block = ["def helper(a):", "    b = a + 1", "    c = b * 2", "    return c"]
    found = signals(ctx(d_modify("m/old.py", block, ["keep = 1"]), d_add("m/new.py", block)))
    assert codes(found, "m/new.py") == ["moved_code"]


# --------------------------------------------------------------------------- #
# isolated_file
# --------------------------------------------------------------------------- #
def test_untraced_python_file_with_no_link_to_other_changed_files_is_isolated():
    found = signals(ctx(d_add("app/users.py", USERS), d_add("utils/date.py", DATE), with_content=True))
    assert codes(found, "utils/date.py") == ["isolated_file"]
    assert codes(found, "app/users.py") == []  # core files are not questioned


def test_a_file_imported_by_another_changed_file_is_not_isolated():
    found = signals(
        ctx(
            d_add("app/users.py", ["from utils.date import today", *USERS]),
            d_add("utils/date.py", DATE),
            with_content=True,
        )
    )
    assert "isolated_file" not in codes(found, "utils/date.py")


def test_a_file_whose_function_is_used_elsewhere_is_not_isolated():
    found = signals(
        ctx(
            d_add("app/users.py", ["def list_users():", "    return today()"]),
            d_add("utils/date.py", DATE),
            with_content=True,
        )
    )
    assert "isolated_file" not in codes(found, "utils/date.py")


def test_a_lone_python_file_cannot_be_isolated():
    assert codes(signals(ctx(d_add("utils/date.py", DATE), with_content=True))) == []


def test_isolation_needs_file_contents():
    assert "isolated_file" not in codes(signals(ctx(d_add("app/users.py", USERS), d_add("utils/date.py", DATE))))


def test_isolation_is_not_reported_for_tests_or_config():
    found = signals(
        ctx(
            d_add("app/users.py", USERS),
            d_add("tests/test_other.py", ["def test_a():", "    pass"]),
            d_modify("pyproject.toml", ["a=1"], ["a=2"]),
            with_content=True,
        )
    )
    assert "isolated_file" not in codes(found)


# --------------------------------------------------------------------------- #
# single_implementation
# --------------------------------------------------------------------------- #
ABSTRACT = ["from abc import ABC, abstractmethod", "class Store(ABC):", "    @abstractmethod", "    def get(self): ..."]


def test_new_abstract_class_with_one_implementation_is_flagged():
    found = signals(ctx(d_add("app/store.py", [*ABSTRACT, "class MemoryStore(Store):", "    def get(self): return 1"]),
                        with_content=True))
    assert codes(found, "app/store.py") == ["single_implementation"]
    assert "Store" in next(s for s in found if s.code == "single_implementation").message


def test_new_abstract_class_with_two_implementations_is_not_flagged():
    body = [*ABSTRACT, "class A(Store):", "    def get(self): return 1", "class B(Store):", "    def get(self): return 2"]
    assert codes(signals(ctx(d_add("app/store.py", body), with_content=True))) == []


def test_implementations_in_other_changed_files_are_counted():
    found = signals(
        ctx(
            d_add("app/store.py", ABSTRACT),
            d_add("app/a.py", ["from app.store import Store", "class A(Store):", "    def get(self): return 1"]),
            d_add("app/b.py", ["from app.store import Store", "class B(Store):", "    def get(self): return 2"]),
            with_content=True,
        )
    )
    assert "single_implementation" not in codes(found)


def test_protocol_with_no_implementation_is_flagged():
    found = signals(ctx(d_add("app/p.py", ["from typing import Protocol", "class Sender(Protocol):", "    def send(self): ..."]),
                        with_content=True))
    assert codes(found) == ["single_implementation"]


def test_a_pre_existing_abstract_class_is_not_flagged():
    content = "\n".join([*ABSTRACT, "x = 2"]) + "\n"
    diff = d_modify("app/store.py", ["x = 1"], ["x = 2"], before=ABSTRACT)
    assert codes(signals(ctx(diff, contents={"app/store.py": content}))) == []


def test_new_factory_that_builds_one_kind_of_object_is_flagged():
    body = ["class Repo:", "    pass", "class UserRepositoryFactory:", "    def create(self):", "        return Repo()"]
    found = signals(ctx(d_add("app/factory.py", body), with_content=True))
    assert codes(found, "app/factory.py") == ["single_implementation"]


def test_factory_that_builds_two_kinds_is_not_flagged():
    body = ["class A: ...", "class B: ...", "class WidgetFactory:", "    def make(self, kind):",
            "        return A() if kind else B()"]
    assert codes(signals(ctx(d_add("app/f.py", body), with_content=True))) == []


# --------------------------------------------------------------------------- #
# duplicate_function
# --------------------------------------------------------------------------- #
BODY = ["    total = 0", "    for x in items:", "        total += x * 2", "    return total"]


def test_new_function_with_the_same_body_as_another_changed_function_is_flagged():
    found = signals(
        ctx(
            d_add("a/one.py", ["def double_sum(items):", *BODY]),
            d_add("b/two.py", ["def twice_total(items):", *BODY]),
            with_content=True,
        )
    )
    assert "duplicate_function" in codes(found, "a/one.py")
    assert "twice_total" in next(s for s in found if s.path == "a/one.py" and s.code == "duplicate_function").message


def test_functions_with_different_bodies_are_not_duplicates():
    other = ["    total = 1", "    for x in items:", "        total += x * 3", "    return total"]
    found = signals(
        ctx(d_add("a/one.py", ["def a(items):", *BODY]), d_add("b/two.py", ["def b(items):", *other]),
            with_content=True)
    )
    assert "duplicate_function" not in codes(found)


def test_tiny_functions_are_not_duplicates():
    found = signals(ctx(d_add("a/one.py", ["def a():", "    return 1"]), d_add("b/two.py", ["def b():", "    return 1"]),
                        with_content=True))
    assert "duplicate_function" not in codes(found)


def test_functions_in_test_files_are_ignored():
    found = signals(
        ctx(d_add("tests/test_a.py", ["def test_a(items):", *BODY]),
            d_add("tests/test_b.py", ["def test_b(items):", *BODY]), with_content=True)
    )
    assert "duplicate_function" not in codes(found)


def test_an_unchanged_function_is_not_reported_even_when_it_matches():
    content = "\n".join(["def old(items):", *BODY, "x = 2"]) + "\n"
    found = signals(
        ctx(d_modify("a/one.py", ["x = 1"], ["x = 2"], before=["def old(items):", *BODY]),
            d_add("b/two.py", ["def new(items):", *BODY]), contents={"a/one.py": content}, with_content=True)
    )
    assert "duplicate_function" not in codes(found, "a/one.py")
    assert "duplicate_function" in codes(found, "b/two.py")


# --------------------------------------------------------------------------- #
# size_vs_intent
# --------------------------------------------------------------------------- #
def _many(n: int):
    return ctx(*(d_add(f"m/f{i}.py", ["x = 1"]) for i in range(n)), with_content=True)


def test_small_change_has_no_size_signal():
    assert "size_vs_intent" not in codes(signals(_many(3)))


def test_many_files_trigger_a_size_signal_with_numbers_not_judgement():
    found = signals(_many(12), max_files=10)
    size = next(s for s in found if s.code == "size_vs_intent")
    assert size.path is None
    assert "12 files" in size.message and "10" in size.message
    assert "too" not in size.message.lower()


def test_size_signal_reports_how_many_files_are_untraced():
    found = signals(_many(12), max_files=10)
    size = next(s for s in found if s.code == "size_vs_intent")
    assert "12 of 12" in size.message and "no visible link" in size.message


def test_many_lines_trigger_a_size_signal():
    big = ctx(d_add("app/users.py", [f"x{i} = {i}" for i in range(50)]), with_content=True)
    assert "size_vs_intent" in codes(signals(big, max_lines=40))
    assert "size_vs_intent" not in codes(signals(big, max_lines=400))


# --------------------------------------------------------------------------- #
# Pipeline and guarantees
# --------------------------------------------------------------------------- #
def test_signals_are_advisory_and_do_not_change_findings_or_verdict(tmp_path):
    import subprocess

    repo = tmp_path / "r"
    repo.mkdir()
    run = lambda *a: subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a],  # noqa: E731
                                    cwd=repo, check=True, capture_output=True)
    run("init", "-q", "-b", "main")
    (repo / "a.py").write_text("x = 1\n")
    run("add", ".")
    run("commit", "-q", "-m", "base")
    (repo / "b.py").write_text("def f():\n    return 1\n")
    (repo / "c.py").write_text("def g():\n    return 2\n")
    run("add", ".")
    run("commit", "-q", "-m", "wip")

    def go(**scope):
        s = Settings.load(llm={"provider": "none"}, tools={"enabled": False}, scope=scope)
        return run_review(s, repo_path=repo, base="HEAD~1", write=False, pr_title="Add users endpoint")

    on, off = go(enabled=True, max_files_advisory=1), go(enabled=False)
    assert on.result.verdict == off.result.verdict
    assert [f.fingerprint() for f in on.result.findings] == [f.fingerprint() for f in off.result.findings]
    payload = json.loads(json.dumps(on.result.to_dict()))["comprehension"]
    assert any(s["code"] == "size_vs_intent" for s in payload["signals"])
    assert all(set(s) == {"code", "message", "path", "line", "evidence"} for s in payload["signals"])


def test_signal_thresholds_have_defaults_and_validate():
    s = Settings.load(llm={"provider": "none"})
    assert s.scope.max_files_advisory == 10 and s.scope.max_lines_advisory == 400
