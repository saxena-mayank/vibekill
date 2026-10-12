"""Core data model shared by every layer of vibekill.

Everything downstream of the diff parser speaks in terms of these objects, which
is what lets the air-gapped and GitHub-connected modes share one review engine:
the source adapters differ, the `ReviewContext` they produce does not.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal

# --------------------------------------------------------------------------- #
# Severity and verdict
# --------------------------------------------------------------------------- #


class Severity(StrEnum):
    """Ordered severity scale.

    `BLOCKER` is reserved for things that should stop a merge: exploitable
    security defects, data loss, broken contracts. Everything a model merely
    *suspects* starts at `MAJOR` or below unless a deterministic tool agrees.

    Order comparisons go through `.rank`, never through `<`. Inheriting from
    `str` means `<` is alphabetical, and "blocker" < "minor" alphabetically is
    the opposite of what it means here, so the operator is left alone rather
    than overloaded into something surprising.
    """

    BLOCKER = "blocker"
    MAJOR = "major"
    MINOR = "minor"
    INFO = "info"

    @property
    def rank(self) -> int:
        return _SEVERITY_RANK[self]

    @classmethod
    def parse(cls, value: object, default: Severity = None) -> Severity:  # type: ignore[assignment]
        """Lenient parse; LLMs produce 'high', 'critical', 'nit', etc."""
        if isinstance(value, Severity):
            return value
        text = str(value or "").strip().lower()
        if text in _SEVERITY_ALIASES:
            return _SEVERITY_ALIASES[text]
        return default if default is not None else cls.MINOR


_SEVERITY_RANK = {
    Severity.INFO: 0,
    Severity.MINOR: 1,
    Severity.MAJOR: 2,
    Severity.BLOCKER: 3,
}

_SEVERITY_ALIASES = {
    "blocker": Severity.BLOCKER,
    "critical": Severity.BLOCKER,
    "crit": Severity.BLOCKER,
    "fatal": Severity.BLOCKER,
    "high": Severity.MAJOR,
    "major": Severity.MAJOR,
    "error": Severity.MAJOR,
    "medium": Severity.MINOR,
    "moderate": Severity.MINOR,
    "minor": Severity.MINOR,
    "low": Severity.MINOR,
    "warning": Severity.MINOR,
    "warn": Severity.MINOR,
    "nit": Severity.INFO,
    "nitpick": Severity.INFO,
    "info": Severity.INFO,
    "note": Severity.INFO,
    "style": Severity.INFO,
}


class Verdict(StrEnum):
    APPROVE = "approve"
    COMMENT = "comment"
    REQUEST_CHANGES = "request_changes"


class Category(StrEnum):
    """What kind of problem this is, independent of which agent found it."""

    SECURITY = "security"
    CORRECTNESS = "correctness"
    QUALITY = "quality"
    TESTING = "testing"
    COMPLEXITY = "complexity"
    REDUNDANCY = "redundancy"
    PERFORMANCE = "performance"
    DOCS = "docs"
    DEPENDENCY = "dependency"

    @classmethod
    def parse(cls, value: object, default: Category) -> Category:
        text = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
        for member in cls:
            if member.value == text:
                return member
        return _CATEGORY_ALIASES.get(text, default)


_CATEGORY_ALIASES = {
    "vulnerability": Category.SECURITY,
    "vuln": Category.SECURITY,
    "bug": Category.CORRECTNESS,
    "logic": Category.CORRECTNESS,
    "correctness_bug": Category.CORRECTNESS,
    "maintainability": Category.QUALITY,
    "readability": Category.QUALITY,
    "style": Category.QUALITY,
    "test": Category.TESTING,
    "tests": Category.TESTING,
    "coverage": Category.TESTING,
    "over_engineering": Category.COMPLEXITY,
    "overengineering": Category.COMPLEXITY,
    "dead_code": Category.REDUNDANCY,
    "duplication": Category.REDUNDANCY,
    "duplicate": Category.REDUNDANCY,
    "perf": Category.PERFORMANCE,
    "documentation": Category.DOCS,
    "deps": Category.DEPENDENCY,
    "dependencies": Category.DEPENDENCY,
}


# --------------------------------------------------------------------------- #
# Diff model
# --------------------------------------------------------------------------- #

ChangeType = Literal["added", "modified", "deleted", "renamed"]
LineKind = Literal["add", "del", "ctx"]


@dataclass(slots=True)
class DiffLine:
    """One line inside a hunk, with both sides' line numbers resolved."""

    kind: LineKind
    content: str
    old_lineno: int | None = None
    new_lineno: int | None = None

    @property
    def marker(self) -> str:
        return {"add": "+", "del": "-", "ctx": " "}[self.kind]


