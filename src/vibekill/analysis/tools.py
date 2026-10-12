"""External analyser runners.

Each runner wraps a CLI tool, parses its JSON output into `Finding` objects, and
is individually optional. A missing binary produces a note in the report, never
an error: an air-gapped install frequently has none of these, and the review
still has to run.

All of them are invoked with a scrubbed environment (see `airgap.subprocess_env`)
and with flags that keep them offline. Semgrep is the one that needs care —
`--config auto` contacts the Semgrep registry to pick rules, so air-gapped mode
requires explicit local rule paths instead.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from ..airgap import subprocess_env
from ..models import Category, Finding, ReviewContext, Severity
from ..settings import Settings


class ToolOutcome:
    """Result of one analyser, including why it did not run."""

    def __init__(
        self,
        name: str,
        findings: list[Finding] | None = None,
        *,
        available: bool = True,
        skipped_reason: str = "",
        error: str = "",
        duration_s: float = 0.0,
    ) -> None:
        self.name = name
        self.findings = findings or []
        self.available = available
        self.skipped_reason = skipped_reason
        self.error = error
        self.duration_s = duration_s

    @property
    def ran(self) -> bool:
        return self.available and not self.skipped_reason and not self.error

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "ran": self.ran,
            "available": self.available,
            "skipped_reason": self.skipped_reason,
            "error": self.error,
            "finding_count": len(self.findings),
            "duration_s": round(self.duration_s, 2),
        }


def _run(cmd: list[str], cwd: Path, timeout: int) -> tuple[int, str, str]:
    proc = subprocess.run(
        cmd,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=subprocess_env(),
        check=False,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _target_paths(context: ReviewContext, languages: Iterable[str]) -> list[str]:
    """Paths worth handing to a language-specific analyser."""
    wanted = set(languages)
    return [
        f.path
        for f in context.reviewable_files()
        if f.language in wanted and Path(context.meta.root, f.path).is_file()
    ]


# --------------------------------------------------------------------------- #
# Runners
# --------------------------------------------------------------------------- #


def run_ruff(context: ReviewContext, settings: Settings) -> ToolOutcome:
    if not shutil.which("ruff"):
        return ToolOutcome("ruff", available=False, skipped_reason="ruff is not installed")
    paths = _target_paths(context, ["python"])
    if not paths:
        return ToolOutcome("ruff", skipped_reason="no Python files changed")

    code, stdout, stderr = _run(
        ["ruff", "check", "--output-format", "json", "--no-cache", "--force-exclude", *paths],
        Path(context.meta.root),
        settings.tools.timeout_s,
    )
    if code not in (0, 1):
        return ToolOutcome("ruff", error=(stderr or stdout).strip()[:300])

    findings: list[Finding] = []
    for item in _safe_json(stdout, default=[]):
        rule = item.get("code") or "ruff"
        findings.append(
            Finding(
                title=f"{rule}: {item.get('message', '').strip()}"[:160],
                body=item.get("message", "").strip(),
                category=_ruff_category(rule),
                severity=_ruff_severity(rule),
                path=_relative(item.get("filename", ""), context.meta.root),
                line=(item.get("location") or {}).get("row"),
                end_line=(item.get("end_location") or {}).get("row"),
                agent="ruff",
                source="tool",
                rule_id=f"ruff.{rule}",
                confidence=0.95,
            )
        )
    return ToolOutcome("ruff", findings)


def run_bandit(context: ReviewContext, settings: Settings) -> ToolOutcome:
    if not shutil.which("bandit"):
        return ToolOutcome("bandit", available=False, skipped_reason="bandit is not installed")
    paths = _target_paths(context, ["python"])
    if not paths:
        return ToolOutcome("bandit", skipped_reason="no Python files changed")

    code, stdout, stderr = _run(
        ["bandit", "-f", "json", "-q", *paths],
        Path(context.meta.root),
        settings.tools.timeout_s,
    )
    if code not in (0, 1):
        return ToolOutcome("bandit", error=(stderr or stdout).strip()[:300])

    payload = _safe_json(stdout, default={})
    severity_map = {
        "HIGH": Severity.BLOCKER,
        "MEDIUM": Severity.MAJOR,
        "LOW": Severity.MINOR,
    }
    findings = [
        Finding(
            title=f"{item.get('test_id', 'bandit')}: {item.get('issue_text', '')}"[:160],
            body=item.get("issue_text", ""),
            category=Category.SECURITY,
            severity=severity_map.get(str(item.get("issue_severity", "")).upper(), Severity.MINOR),
            path=_relative(item.get("filename", ""), context.meta.root),
            line=item.get("line_number"),
            evidence=(item.get("code") or "").strip()[:200],
            agent="bandit",
            source="tool",
            rule_id=f"bandit.{item.get('test_id', '')}",
            # Bandit's own confidence rating is worth carrying through rather
            # than flattening: LOW-confidence results are frequently noise.
            confidence={"HIGH": 0.9, "MEDIUM": 0.7, "LOW": 0.5}.get(
                str(item.get("issue_confidence", "")).upper(), 0.6
            ),
        )
        for item in payload.get("results", [])
    ]
    return ToolOutcome("bandit", findings)


def run_semgrep(context: ReviewContext, settings: Settings) -> ToolOutcome:
    if not shutil.which("semgrep"):
        return ToolOutcome("semgrep", available=False, skipped_reason="semgrep is not installed")

    configs = list(settings.tools.semgrep_configs)
    if not configs:
        if settings.enforce_airgap:
            # `--config auto` sends languages, frameworks and the project URL to
            # the Semgrep registry to choose rules. Code is not uploaded, but
            # that metadata is exactly what an air-gapped deployment exists to
            # keep in. Refuse rather than quietly leak.
            return ToolOutcome(
                "semgrep",
                skipped_reason=(
                    "no local rules configured; '--config auto' contacts the Semgrep registry "
                    "and is refused in air-gapped mode. Set tools.semgrep_configs to a local "
                    "rules directory."
                ),
            )
        configs = ["auto"]

    missing = [c for c in configs if c != "auto" and not Path(context.meta.root, c).exists() and not Path(c).exists()]
    if missing:
        return ToolOutcome("semgrep", skipped_reason=f"rule paths not found: {', '.join(missing)}")

    cmd = ["semgrep", "--json", "--quiet", "--disable-version-check", "--metrics", "off"]
    for cfg in configs:
        cmd += ["--config", cfg]
    cmd += [f.path for f in context.reviewable_files()]

    try:
        code, stdout, stderr = _run(cmd, Path(context.meta.root), settings.tools.timeout_s)
    except subprocess.TimeoutExpired:
        return ToolOutcome("semgrep", error=f"timed out after {settings.tools.timeout_s}s")
    if code not in (0, 1):
        return ToolOutcome("semgrep", error=(stderr or stdout).strip()[:300])

    payload = _safe_json(stdout, default={})
    severity_map = {"ERROR": Severity.BLOCKER, "WARNING": Severity.MAJOR, "INFO": Severity.MINOR}
    findings = []
    for item in payload.get("results", []):
        extra = item.get("extra") or {}
        metadata = extra.get("metadata") or {}
        findings.append(
            Finding(
                title=(metadata.get("shortDescription") or item.get("check_id", "semgrep"))[:160],
                body=(extra.get("message") or "").strip(),
                category=Category.SECURITY
                if "security" in str(metadata.get("category", "")).lower()
                else Category.CORRECTNESS,
                severity=severity_map.get(str(extra.get("severity", "")).upper(), Severity.MINOR),
                path=_relative(item.get("path", ""), context.meta.root),
                line=(item.get("start") or {}).get("line"),
                end_line=(item.get("end") or {}).get("line"),
                evidence=(extra.get("lines") or "").strip()[:200],
                agent="semgrep",
                source="tool",
                rule_id=str(item.get("check_id", "")),
                confidence=0.9,
            )
        )
    return ToolOutcome("semgrep", findings)


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #

RUNNERS = {
    "ruff": run_ruff,
    "bandit": run_bandit,
    "semgrep": run_semgrep,
}


def run_tools(context: ReviewContext, settings: Settings) -> tuple[list[Finding], list[ToolOutcome]]:
    """Run the enabled analysers concurrently.

    These are separate processes doing CPU-bound work, so this is the one place
    in the pipeline with genuine parallelism — unlike the agents, which queue on
    a single local model.
    """
    import time

    if not settings.tools.enabled:
        return [], [ToolOutcome(name, available=False, skipped_reason="tools disabled") for name in RUNNERS]

    selected = [name for name in RUNNERS if getattr(settings.tools, name, False)]
    if context.mode == "github":
        return [], [
            ToolOutcome(
                name,
                skipped_reason="GitHub diff mode has no local checkout; filesystem analyzer unavailable",
            )
            for name in selected
        ]
    if not selected:
        return [], []

    def invoke(name: str) -> ToolOutcome:
        started = time.monotonic()
        try:
            outcome = RUNNERS[name](context, settings)
        except subprocess.TimeoutExpired:
            outcome = ToolOutcome(name, error=f"timed out after {settings.tools.timeout_s}s")
        except Exception as exc:  # a broken analyser must not fail the review
            outcome = ToolOutcome(name, error=f"{type(exc).__name__}: {exc}"[:300])
        outcome.duration_s = time.monotonic() - started
        return outcome

    with ThreadPoolExecutor(max_workers=max(1, len(selected))) as pool:
        outcomes = list(pool.map(invoke, selected))

    findings: list[Finding] = []
    for outcome in outcomes:
        findings.extend(outcome.findings)

    if settings.tools.changed_lines_only:
        findings = restrict_to_changed_lines(findings, context)
    return findings, outcomes


def restrict_to_changed_lines(findings: list[Finding], context: ReviewContext) -> list[Finding]:
    """Keep only findings on lines this diff touched.

    Without this, adding one line to a legacy file produces a review full of
    pre-existing issues the author did not cause, which is the fastest way to
    train people to ignore the bot.
    """
    changed: dict[str, set[int]] = {f.path: f.changed_line_numbers() for f in context.files}
    kept = []
    for finding in findings:
        if not finding.path or finding.line is None:
            kept.append(finding)
            continue
        lines = changed.get(finding.path)
        if lines is None:
            continue
        span = range(finding.line, (finding.end_line or finding.line) + 1)
        if any(line in lines for line in span):
            kept.append(finding)
    return kept


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _safe_json(text: str, default: Any) -> Any:
    """Tools occasionally print warnings before their JSON payload."""
    text = (text or "").strip()
    if not text:
        return default
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        for opener, closer in (("{", "}"), ("[", "]")):
            start, end = text.find(opener), text.rfind(closer)
            if start != -1 and end > start:
                try:
                    return json.loads(text[start : end + 1])
                except json.JSONDecodeError:
                    continue
        return default


def _relative(path: str, root: str) -> str:
    if not path:
        return path
    try:
        return str(Path(path).resolve().relative_to(Path(root).resolve()))
    except (ValueError, OSError):
        return path.lstrip("./")


# Ruff rule prefixes mapped onto our categories, so a formatting nit is not
# presented with the same weight as a likely bug.
_RUFF_SECURITY = ("S",)
_RUFF_BUGS = ("B", "F82", "F81", "E711", "E712", "E722", "ASYNC", "RUF")
_RUFF_COMPLEXITY = ("C9", "PLR09", "PLR1")
_RUFF_REDUNDANCY = ("F401", "F841", "ERA", "SIM")


def _ruff_category(rule: str) -> Category:
    if rule.startswith(_RUFF_SECURITY):
        return Category.SECURITY
    if rule.startswith(_RUFF_REDUNDANCY):
        return Category.REDUNDANCY
    if rule.startswith(_RUFF_COMPLEXITY):
        return Category.COMPLEXITY
    if rule.startswith(_RUFF_BUGS):
        return Category.CORRECTNESS
    return Category.QUALITY


def _ruff_severity(rule: str) -> Severity:
    if rule.startswith(("F821", "F822", "E999")):
        return Severity.BLOCKER  # undefined name or syntax error
    if rule.startswith(_RUFF_SECURITY):
        return Severity.MAJOR
    if rule.startswith(_RUFF_BUGS):
        return Severity.MINOR
    return Severity.INFO
