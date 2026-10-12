"""Built-in analysers. No external binaries, no model, no network.

These matter more than they look. In a locked-down environment nobody can
`pip install semgrep`, so this module is often the *only* analyser present. It
therefore has to earn its keep on its own, and the checks are chosen for high
precision: every one of them either is a defect or is worth a human glance.

Findings produced here carry `source="heuristic"` and high confidence, and the
agent layer receives them as evidence. Two categories are deliberately
emphasised because they are what AI-generated code gets wrong most often and
human reviewers catch least:

  * swallowed errors and assertion-free tests
  * volume: duplicated blocks, deep nesting, oversized functions, dead params

Each check is a small function registered in `CHECKS`. Adding one is a
self-contained contribution; see docs/CONTRIBUTING.md.
"""

from __future__ import annotations

import ast
import hashlib
import re
from collections.abc import Callable, Iterable

from ..models import Category, FileDiff, Finding, ReviewContext, Severity

# Tunables. Exposed as module constants so a fork can adjust policy without
# touching logic.
MAX_FUNCTION_LINES = 60
MAX_NESTING_DEPTH = 5
MAX_PARAMETERS = 6
DUPLICATE_WINDOW_LINES = 6
MIN_DUPLICATE_TOKENS = 40
HIGH_COMMENT_RATIO = 0.55

CODE_LANGUAGES = {
    "python", "javascript", "typescript", "go", "rust", "java", "kotlin",
    "ruby", "php", "csharp", "swift", "scala", "c", "cpp", "shell",
    "powershell", "elixir", "erlang", "clojure", "lua", "dart", "r", "perl",
}

CheckFn = Callable[[FileDiff, ReviewContext], list[Finding]]
CHECKS: list[tuple[str, CheckFn]] = []


def check(name: str) -> Callable[[CheckFn], CheckFn]:
    def register(fn: CheckFn) -> CheckFn:
        CHECKS.append((name, fn))
        return fn

    return register


def run_heuristics(context: ReviewContext) -> list[Finding]:
    """Run every registered check over every reviewable file."""
    findings: list[Finding] = []
    for file_diff in context.reviewable_files():
        for name, fn in CHECKS:
            try:
                findings.extend(fn(file_diff, context))
            except Exception as exc:  # pragma: no cover - a check must never abort a review
                findings.append(
                    Finding(
                        title=f"Heuristic '{name}' failed",
                        body=f"Internal error while analysing this file: {type(exc).__name__}: {exc}",
                        category=Category.QUALITY,
                        severity=Severity.INFO,
                        path=file_diff.path,
                        agent="heuristics",
                        source="heuristic",
                        rule_id=f"vibekill.internal.{name}",
                        confidence=1.0,
                    )
                )
    findings.extend(_cross_file_duplicates(context))
    return findings


def _finding(
    file_diff: FileDiff,
    *,
    rule: str,
    title: str,
    body: str,
    category: Category,
    severity: Severity,
    line: int | None = None,
    end_line: int | None = None,
    suggestion: str = "",
    confidence: float = 0.9,
    tags: Iterable[str] = (),
) -> Finding:
    evidence = (file_diff.line_at(line) or "").strip() if line else ""
    return Finding(
        title=title,
        body=body,
        category=category,
        severity=severity,
        path=file_diff.path,
        line=line,
        end_line=end_line,
        evidence=evidence,
        suggestion=suggestion,
        agent="heuristics",
        source="heuristic",
        rule_id=f"vibekill.{rule}",
        confidence=confidence,
        tags=list(tags),
    )


# --------------------------------------------------------------------------- #
# Language-agnostic checks (regex over added lines only)
# --------------------------------------------------------------------------- #