@dataclass(slots=True)
class Hunk:
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    heading: str = ""
    lines: list[DiffLine] = field(default_factory=list)

    @property
    def new_end(self) -> int:
        return self.new_start + max(self.new_count - 1, 0)

    def added_line_numbers(self) -> set[int]:
        return {ln.new_lineno for ln in self.lines if ln.kind == "add" and ln.new_lineno}

    def render(self, *, with_line_numbers: bool = True) -> str:
        """Render the hunk for an LLM prompt.

        Line numbers are inlined on purpose: every agent is required to cite a
        `line`, and models are far more accurate when the number sits directly
        next to the code rather than having to be counted from a header.
        """
        out = [f"@@ -{self.old_start},{self.old_count} +{self.new_start},{self.new_count} @@ {self.heading}".rstrip()]
        for ln in self.lines:
            if with_line_numbers:
                num = ln.new_lineno if ln.new_lineno is not None else ln.old_lineno
                label = f"{num:>6}" if num is not None else "      "
                out.append(f"{label} {ln.marker}{ln.content}")
            else:
                out.append(f"{ln.marker}{ln.content}")
        return "\n".join(out)


@dataclass(slots=True)
class FileDiff:
    path: str
    change_type: ChangeType = "modified"
    old_path: str | None = None
    hunks: list[Hunk] = field(default_factory=list)
    is_binary: bool = False
    language: str = "unknown"
    # Full post-change content is attached for local AST heuristics. Model
    # prompts remain diff-only so their size is predictable.
    new_content: str | None = None
    additions: int = 0
    deletions: int = 0

    @property
    def is_test_file(self) -> bool:
        low = self.path.lower()
        name = low.rsplit("/", 1)[-1]
        return (
            "/test" in low
            or low.startswith("test")
            or "/spec/" in low
            or "__tests__" in low
            or name.startswith("test_")
            or name.endswith(("_test.py", "_test.go", "_test.ts", "_test.js"))
            or ".test." in name
            or ".spec." in name
        )

    def added_line_numbers(self) -> set[int]:
        nums: set[int] = set()
        for hunk in self.hunks:
            nums |= hunk.added_line_numbers()
        return nums

    def changed_line_numbers(self) -> set[int]:
        """Line numbers introduced or replaced by this diff."""
        return self.added_line_numbers()

    def added_source(self) -> str:
        return "\n".join(ln.content for h in self.hunks for ln in h.lines if ln.kind == "add")

    def line_at(self, lineno: int) -> str | None:
        """Post-change content of `lineno`, from full content or the hunks."""
        if self.new_content is not None:
            lines = self.new_content.splitlines()
            if 1 <= lineno <= len(lines):
                return lines[lineno - 1]
        for hunk in self.hunks:
            for ln in hunk.lines:
                if ln.new_lineno == lineno:
                    return ln.content
        return None

    def render(self) -> str:
        header = f"--- a/{self.old_path or self.path}\n+++ b/{self.path}"
        if self.is_binary:
            return f"{header}\n(binary file, not shown)"
        return "\n".join([header, *(h.render() for h in self.hunks)])


