"""Aggregation: turn raw agent output into a review somebody will trust.

This module is where precision is bought. A reviewer that cries wolf gets muted
within two weeks, so the default posture is to discard anything we cannot stand
behind. Four stages, in order:

1. **Evidence gate** — a model finding must quote the line it cites, and that
   quote must match the real file. This is the main defence against invented
   locations, and it is cheap: one string comparison per finding.
2. **Corroboration** — a model finding that a deterministic analyser independently
   flagged is more likely real, and is marked as such.
3. **Dedup** — collapse the same defect reported by several agents.
4. **Severity policy** — decide what may block a merge, then derive the verdict.

The counts of what was dropped are reported rather than hidden. "We discarded six
unverifiable findings" is information; silently discarding them is not.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence

from .models import (
    AgentResult,
    Finding,
    ReviewContext,
    ReviewResult,
    Severity,
    Verdict,
    sort_findings,
)
from .settings import Settings

logger = logging.getLogger("vibekill.aggregate")

# Two findings on the same path within this many lines are treated as the same
# defect if their categories match. Agents rarely agree on the exact line.
DEDUP_LINE_WINDOW = 3

# Confidence added when a deterministic tool independently flagged the same spot.
CORROBORATION_BONUS = 0.25


def aggregate(
    context: ReviewContext,
    agent_results: Sequence[AgentResult],
    deterministic: Sequence[Finding],
    settings: Settings,
    *,
    duration_s: float = 0.0,
    tools_used: Sequence[str] | None = None,
    analyzer_results: Sequence[dict] | None = None,
    warnings: Sequence[str] | None = None,
) -> ReviewResult:
    """Combine deterministic and agent findings into the final result."""
    result = ReviewResult(
        context=context,
        agent_results=list(agent_results),
        duration_s=duration_s,
        airgapped=(
            settings.mode == "airgapped"
            and settings.enforce_airgap
            and settings.only_local_hosts_allowed()
        ),
        tools_used=list(tools_used or []),
        analyzer_results=list(analyzer_results or []),
        policy={
            "fail_on": settings.fail_on.value,
            "require_evidence": settings.require_evidence,
            "min_confidence": settings.min_confidence,
            "changed_lines_only": settings.tools.changed_lines_only,
            "network_guard_enabled": settings.enforce_airgap,
            "allowed_hosts": list(settings.allow_hosts),
        },
        warnings=list(warnings or []),
    )

    llm_findings = [f for r in agent_results for f in r.findings]

    verified, dropped = _apply_evidence_gate(llm_findings, context, settings)
    result.dropped_unverified = dropped

    _corroborate(verified, deterministic)
    _apply_severity_policy(verified)

    below_floor = [f for f in verified if f.confidence < settings.min_confidence]
    verified = [f for f in verified if f.confidence >= settings.min_confidence]
    result.suppressed = len(below_floor)

    combined, deduped = _dedup([*deterministic, *verified])
    result.deduped = deduped

    result.findings = sort_findings(combined)
    result.verdict = decide_verdict(result.findings, settings)

    if context.skipped_files:
        result.warnings.append(
            f"{len(context.skipped_files)} file(s) were not reviewed because the diff exceeded "
            f"the configured budget: {', '.join(context.skipped_files[:5])}"
            + (" ..." if len(context.skipped_files) > 5 else "")
        )
    for agent in agent_results:
        if agent.error:
            result.warnings.append(
                f"The {agent.name} agent did not finish ({agent.error}). "
                "Its findings, if any, are incomplete."
            )
    return result


# --------------------------------------------------------------------------- #
# 1. Evidence gate
# --------------------------------------------------------------------------- #


def _apply_evidence_gate(
    findings: Sequence[Finding], context: ReviewContext, settings: Settings
) -> tuple[list[Finding], int]:
    """Keep model findings whose cited evidence matches the real file.

    A finding surviving with a corrected line number is better than one dropped,
    so when the quote is real but the number is off we search for it nearby and
    fix the number instead of discarding the finding.
    """
    kept: list[Finding] = []
    dropped = 0

    for finding in findings:
        if finding.source != "llm":
            kept.append(finding)
            continue

        file_diff = context.file_by_path(finding.path or "")
        if file_diff is None:
            dropped += 1
            logger.debug("dropping finding on unknown path: %s", finding.path)
            continue

        quoted = finding.evidence.strip()
        if finding.line is None or not quoted:
            if settings.require_evidence:
                dropped += 1
                continue
            finding.tags.append("evidence-unverified")
            finding.confidence *= 0.5
            kept.append(finding)
            continue

        actual = file_diff.line_at(finding.line)

        if (
            finding.line in file_diff.changed_line_numbers()
            and actual is not None
            and _lines_match(quoted, actual)
        ):
            finding.tags.append("evidence-verified")
            kept.append(finding)
            continue

        relocated = _find_line(file_diff, quoted) if quoted else None
        if relocated is not None:
            finding.line = relocated
            finding.tags.append("evidence-relocated")
            finding.confidence *= 0.9
            kept.append(finding)
            continue

        # The quote matches nothing in the file. Either the model invented the
        # code or it is reviewing something that is not there.
        if settings.require_evidence:
            dropped += 1
            logger.debug(
                "dropping unverifiable finding %s:%s (%s)", finding.path, finding.line, finding.title
            )
            continue

        finding.tags.append("evidence-unverified")
        finding.confidence *= 0.5
        kept.append(finding)

    return kept, dropped


def _lines_match(quoted: str, actual: str) -> bool:
    """Compare ignoring whitespace and quote style.

    Models reflow indentation and swap quote characters constantly; neither
    changes what line is being referred to.
    """
    return _normalise_line(quoted) == _normalise_line(actual) or (
        len(quoted) > 12 and _normalise_line(quoted) in _normalise_line(actual)
    )


def _normalise_line(text: str) -> str:
    return "".join(text.split()).replace('"', "'").rstrip(";")


def _find_line(file_diff, quoted: str) -> int | None:
    """Locate a quoted snippet among the lines the change touched."""
    target = _normalise_line(quoted)
    if len(target) < 8:
        return None
    for lineno in sorted(file_diff.changed_line_numbers()):
        actual = file_diff.line_at(lineno)
        if actual and target in _normalise_line(actual):
            return lineno
    return None


# --------------------------------------------------------------------------- #
# 2. Corroboration
# --------------------------------------------------------------------------- #


def _corroborate(llm_findings: Sequence[Finding], deterministic: Sequence[Finding]) -> None:
    """Raise confidence where a tool independently flagged the same location.

    Agreement between a rule engine and a language model is meaningfully stronger
    evidence than either alone, because they fail in unrelated ways.
    """
    index: dict[str, list[Finding]] = {}
    for finding in deterministic:
        if finding.path and finding.line:
            index.setdefault(finding.path, []).append(finding)

    for finding in llm_findings:
        if not finding.path or finding.line is None:
            continue
        for other in index.get(finding.path, []):
            same_line = other.line == finding.line
            same_category = other.category is finding.category
            same_evidence = bool(
                finding.evidence
                and other.evidence
                and _normalise_line(finding.evidence) == _normalise_line(other.evidence)
            )
            similar_title = _title_similarity(finding.title, other.title) >= 0.5
            if same_line and (same_category or same_evidence or similar_title):
                finding.confidence = min(1.0, finding.confidence + CORROBORATION_BONUS)
                finding.corroborated_by.append(other.rule_id or other.agent)
                finding.tags.append("corroborated")
                break


# --------------------------------------------------------------------------- #
# 3. Dedup
# --------------------------------------------------------------------------- #


def _dedup(findings: Sequence[Finding]) -> tuple[list[Finding], int]:
    """Merge findings describing the same defect.

    Deterministic findings win as the surviving record because their wording is
    stable and their rule id is actionable, but the model's explanation and
    suggested fix are folded in, since that is usually the more useful prose.
    """
    buckets: list[list[Finding]] = []

    for finding in sorted(findings, key=lambda f: (f.source != "tool", f.source != "heuristic")):
        for bucket in buckets:
            if _same_defect(bucket[0], finding):
                bucket.append(finding)
                break
        else:
            buckets.append([finding])

    merged: list[Finding] = []
    removed = 0
    for bucket in buckets:
        primary = bucket[0]
        if len(bucket) > 1:
            removed += len(bucket) - 1
            primary = _merge_bucket(bucket)
        merged.append(primary)
    return merged, removed


def _same_defect(a: Finding, b: Finding) -> bool:
    """Whether two findings describe the same problem.

    Category is a weak signal here and must not be required to match. Agents
    classify the same defect differently by design — a swallowed exception is
    `security` to one agent and `correctness` to another — so keying dedup on
    category leaves one copy of the finding per agent. Location plus wording is
    the reliable test.

    When both findings quote code, the quote is the deciding signal in both
    directions: identical quotes mean the same code regardless of wording or
    category; different quotes mean different code even on adjacent lines.
    Proximity and category cannot distinguish those two cases structurally.
    """
    if a.path != b.path:
        return False

    if a.line is None or b.line is None:
        return a.line == b.line and _title_similarity(a.title, b.title) >= 0.6

    if abs(a.line - b.line) > DEDUP_LINE_WINDOW:
        return False

    # When both findings quote code, the quote decides it — in both directions.
    # Identical quotes mean both are about the same code, whatever words or
    # category each agent chose. Different quotes mean they are about different
    # code, even on adjacent lines, and merging them would silently discard one.
    #
    # This is the only discriminator that reliably separates "two agents
    # describing one defect differently" from "two real defects side by side".
    # Proximity and category cannot: those two cases look identical structurally.
    if a.evidence and b.evidence:
        return _normalise_line(a.evidence) == _normalise_line(b.evidence)

    return a.category is b.category or _title_similarity(a.title, b.title) >= 0.5


def _title_similarity(a: str, b: str) -> float:
    """Token overlap between two titles, 0.0 to 1.0 (Jaccard)."""
    tokens_a = set(_normalise_title(a).split()) - _STOPWORDS
    tokens_b = set(_normalise_title(b).split()) - _STOPWORDS
    if not tokens_a or not tokens_b:
        return 0.0
    return len(tokens_a & tokens_b) / len(tokens_a | tokens_b)


_STOPWORDS: frozenset[str] = frozenset({
    "a", "an", "the", "is", "are", "be", "this", "that", "it", "in", "on", "of",
    "to", "and", "or", "not", "with", "for", "from", "by", "as", "at", "can",
})


def _normalise_title(title: str) -> str:
    return " ".join("".join(c.lower() if c.isalnum() else " " for c in title).split())


def _merge_bucket(bucket: Sequence[Finding]) -> Finding:
    primary = bucket[0]
    others = bucket[1:]

    primary.severity = max((f.severity for f in bucket), key=lambda s: s.rank)
    primary.confidence = min(1.0, max(f.confidence for f in bucket))

    for other in others:
        label = other.rule_id or other.agent
        if label and label not in primary.corroborated_by:
            primary.corroborated_by.append(label)
        if len(other.body) > len(primary.body) * 1.3:
            primary.body = other.body
        if other.suggestion and not primary.suggestion:
            primary.suggestion = other.suggestion
        for tag in other.tags:
            if tag not in primary.tags:
                primary.tags.append(tag)

    if len(bucket) > 1 and "merged" not in primary.tags:
        primary.tags.append("merged")
    return primary


# --------------------------------------------------------------------------- #
# 4. Severity policy
# --------------------------------------------------------------------------- #


def _apply_severity_policy(findings: Iterable[Finding]) -> None:
    """Constrain what a model alone is allowed to escalate.

    A model finding may not block a merge on its own judgement. `blocker`
    survives only when an independent deterministic analyzer corroborates it;
    standalone model findings are capped at `major`.
    """
    for finding in findings:
        if finding.severity is not Severity.BLOCKER or finding.source != "llm":
            continue

        if not finding.corroborated_by:
            finding.severity = Severity.MAJOR
            finding.tags.append("capped-uncorroborated")


def decide_verdict(findings: Sequence[Finding], settings: Settings) -> Verdict:
    """Map surviving findings onto a recommendation.

    Advisory by design: the threshold is configurable, and the default only
    requests changes for findings the policy above has already vouched for.
    """
    worst = max((f.severity for f in findings), key=lambda s: s.rank, default=Severity.INFO)
    if worst.rank >= settings.fail_on.rank:
        return Verdict.REQUEST_CHANGES
    if worst.rank >= Severity.MINOR.rank:
        return Verdict.COMMENT
    return Verdict.APPROVE
