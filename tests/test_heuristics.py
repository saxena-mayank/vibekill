"""Heuristic checks.

These run with no model and no external tools, so they are the floor of what vibekill
guarantees. Each test states the behaviour a contributor must not break.
"""

from __future__ import annotations

from conftest import make_context
from vibekill.analysis.heuristics import run_heuristics
from vibekill.models import Category, Severity


def rules(context) -> set[str]:
    return {f.rule_id for f in run_heuristics(context)}


def find(context, rule_id: str):
    return [f for f in run_heuristics(context) if f.rule_id == rule_id]


class TestSecurityPatterns:
    def test_flags_hardcoded_credential(self):
        context = make_context("app/conf.py", ['API_TOKEN = "sk_live_9f8a7b6c5d4e"'])
        matches = find(context, "vibekill.security.hardcoded_secret")
        assert matches and matches[0].severity is Severity.BLOCKER

    def test_ignores_interpolated_value(self):
        """Reading a secret from elsewhere is the correct pattern, not a finding."""
        context = make_context("app/conf.py", ['API_TOKEN = f"{os.environ[\'TOKEN\']}"'])
        assert "vibekill.security.hardcoded_secret" not in rules(context)

    def test_downgrades_secrets_in_test_files(self):
        """Fixtures contain fake credentials; reporting them as blockers is noise."""
        context = make_context("tests/test_auth.py", ['PASSWORD = "hunter2hunter2"'])
        matches = find(context, "vibekill.security.hardcoded_secret")
        assert matches and matches[0].severity is Severity.INFO

    def test_flags_sql_string_concatenation(self):
        context = make_context(
            "app/db.py", ['query = "SELECT * FROM users WHERE n = \'" + name + "\'"']
        )
        matches = find(context, "vibekill.security.sql_injection")
        assert matches and matches[0].category is Category.SECURITY
        assert matches[0].severity is Severity.BLOCKER

    def test_flags_sql_fstring(self):
        context = make_context("app/db.py", ['cur.execute(f"SELECT * FROM t WHERE id = {uid}")'])
        assert "vibekill.security.sql_injection" in rules(context)

    def test_ignores_parameterised_query(self):
        context = make_context(
            "app/db.py", ['cur.execute("SELECT * FROM users WHERE n = ?", (name,))']
        )
        assert "vibekill.security.sql_injection" not in rules(context)

    def test_flags_disabled_tls_verification(self):
        context = make_context("app/http.py", ["requests.get(url, verify=False)"])
        assert "vibekill.security.insecure_transport" in rules(context)

    def test_flags_shell_true(self):
        context = make_context("app/run.py", ['subprocess.run(cmd, shell=True)'])
        assert "vibekill.security.shell_injection_risk" in rules(context)


class TestExceptionHandling:
    def test_flags_silently_swallowed_exception(self):
        context = make_context(
            "app/x.py",
            ["def f():", "    try:", "        risky()", "    except Exception:", "        pass"],
        )
        matches = find(context, "vibekill.correctness.swallowed_exception")
        assert matches and matches[0].severity is Severity.MAJOR

    def test_flags_broad_except_that_does_something(self):
        context = make_context(
            "app/x.py",
            [
                "def f():",
                "    try:",
                "        risky()",
                "    except Exception as exc:",
                "        log(exc)",
            ],
        )
        assert "vibekill.correctness.broad_except" in rules(context)
        assert "vibekill.correctness.swallowed_exception" not in rules(context)

    def test_specific_exception_is_not_flagged(self):
        context = make_context(
            "app/x.py",
            ["def f():", "    try:", "        risky()", "    except ValueError:", "        return None"],
        )
        assert not {"vibekill.correctness.broad_except", "vibekill.correctness.swallowed_exception"} & rules(context)


class TestTestQuality:
    def test_flags_test_with_no_assertion(self):
        context = make_context("tests/test_a.py", ["def test_runs():", "    do_something()"])
        matches = find(context, "vibekill.testing.no_assertion")
        assert matches and matches[0].severity is Severity.MAJOR

    def test_flags_assert_true(self):
        context = make_context("tests/test_a.py", ["def test_a():", "    assert True"])
        assert "vibekill.testing.weak_assertion" in rules(context)

    def test_flags_not_none_assertion(self):
        context = make_context(
            "tests/test_a.py", ["def test_a():", "    result = build()", "    assert result is not None"]
        )
        assert "vibekill.testing.weak_assertion" in rules(context)

    def test_flags_isinstance_only_assertion(self):
        context = make_context(
            "tests/test_a.py", ["def test_a():", "    r = build()", "    assert isinstance(r, dict)"]
        )
        assert "vibekill.testing.weak_assertion" in rules(context)

    def test_real_assertion_is_not_flagged(self):
        context = make_context(
            "tests/test_a.py", ["def test_a():", "    assert build() == {'ok': True}"]
        )
        assert not {"vibekill.testing.weak_assertion", "vibekill.testing.no_assertion"} & rules(context)

    def test_pytest_raises_counts_as_an_assertion(self):
        context = make_context(
            "tests/test_a.py",
            ["def test_a():", "    with pytest.raises(ValueError):", "        build(None)"],
        )
        assert "vibekill.testing.no_assertion" not in rules(context)

    def test_non_test_file_is_not_checked_for_assertions(self):
        context = make_context("app/helper.py", ["def test_connection():", "    ping()"])
        assert "vibekill.testing.no_assertion" not in rules(context)