# (rule, pattern, title, body, category, severity)
_LINE_PATTERNS: list[tuple[str, re.Pattern[str], str, str, Category, Severity]] = [
    (
        "debug.leftover_print",
        re.compile(r"^\s*(?:print\s*\(|console\.(?:log|debug)\s*\(|fmt\.Print(?:ln|f)?\s*\()"),
        "Debug output left in the change",
        "Debug printing reached the diff. Use the project's logger, or remove it.",
        Category.QUALITY,
        Severity.MINOR,
    ),
    (
        "quality.todo_added",
        re.compile(r"(?://|#|/\*|\*)\s*(TODO|FIXME|XXX|HACK)\b", re.IGNORECASE),
        "New TODO/FIXME introduced",
        "The change adds a TODO marker. Either resolve it or link it to a tracked issue, "
        "otherwise it becomes permanent.",
        Category.QUALITY,
        Severity.INFO,
    ),
    (
        "security.hardcoded_secret",
        # An assigned literal of real length. Interpolations ($, {) are excluded
        # because those are almost always reading the value from somewhere else.
        re.compile(
            r"(?i)(?:password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key"
            r"|private[_-]?key|credential)\s*[:=]\s*"
            r"""['"][^'"\s${}]{8,}['"]"""
        ),
        "Possible hardcoded credential",
        "A literal that looks like a credential is assigned in source. Move it to an "
        "environment variable or a secrets manager. If this is a test fixture, name it so "
        "that is obvious (for example `DUMMY_TOKEN`).",
        Category.SECURITY,
        Severity.BLOCKER,
    ),
    (
        "security.insecure_transport",
        re.compile(r"""(?ix) (?:verify\s*=\s*False | rejectUnauthorized\s*:\s*false | InsecureSkipVerify\s*:\s*true)"""),
        "TLS verification disabled",
        "Certificate verification is switched off, which removes the protection TLS "
        "provides against interception.",
        Category.SECURITY,
        Severity.BLOCKER,
    ),
    (
        "security.dangerous_eval",
        re.compile(r"""(?ix) (?:\beval\s*\( | \bexec\s*\( | new\s+Function\s*\( | \bsetTimeout\s*\(\s*['"])"""),
        "Dynamic code execution",
        "Evaluating code at runtime turns any tainted input into remote code execution. "
        "Replace it with an explicit dispatch table or a parser.",
        Category.SECURITY,
        Severity.MAJOR,
    ),
    (
        # A SQL keyword on the same line as string building. Concatenation, %,
        # .format() and f-string interpolation all reach the same sink.
        "security.sql_injection",
        # Two independent conditions rather than one ordered pattern: a SQL
        # keyword anywhere on the line, and an interpolation marker anywhere on
        # the line. Requiring the marker to follow the keyword misses f-strings,
        # where the `f"` prefix comes first.
        re.compile(
            r"(?i)"
            r"(?=.*(?:select\s|insert\s+into|update\s+\w+\s+set|delete\s+from|where\s))"
            r"""(?=.*(?:["']\s*\+|\+\s*["']|%\s*\(?\w|\.format\s*\(|\{\w))"""
        ),
        "SQL query built by string interpolation",
        "This query is assembled from a string rather than parameterised, so any value "
        "reaching it becomes part of the SQL. If that value can come from a request, a "
        "header, or a filename, this is an injection. Pass values as query parameters "
        "instead: `cur.execute('... WHERE name = ?', (name,))`.",
        Category.SECURITY,
        Severity.BLOCKER,
    ),
    (
        "security.shell_injection_risk",
        re.compile(r"(?i)(?:subprocess\.\w+\([^)]*shell\s*=\s*True|os\.system\s*\(|child_process\.exec\s*\()"),
        "Shell invocation from application code",
        "Running a command through a shell makes argument quoting a security boundary. "
        "Pass an argument list instead of a shell string.",
        Category.SECURITY,
        Severity.MAJOR,
    ),
]


@check("line_patterns")
def check_line_patterns(file_diff: FileDiff, _context: ReviewContext) -> list[Finding]:
    """Regex checks over added lines. Cheap, language-agnostic, high precision."""
    findings: list[Finding] = []
    is_test = file_diff.is_test_file
    for hunk in file_diff.hunks:
        for line in hunk.lines:
            if line.kind != "add" or not line.content.strip():
                continue
            for rule, pattern, title, body, category, severity in _LINE_PATTERNS:
                if not pattern.search(line.content):
                    continue
                if rule == "debug.leftover_print" and file_diff.language not in CODE_LANGUAGES:
                    continue
                if rule == "security.hardcoded_secret" and re.search(
                    r"(?i)(?:dummy|fake|example|test)[_-]?(?:password|secret|token|key)",
                    line.content,
                ):
                    continue
                # Test files legitimately contain fake credentials and debug
                # output; flagging them is how a reviewer bot loses credibility.
                effective = severity
                if is_test and category is Category.SECURITY:
                    effective = Severity.INFO
                elif is_test and rule == "debug.leftover_print":
                    continue
                findings.append(
                    _finding(
                        file_diff,
                        rule=rule,
                        title=title,
                        body=body,
                        category=category,
                        severity=effective,
                        line=line.new_lineno,
                        confidence=0.75 if category is Category.SECURITY else 0.9,
                        tags=["test-file"] if is_test else [],
                    )
                )
    return findings