@dataclass(slots=True)
class RepoMeta:
    """Everything we know about the change beyond the diff itself."""

    root: str
    base_ref: str = ""
    head_ref: str = ""
    base_sha: str = ""
    head_sha: str = ""
    title: str = ""
    description: str = ""
    author: str = ""
    branch: str = ""
    pr_number: int | None = None
    remote_url: str = ""
    commit_subjects: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ReviewContext:
    """Normalised, source-agnostic input to the review engine."""

    meta: RepoMeta
    files: list[FileDiff] = field(default_factory=list)
    mode: Literal["airgapped", "github"] = "airgapped"
    # Repository inventory used for "is this reinventing something?" checks,
    # populated cheaply from the working tree without reading every file.
    repo_file_index: list[str] = field(default_factory=list)
    # Files dropped to stay inside the budget. Surfaced in the report, because a
    # silently unreviewed file is worse than an obviously unreviewed one.
    skipped_files: list[str] = field(default_factory=list)
    excluded_files: list[str] = field(default_factory=list)

    @property
    def additions(self) -> int:
        return sum(f.additions for f in self.files)

    @property
    def deletions(self) -> int:
        return sum(f.deletions for f in self.files)

    def reviewable_files(self) -> list[FileDiff]:
        return [f for f in self.files if not f.is_binary and f.change_type != "deleted" and f.hunks]

    def file_by_path(self, path: str) -> FileDiff | None:
        for f in self.files:
            if f.path == path:
                return f
        return None

    def touches_tests(self) -> bool:
        return any(f.is_test_file for f in self.files)

    def languages(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for f in self.files:
            counts[f.language] = counts.get(f.language, 0) + f.additions
        return dict(sorted(counts.items(), key=lambda kv: -kv[1]))


# --------------------------------------------------------------------------- #
# Findings
# --------------------------------------------------------------------------- #

FindingSource = Literal["tool", "heuristic", "llm"]


@dataclass(slots=True)
class Finding:
    """A single reviewer remark.

    `evidence` is load-bearing, not decoration. The aggregator verifies that the
    quoted snippet actually appears at the cited location and drops findings that
    fail, which is the main defence against a local model inventing line numbers.
    """

    title: str
    body: str
    category: Category
    severity: Severity
    path: str | None = None
    line: int | None = None
    end_line: int | None = None
    evidence: str = ""
    suggestion: str = ""
    agent: str = "unknown"
    source: FindingSource = "llm"
    rule_id: str = ""
    # 0.0-1.0. Tool findings are near-certain; LLM findings get scaled by
    # whether evidence verified and whether a tool independently agreed.
    confidence: float = 0.5
    corroborated_by: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)

    def fingerprint(self) -> str:
        """Stable identity for dedup and for suppressing known findings.

        Deliberately excludes severity and prose so that two agents describing
        the same defect in different words collapse into one entry.
        """
        basis = "|".join(
            [
                self.path or "",
                str(self.line or 0),
                self.category.value,
                _normalise_text(self.title)[:80],
            ]
        )
        return hashlib.sha1(basis.encode("utf-8")).hexdigest()[:12]

    @property
    def location(self) -> str:
        if not self.path:
            return "(repository)"
        if self.line:
            return f"{self.path}:{self.line}"
        return self.path

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.fingerprint(),
            "title": self.title,
            "body": self.body,
            "category": self.category.value,
            "severity": self.severity.value,
            "path": self.path,
            "line": self.line,
            "end_line": self.end_line,
            "evidence": self.evidence,
            "suggestion": self.suggestion,
            "agent": self.agent,
            "source": self.source,
            "rule_id": self.rule_id,
            "confidence": round(self.confidence, 3),
            "corroborated_by": list(self.corroborated_by),
            "tags": list(self.tags),
        }


def _normalise_text(text: str) -> str:
    return " ".join("".join(c.lower() if c.isalnum() else " " for c in text).split())