class TestComplexityAndRedundancy:
    def test_flags_unused_parameters(self):
        context = make_context(
            "app/x.py", ["def handler(request, locale, feature_flag):", "    return request.body"]
        )
        matches = find(context, "vibekill.redundancy.unused_parameter")
        assert matches
        assert "locale" in matches[0].title and "feature_flag" in matches[0].title

    def test_underscore_prefixed_parameter_is_allowed(self):
        context = make_context("app/x.py", ["def handler(request, _locale):", "    return request"])
        assert "vibekill.redundancy.unused_parameter" not in rules(context)

    def test_abstract_method_may_ignore_parameters(self):
        context = make_context(
            "app/x.py",
            ["class Base:", "    @abstractmethod", "    def run(self, payload, opts):", "        ..."],
        )
        assert "vibekill.redundancy.unused_parameter" not in rules(context)

    def test_flags_long_function(self):
        body = ["def big():"] + [f"    x{i} = {i}" for i in range(70)]
        context = make_context("app/x.py", body)
        assert "vibekill.complexity.long_function" in rules(context)

    def test_flags_deep_nesting(self):
        lines = ["def f(a):"]
        for depth in range(6):
            lines.append("    " * (depth + 1) + f"if a > {depth}:")
        lines.append("    " * 7 + "return a")
        context = make_context("app/x.py", lines)
        assert "vibekill.complexity.deep_nesting" in rules(context)

    def test_flags_too_many_parameters(self):
        context = make_context(
            "app/x.py", ["def f(a, b, c, d, e, f_, g):", "    return (a, b, c, d, e, f_, g)"]
        )
        assert "vibekill.complexity.too_many_parameters" in rules(context)

    def test_detects_duplicated_block(self):
        block = [
            "    conn = connect(path)",
            "    cur = conn.cursor()",
            "    cur.execute(sql)",
            "    rows = cur.fetchall()",
            "    conn.close()",
            "    return rows",
        ]
        context = make_context("app/x.py", ["def a():", *block, "def b():", *block])
        matches = find(context, "vibekill.redundancy.duplicate_block")
        assert matches, "an exact copy-paste of six statements should be reported"

    def test_interleaved_comments_do_not_hide_duplication(self):
        """Generated code narrates every line; that must not defeat detection."""
        block = [
            "    conn = connect(path)",
            "    cur = conn.cursor()",
            "    cur.execute(sql)",
            "    rows = cur.fetchall()",
            "    conn.close()",
            "    return rows",
        ]
        commented: list[str] = []
        for line in block:
            commented.extend([f"    # {line.strip()}", line])
        context = make_context("app/x.py", ["def a():", *commented, "def b():", *block])
        assert "vibekill.redundancy.duplicate_block" in rules(context)

    def test_detects_cross_file_duplication(self):
        from conftest import build_diff
        from vibekill.diffparse import parse_unified_diff
        from vibekill.models import RepoMeta, ReviewContext

        block = [
            "def normalise(value):",
            "    cleaned = value.strip().lower()",
            "    cleaned = cleaned.replace('-', '_')",
            "    if not cleaned:",
            "        raise ValueError('empty')",
            "    return cleaned",
        ]
        files = parse_unified_diff(build_diff("a/one.py", block) + build_diff("a/two.py", block))
        for file_diff in files:
            file_diff.new_content = "\n".join(block) + "\n"
        context = ReviewContext(meta=RepoMeta(root="/tmp/x"), files=files)
        assert "vibekill.redundancy.cross_file_duplicate" in rules(context)

    def test_flags_comment_heavy_additions(self):
        lines: list[str] = []
        for i in range(20):
            lines.extend([f"# set value {i}", f"value_{i} = {i}", f"# done with {i}"])
        context = make_context("app/x.py", lines)
        assert "vibekill.complexity.comment_ratio" in rules(context)


class TestNoise:
    def test_flags_debug_print(self):
        context = make_context("app/x.py", ['print("here")'])
        assert "vibekill.debug.leftover_print" in rules(context)

    def test_allows_print_in_tests(self):
        context = make_context("tests/test_x.py", ['print("debugging")'])
        assert "vibekill.debug.leftover_print" not in rules(context)

    def test_flags_new_todo(self):
        context = make_context("app/x.py", ["# TODO: handle the error case"])
        matches = find(context, "vibekill.quality.todo_added")
        assert matches and matches[0].severity is Severity.INFO


class TestRobustness:
    def test_syntax_error_is_reported_not_raised(self):
        context = make_context("app/x.py", ["def broken(:", "    pass"])
        matches = find(context, "vibekill.correctness.syntax_error")
        assert matches and matches[0].severity is Severity.BLOCKER

    def test_non_python_files_do_not_break_ast_checks(self):
        context = make_context("web/app.ts", ["const x: any = 1;", "console.log(x);"])
        run_heuristics(context)  # must not raise

    def test_clean_code_produces_no_findings(self):
        context = make_context(
            "app/clean.py",
            [
                "def add(left: int, right: int) -> int:",
                '    """Return the sum."""',
                "    return left + right",
            ],
        )
        assert run_heuristics(context) == []


def test_explicit_dummy_secret_fixture_is_not_reported():
    context = make_context("demo.py", ['DUMMY_TOKEN = "sk_live_1234567890"'])
    assert "vibekill.security.hardcoded_secret" not in rules(context)


def test_embedded_print_in_yaml_is_not_debug_output():
    context = make_context("workflow.yml", ['run: python -c "print(123)"'])
    assert "vibekill.debug.leftover_print" not in rules(context)


def test_duplicate_check_ignores_configuration_files():
    lines = [f"KEY_{index}=value" for index in range(6)] * 2
    context = make_context("example.env", lines)
    assert "vibekill.redundancy.duplicate_block" not in rules(context)