@check("comment_ratio")
def check_comment_ratio(file_diff: FileDiff, _context: ReviewContext) -> list[Finding]:
    """Flag added code blocks that are mostly narration comments."""
    if file_diff.language not in CODE_LANGUAGES:
        return []
    added = [ln.content.strip() for h in file_diff.hunks for ln in h.lines if ln.kind == "add"]
    code = [ln for ln in added if ln]
    if len(code) < 25:
        return []
    markers = ("#", "//", "/*", "*", '"""', "'''")
    comments = [ln for ln in code if ln.startswith(markers)]
    ratio = len(comments) / len(code)
    if ratio < HIGH_COMMENT_RATIO:
        return []
    first = next((ln.new_lineno for h in file_diff.hunks for ln in h.lines if ln.kind == "add"), None)
    return [
        _finding(
            file_diff,
            rule="complexity.comment_ratio",
            title=f"{ratio:.0%} of added lines are comments",
            body=(
                f"{len(comments)} of {len(code)} added lines are comments. Comments that restate "
                "the code add maintenance weight without adding information: they drift out of "
                "date and make the real logic harder to find. Keep the ones explaining *why*."
            ),
            category=Category.COMPLEXITY,
            severity=Severity.INFO,
            line=first,
            confidence=0.8,
        )
    ]


@check("duplicate_blocks_in_file")
def check_duplicate_blocks(file_diff: FileDiff, _context: ReviewContext) -> list[Finding]:
    """Detect repeated blocks among added lines within one file.

    Normalises whitespace and string/number literals before hashing a sliding
    window, so near-identical copies with different constants still match.
    """
    if file_diff.language not in CODE_LANGUAGES:
        return []
    added = _code_lines(file_diff)
    if len(added) < DUPLICATE_WINDOW_LINES * 2:
        return []

    seen: dict[str, int] = {}
    findings: list[Finding] = []
    reported: set[str] = set()
    for index in range(len(added) - DUPLICATE_WINDOW_LINES + 1):
        window = added[index : index + DUPLICATE_WINDOW_LINES]
        normalised = " ".join(_normalise_code(text) for _, text in window)
        if len(normalised) < MIN_DUPLICATE_TOKENS:
            continue
        digest = hashlib.sha1(normalised.encode()).hexdigest()
        if digest in seen and digest not in reported:
            reported.add(digest)
            first_line, current_line = seen[digest], window[0][0]
            findings.append(
                _finding(
                    file_diff,
                    rule="redundancy.duplicate_block",
                    title=f"Duplicated block, also at line {first_line}",
                    body=(
                        f"These {DUPLICATE_WINDOW_LINES} lines repeat the block starting at line "
                        f"{first_line} (ignoring whitespace and literals). Two copies means two "
                        "places to fix when the logic changes, and one of them will be missed."
                    ),
                    category=Category.REDUNDANCY,
                    severity=Severity.MINOR,
                    line=current_line,
                    end_line=window[-1][0],
                    suggestion="Extract the shared logic into one function and call it twice.",
                    confidence=0.7,
                )
            )
        seen.setdefault(digest, window[0][0] or 0)
    return findings[:5]


def _normalise_code(text: str) -> str:
    """Collapse whitespace and literals so cosmetic differences stop mattering."""
    text = re.sub(r"""(['"])(?:\\.|(?!\1).)*\1""", "S", text)
    text = re.sub(r"\b\d+(?:\.\d+)?\b", "N", text)
    return " ".join(text.split())


_COMMENT_PREFIXES = ("#", "//", "/*", "*/", "*")


def _is_comment_line(text: str) -> bool:
    stripped = text.strip()
    return bool(stripped) and stripped.startswith(_COMMENT_PREFIXES)