@dataclass(slots=True)
class AgentResult:
    """Outcome of one agent, including failures.

    An agent that crashes or times out must not silently vanish: the report says
    so, because "no findings" and "the security agent never ran" mean very
    different things to whoever is about to merge.
    """

    name: str
    findings: list[Finding] = field(default_factory=list)
    summary: str = ""
    duration_s: float = 0.0
    error: str | None = None
    model: str = ""
    llm_calls: int = 0
    prompt_chars: int = 0
    completion_chars: int = 0
    skipped_reason: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "summary": self.summary,
            "duration_s": round(self.duration_s, 2),
            "error": self.error,
            "skipped_reason": self.skipped_reason,
            "model": self.model,
            "llm_calls": self.llm_calls,
            "prompt_chars": self.prompt_chars,
            "completion_chars": self.completion_chars,
            "finding_count": len(self.findings),
        }


@dataclass(slots=True)
class ReviewResult:
    context: ReviewContext
    findings: list[Finding] = field(default_factory=list)
    agent_results: list[AgentResult] = field(default_factory=list)
    verdict: Verdict = Verdict.COMMENT
    duration_s: float = 0.0
    dropped_unverified: int = 0
    deduped: int = 0
    suppressed: int = 0
    warnings: list[str] = field(default_factory=list)
    airgapped: bool = True
    tools_used: list[str] = field(default_factory=list)
    analyzer_results: list[dict[str, Any]] = field(default_factory=list)
    policy: dict[str, Any] = field(default_factory=dict)

    def by_severity(self, severity: Severity) -> list[Finding]:
        return [f for f in self.findings if f.severity is severity]

    def counts(self) -> dict[str, int]:
        return {s.value: len(self.by_severity(s)) for s in Severity}

    def iter_grouped_by_file(self) -> Iterator[tuple[str, list[Finding]]]:
        groups: dict[str, list[Finding]] = {}
        for f in self.findings:
            groups.setdefault(f.path or "(repository)", []).append(f)
        for path in sorted(groups):
            yield path, sorted(
                groups[path], key=lambda f: (-f.severity.rank, f.line or 0)
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "vibekill/review/v1",
            "verdict": self.verdict.value,
            "airgapped": self.airgapped,
            "mode": self.context.mode,
            "duration_s": round(self.duration_s, 2),
            "repo": {
                "root": self.context.meta.root,
                "base": self.context.meta.base_ref,
                "head": self.context.meta.head_ref,
                "base_sha": self.context.meta.base_sha,
                "head_sha": self.context.meta.head_sha,
                "title": self.context.meta.title,
                "pr_number": self.context.meta.pr_number,
            },
            "stats": {
                "files_changed": (
                    len(self.context.files)
                    + len(self.context.skipped_files)
                    + len(self.context.excluded_files)
                ),
                "files_reviewed": len(self.context.reviewable_files()),
                "additions": self.context.additions,
                "deletions": self.context.deletions,
                "severity_counts": self.counts(),
                "dropped_unverified": self.dropped_unverified,
                "deduped": self.deduped,
                "suppressed": self.suppressed,
            },
            "tools_used": self.tools_used,
            "analyzers": self.analyzer_results,
            "policy": self.policy,
            "coverage": {
                "reviewed_files": [f.path for f in self.context.reviewable_files()],
                "skipped_files": list(self.context.skipped_files),
                "excluded_files": list(self.context.excluded_files),
            },
            "agents": [a.to_dict() for a in self.agent_results],
            "findings": [f.to_dict() for f in self.findings],
            "warnings": self.warnings,
        }


def sort_findings(findings: Iterable[Finding]) -> list[Finding]:
    """Highest severity first, then most confident, then by location."""
    return sorted(
        findings,
        key=lambda f: (-f.severity.rank, -f.confidence, f.path or "", f.line or 0),
    )