def _code_lines(file_diff: FileDiff) -> list[tuple[int | None, str]]:
    """Added lines with blanks and comment-only lines removed.

    Comment-only lines are excluded because they do not change behaviour, and
    leaving them in lets a copy-pasted block escape duplicate detection simply by
    having a narration line inserted between each statement — which is exactly
    what generated code tends to look like.
    """
    return [
        (ln.new_lineno, ln.content)
        for hunk in file_diff.hunks
        for ln in hunk.lines
        if ln.kind == "add" and ln.content.strip() and not _is_comment_line(ln.content)
    ]


def _cross_file_duplicates(context: ReviewContext) -> list[Finding]:
    """Spot the same new block landing in two different files.

    Copy-paste across files is the most common way generated code accumulates
    volume, and it is invisible when reviewing file by file.
    """
    index: dict[str, tuple[str, int]] = {}
    findings: list[Finding] = []
    for file_diff in context.reviewable_files():
        if file_diff.language not in CODE_LANGUAGES:
            continue
        added = _code_lines(file_diff)
        if len(added) < DUPLICATE_WINDOW_LINES:
            continue
        local: set[str] = set()
        for i in range(len(added) - DUPLICATE_WINDOW_LINES + 1):
            window = added[i : i + DUPLICATE_WINDOW_LINES]
            normalised = " ".join(_normalise_code(text) for _, text in window)
            if len(normalised) < MIN_DUPLICATE_TOKENS:
                continue
            digest = hashlib.sha1(normalised.encode()).hexdigest()
            if digest in local:
                continue
            local.add(digest)
            if digest in index and index[digest][0] != file_diff.path:
                other_path, other_line = index[digest]
                findings.append(
                    _finding(
                        file_diff,
                        rule="redundancy.cross_file_duplicate",
                        title=f"Block duplicated from {other_path}:{other_line}",
                        body=(
                            f"This block also appears in `{other_path}` at line {other_line}. "
                            "Copy-paste between files is the usual way a change grows larger than "
                            "the problem it solves. Consider a shared helper."
                        ),
                        category=Category.REDUNDANCY,
                        severity=Severity.MINOR,
                        line=window[0][0],
                        end_line=window[-1][0],
                        confidence=0.7,
                    )
                )
                break
            index.setdefault(digest, (file_diff.path, window[0][0] or 0))
    return findings[:5]


# --------------------------------------------------------------------------- #
# Python AST checks
# --------------------------------------------------------------------------- #


@check("python_ast")
def check_python_ast(file_diff: FileDiff, _context: ReviewContext) -> list[Finding]:
    """Structural checks that need a real parse.

    Only applied to Python, and only to functions the diff actually touched, so
    a small change to a large legacy file does not produce a wall of complaints
    about code the author never saw.
    """
    if file_diff.language != "python" or not file_diff.new_content:
        return []
    try:
        tree = ast.parse(file_diff.new_content)
    except SyntaxError as exc:
        return [
            _finding(
                file_diff,
                rule="correctness.syntax_error",
                title="File does not parse",
                body=f"Python could not parse this file: {exc.msg} at line {exc.lineno}.",
                category=Category.CORRECTNESS,
                severity=Severity.BLOCKER,
                line=exc.lineno,
                confidence=1.0,
            )
        ]

    changed = file_diff.added_line_numbers()
    findings: list[Finding] = []
    findings.extend(_python_exception_handling(file_diff, tree, changed))
    findings.extend(_python_function_shape(file_diff, tree, changed))
    if file_diff.is_test_file:
        findings.extend(_python_test_quality(file_diff, tree, changed))
    return findings


def _touches(node: ast.AST, changed: set[int]) -> bool:
    start = getattr(node, "lineno", None)
    end = getattr(node, "end_lineno", None) or start
    if start is None:
        return False
    return any(start <= line <= end for line in changed)


def _is_noop_statement(node: ast.stmt) -> bool:
    """`pass` or a bare `...`, the two ways to write an empty block."""
    if isinstance(node, ast.Pass):
        return True
    return (
        isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and node.value.value is Ellipsis
    )


def _python_exception_handling(
    file_diff: FileDiff, tree: ast.AST, changed: set[int]
) -> list[Finding]:
    findings: list[Finding] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler) or not _touches(node, changed):
            continue
        body = node.body
        only_pass = len(body) == 1 and _is_noop_statement(body[0])
        bare = node.type is None
        broad = isinstance(node.type, ast.Name) and node.type.id in ("Exception", "BaseException")

        if only_pass:
            findings.append(
                _finding(
                    file_diff,
                    rule="correctness.swallowed_exception",
                    title="Exception silently swallowed",
                    body=(
                        "This handler catches an exception and does nothing. The failure becomes "
                        "invisible: the caller proceeds as though the operation succeeded, and the "
                        "eventual bug report will point somewhere unrelated."
                    ),
                    category=Category.CORRECTNESS,
                    severity=Severity.MAJOR,
                    line=node.lineno,
                    end_line=node.end_lineno,
                    suggestion="Log with context and re-raise, or handle the specific error you expect.",
                    confidence=0.95,
                )
            )
        elif bare or broad:
            what = "bare `except:`" if bare else f"`except {node.type.id}`"  # type: ignore[union-attr]
            findings.append(
                _finding(
                    file_diff,
                    rule="correctness.broad_except",
                    title=f"Overly broad {what}",
                    body=(
                        f"{what} also catches programming errors such as `AttributeError` and "
                        "`TypeError`, which turns a crash you could have fixed into behaviour you "
                        "will have to debug. Catch the specific exceptions you expect."
                    ),
                    category=Category.CORRECTNESS,
                    severity=Severity.MINOR,
                    line=node.lineno,
                    end_line=node.end_lineno,
                    confidence=0.85,
                )
            )
    return findings


def _python_function_shape(file_diff: FileDiff, tree: ast.AST, changed: set[int]) -> list[Finding]:
    """Size, nesting depth, parameter count, and unused parameters."""
    findings: list[Finding] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not _touches(node, changed):
            continue

        length = (node.end_lineno or node.lineno) - node.lineno + 1
        if length > MAX_FUNCTION_LINES:
            findings.append(
                _finding(
                    file_diff,
                    rule="complexity.long_function",
                    title=f"`{node.name}` is {length} lines long",
                    body=(
                        f"`{node.name}` spans {length} lines, past the {MAX_FUNCTION_LINES}-line "
                        "threshold. Long functions are hard to test in isolation, which usually "
                        "means they end up tested only through their callers."
                    ),
                    category=Category.COMPLEXITY,
                    severity=Severity.MINOR,
                    line=node.lineno,
                    end_line=node.end_lineno,
                    suggestion="Split the distinct responsibilities into named helpers.",
                    confidence=0.9,
                )
            )

        depth = _max_nesting(node)
        if depth > MAX_NESTING_DEPTH:
            findings.append(
                _finding(
                    file_diff,
                    rule="complexity.deep_nesting",
                    title=f"`{node.name}` nests {depth} levels deep",
                    body=(
                        f"Control flow in `{node.name}` reaches {depth} levels. Each level is "
                        "another condition the reader has to hold in their head at once."
                    ),
                    category=Category.COMPLEXITY,
                    severity=Severity.MINOR,
                    line=node.lineno,
                    suggestion="Return early on the failure cases to flatten the happy path.",
                    confidence=0.9,
                )
            )

        args = node.args
        names = [a.arg for a in (*args.posonlyargs, *args.args, *args.kwonlyargs)]
        positional = [n for n in names if n not in ("self", "cls")]
        if len(positional) > MAX_PARAMETERS:
            findings.append(
                _finding(
                    file_diff,
                    rule="complexity.too_many_parameters",
                    title=f"`{node.name}` takes {len(positional)} parameters",
                    body=(
                        f"{len(positional)} parameters makes call sites hard to read and easy to "
                        "get wrong when two share a type. Consider grouping related ones into a "
                        "dataclass."
                    ),
                    category=Category.COMPLEXITY,
                    severity=Severity.INFO,
                    line=node.lineno,
                    confidence=0.85,
                )
            )

        # Parameters nothing reads: a common artefact of generated code that was
        # written for a signature somebody imagined rather than one anybody calls.
        used = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
        used |= {n.attr for n in ast.walk(node) if isinstance(n, ast.Attribute)}
        unused = [
            n
            for n in positional
            if n not in used and not n.startswith("_") and not _is_abstract(node)
        ]
        if unused and len(unused) < len(positional):
            findings.append(
                _finding(
                    file_diff,
                    rule="redundancy.unused_parameter",
                    title=f"`{node.name}` ignores {', '.join(f'`{u}`' for u in unused)}",
                    body=(
                        "These parameters are never read in the function body. Either the logic "
                        "that should use them is missing, or they are dead weight every caller has "
                        "to keep supplying. Prefix with `_` if the signature is fixed by an interface."
                    ),
                    category=Category.REDUNDANCY,
                    severity=Severity.MINOR,
                    line=node.lineno,
                    confidence=0.8,
                )
            )
    return findings


def _is_abstract(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Abstract methods and protocol stubs legitimately ignore their arguments."""
    for decorator in node.decorator_list:
        name = getattr(decorator, "id", None) or getattr(decorator, "attr", None)
        if name in ("abstractmethod", "abstractproperty", "overload"):
            return True
    body = [n for n in node.body if not isinstance(n, ast.Expr) or not isinstance(n.value, ast.Constant)]
    if not body:
        return True
    return len(body) == 1 and isinstance(body[0], (ast.Pass, ast.Raise))


def _max_nesting(node: ast.AST, depth: int = 0) -> int:
    nesting_nodes = (ast.If, ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith, ast.Try)
    best = depth
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue  # nested definitions have their own budget
        child_depth = depth + 1 if isinstance(child, nesting_nodes) else depth
        best = max(best, _max_nesting(child, child_depth))
    return best


def _python_test_quality(file_diff: FileDiff, tree: ast.AST, changed: set[int]) -> list[Finding]:
    """Does this test actually test anything?

    The most dangerous generated test is one that passes unconditionally: it
    raises the coverage number, which makes everyone *less* likely to look.
    """
    findings: list[Finding] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not node.name.startswith("test") or not _touches(node, changed):
            continue

        asserts = [n for n in ast.walk(node) if isinstance(n, ast.Assert)]
        assert_calls = [
            n
            for n in ast.walk(node)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr.startswith(("assert", "assert_"))
        ]
        raises_ctx = [
            n
            for n in ast.walk(node)
            if isinstance(n, ast.withitem)
            and isinstance(n.context_expr, ast.Call)
            and "raises" in ast.dump(n.context_expr.func)
        ]

        if not asserts and not assert_calls and not raises_ctx:
            findings.append(
                _finding(
                    file_diff,
                    rule="testing.no_assertion",
                    title=f"`{node.name}` asserts nothing",
                    body=(
                        "This test has no assertion, so it passes as long as the code does not "
                        "raise. It will report success for almost any regression while still "
                        "counting towards coverage, which is worse than having no test at all."
                    ),
                    category=Category.TESTING,
                    severity=Severity.MAJOR,
                    line=node.lineno,
                    end_line=node.end_lineno,
                    suggestion="Assert the specific value or behaviour this test is meant to pin down.",
                    confidence=0.95,
                )
            )
            continue

        # Assertions that are true for almost any value.
        for assertion in asserts:
            test = assertion.test
            weak_reason = ""
            if isinstance(test, ast.Constant) and test.value:
                weak_reason = "asserts a constant that is always true"
            elif isinstance(test, ast.Compare) and isinstance(test.ops[0], (ast.IsNot,)):
                comparators = test.comparators
                if comparators and isinstance(comparators[0], ast.Constant) and comparators[0].value is None:
                    weak_reason = "only checks the result is not None"
            elif isinstance(test, ast.Call) and isinstance(test.func, ast.Name) and test.func.id == "isinstance":
                weak_reason = "only checks the result's type"
            if weak_reason:
                findings.append(
                    _finding(
                        file_diff,
                        rule="testing.weak_assertion",
                        title=f"Weak assertion in `{node.name}`",
                        body=(
                            f"This assertion {weak_reason}, so it would still pass if the value "
                            "were wrong in every way that matters. Pin down the expected value."
                        ),
                        category=Category.TESTING,
                        severity=Severity.MINOR,
                        line=assertion.lineno,
                        confidence=0.85,
                    )
                )
    return findings
