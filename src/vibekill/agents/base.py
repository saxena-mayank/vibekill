"""Shared agent machinery.

Every agent is the same loop — chunk the diff, prompt the model, parse findings —
differing only in its system prompt, the evidence it cares about, and how it
grades severity. Keeping that loop in one place means a new agent is a ~40 line
file, which is the point: see `docs/CONTRIBUTING.md`.

Two design decisions worth knowing before changing anything here:

**Agents return only their own model findings.** Deterministic findings travel
separately to the aggregator. Agents *read* them as evidence but never re-emit
them, otherwise every tool finding would be counted twice.

**Every finding must cite evidence.** The prompt demands a verbatim snippet and
a line number, and `aggregate.py` verifies the snippet against the real file.
A model that invents a location gets dropped rather than believed, which is what
makes a small local model safe to rely on.
"""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from collections.abc import Iterable, Sequence
from dataclasses import replace

from ..llm import LLMClient, LLMError, coerce_finding_list, extract_json
from ..models import (
    AgentResult,
    Category,
    FileDiff,
    Finding,
    Hunk,
    ReviewContext,
    Severity,
    sort_findings,
)
from ..settings import AgentSettings, Settings

logger = logging.getLogger("vibekill.agents")

# Space reserved beyond the rendered diff for metadata and analyzer evidence.
PROMPT_OVERHEAD_CHARS = 2_000

RESPONSE_CONTRACT = """
Reply with JSON only, in exactly this shape:

{
  "summary": "one sentence on the overall state of this change",
  "findings": [
    {
      "title": "short imperative summary, under 90 characters",
      "body": "what is wrong and why it matters, 1-3 sentences",
      "path": "exact path from the diff header",
      "line": 123,
      "evidence": "the single line of code you are referring to, copied verbatim",
      "suggestion": "concrete fix, or \\"\\" if none",
      "severity": "blocker|major|minor|info",
      "category": "{categories}",
      "confidence": 0.0
    }
  ]
}

Rules you must follow:
- `line` must be a line number shown in the diff, and `evidence` must be that
  line copied exactly. Findings whose evidence does not match are discarded.
- Only comment on lines the diff adds or changes. Do not review pre-existing code.
- Report nothing rather than padding. An empty `findings` array is a valid answer
  and is the correct answer for a clean change.
- Do not restate what a listed analyser finding already says. Add judgement:
  whether it is reachable, whether it matters here, or what the real fix is.
- `confidence` is your own estimate from 0.0 to 1.0. Be honest; low-confidence
  findings are filtered rather than shown.
""".strip()


class Agent(ABC):
    """Base class for a reviewing agent."""

    #: Stable identifier, also the settings key and the label in reports.
    name: str = "agent"
    #: Default category for findings this agent produces.
    default_category: Category = Category.QUALITY
    #: Highest severity this agent may assign on its own. See `severity_cap`.
    severity_cap: Severity = Severity.MAJOR
    #: Human-readable description, shown in the UI and docs.
    description: str = ""

    def __init__(self, settings: Settings, client: LLMClient) -> None:
        self.settings = settings
        self.client = client

    @property
    def config(self) -> AgentSettings:
        return self.settings.agents.get(self.name)

    # -- to implement --------------------------------------------------- #

    @abstractmethod
    def system_prompt(self) -> str:
        """The agent's role and what it should look for."""

    @abstractmethod
    def relevant_evidence(self, evidence: Sequence[Finding]) -> list[Finding]:
        """Subset of deterministic findings this agent should reason about."""

    def allowed_categories(self) -> list[Category]:
        """Categories this agent may report. Anything else is coerced."""
        return [self.default_category]

    def files_to_review(self, context: ReviewContext) -> list[FileDiff]:
        """Which files this agent looks at. Overridden by the testing agent."""
        return context.reviewable_files()

    # -- the loop ------------------------------------------------------- #

    def run(self, context: ReviewContext, evidence: Sequence[Finding]) -> AgentResult:
        started = time.monotonic()
        result = AgentResult(name=self.name, model=self.client.model)

        if not self.config.enabled:
            result.skipped_reason = "disabled in configuration"
            return result

        files = self.files_to_review(context)
        if not files:
            result.skipped_reason = "no relevant files in this change"
            result.summary = "Nothing for this agent to review."
            return result

        if not self.client.available:
            result.skipped_reason = "no model available"
            result.summary = "Skipped: no local model. Deterministic findings still apply."
            return result

        my_evidence = self.relevant_evidence(evidence)
        findings: list[Finding] = []
        summaries: list[str] = []
        system_prompt = self._system_prompt_full()
        prompt_budget = (
            self.settings.llm.context_chars - len(system_prompt) - PROMPT_OVERHEAD_CHARS
        )
        if prompt_budget < 1_000:
            result.error = "The model context is too small for this agent's required instructions."
            result.duration_s = time.monotonic() - started
            return result

        diff_budget = max(512, prompt_budget - 1_000)
        for chunk in self._chunk(files, diff_budget):
            user_prompt = self._user_prompt(context, chunk, my_evidence)
            if len(user_prompt) > prompt_budget:
                marker = "\n\n[Additional context truncated to fit the model window.]"
                user_prompt = user_prompt[: prompt_budget - len(marker)] + marker
            try:
                text = self.client.chat(system_prompt, user_prompt)
            except LLMError as exc:
                result.error = str(exc)
                logger.warning("%s agent: %s", self.name, exc)
                break

            try:
                payload = extract_json(text)
            except ValueError as exc:
                logger.warning("%s agent: unparsable response: %s", self.name, exc)
                continue

            if isinstance(payload, dict) and payload.get("summary"):
                summaries.append(str(payload["summary"]).strip())
            findings.extend(self._parse_findings(payload, chunk))

        result.findings = sort_findings(findings)[: self.config.max_findings]
        result.summary = self._merge_summaries(summaries, result.findings)
        result.duration_s = time.monotonic() - started
        result.llm_calls = 0
        return result

    # -- prompting ------------------------------------------------------ #

    def _system_prompt_full(self) -> str:
        categories = "|".join(c.value for c in self.allowed_categories())
        parts = [
            self.system_prompt().strip(),
            RESPONSE_CONTRACT.format(categories=categories),
        ]
        if self.config.extra_instructions.strip():
            parts.append(
                "Additional project-specific instructions:\n"
                + self.config.extra_instructions.strip()
            )
        return "\n\n".join(parts)

    def _user_prompt(
        self,
        context: ReviewContext,
        chunk: Sequence[FileDiff],
        evidence: Sequence[Finding],
    ) -> str:
        sections: list[str] = []

        meta = context.meta
        header = [f"Change: {(meta.title or '(no title)')[:200]}"]
        if meta.description.strip():
            header.append(f"Description: {meta.description.strip()[:500]}")
        if meta.commit_subjects:
            header.append("Commits: " + "; ".join(item[:100] for item in meta.commit_subjects[:5]))
        sections.append("\n".join(header))
        sections.append(self._render_diff(chunk))

        chunk_paths = {f.path for f in chunk}
        relevant = [f for f in evidence if f.path in chunk_paths]
        if relevant:
            lines = [
                f"- {f.location} [{f.rule_id or f.source}] {f.severity.value}: {f.title}"
                for f in relevant[:40]
            ]
            sections.append(
                "Findings already reported by deterministic analysers. Treat these as facts. "
                "Do not repeat them; judge whether they matter here, and look for what they miss:\n"
                + "\n".join(lines)
            )

        extra = self.extra_context(context, chunk)
        if extra:
            sections.append(extra)

        return "\n\n---\n\n".join(sections)

    def extra_context(self, context: ReviewContext, chunk: Sequence[FileDiff]) -> str:
        """Hook for agent-specific context. Empty by default."""
        return ""

    def _render_diff(self, chunk: Sequence[FileDiff]) -> str:
        blocks = ["Diff under review. Numbers on the left are line numbers in the new file."]
        for file_diff in chunk:
            blocks.append(
                f"\n### {file_diff.path} ({file_diff.language}, {file_diff.change_type}, "
                f"+{file_diff.additions}/-{file_diff.deletions})\n"
                + "\n".join(hunk.render() for hunk in file_diff.hunks)
            )
        return "\n".join(blocks)

    # -- chunking ------------------------------------------------------- #

    def _chunk(self, files: Sequence[FileDiff], budget: int) -> list[list[FileDiff]]:
        """Group bounded file segments without dropping an oversized file tail."""
        segments = [segment for file in files for segment in self._split_file(file, budget)]
        chunks: list[list[FileDiff]] = []
        current: list[FileDiff] = []
        size = 0
        for segment in segments:
            rendered = len(segment.render())
            if current and size + rendered > budget:
                chunks.append(current)
                current, size = [], 0
            current.append(segment)
            size += rendered
        if current:
            chunks.append(current)
        return chunks

    def _split_file(self, file: FileDiff, budget: int) -> list[FileDiff]:
        """Split large diffs by hunk and then by line, preserving every line."""
        header_size = len(f"--- a/{file.old_path or file.path}\n+++ b/{file.path}\n")
        hunk_budget = max(256, budget - header_size)
        groups: list[list[Hunk]] = []
        current: list[Hunk] = []
        current_size = 0

        for hunk in file.hunks:
            pieces = self._split_hunk(hunk, hunk_budget)
            for piece in pieces:
                piece_size = len(piece.render())
                if current and current_size + piece_size > hunk_budget:
                    groups.append(current)
                    current, current_size = [], 0
                current.append(piece)
                current_size += piece_size
        if current:
            groups.append(current)
        return [replace(file, hunks=group) for group in groups] or [file]

    @staticmethod
    def _split_hunk(hunk: Hunk, budget: int) -> list[Hunk]:
        if len(hunk.render()) <= budget:
            return [hunk]

        pieces: list[Hunk] = []
        lines = []
        for line in hunk.lines:
            candidate = replace(hunk, lines=[*lines, line])
            if lines and len(candidate.render()) > budget:
                pieces.append(replace(hunk, lines=lines))
                lines = [line]
            else:
                lines.append(line)
        if lines:
            pieces.append(replace(hunk, lines=lines))
        return pieces

    # -- parsing -------------------------------------------------------- #

    def _parse_findings(self, payload: object, chunk: Sequence[FileDiff]) -> list[Finding]:
        by_path = {f.path: f for f in chunk}
        allowed = self.allowed_categories()
        findings: list[Finding] = []

        for item in coerce_finding_list(payload):
            title = _first_str(item, "title", "message", "issue", "summary")
            if not title:
                continue
            body = _first_str(item, "body", "description", "detail", "explanation", "rationale")
            path = _first_str(item, "path", "file", "filename", "file_path")

            # A path the model invented means we cannot verify anything it says
            # about location, so the finding is unusable.
            if path not in by_path:
                resolved = _resolve_path(path, by_path)
                if resolved is None:
                    logger.debug("%s agent: dropping finding for unknown path %r", self.name, path)
                    continue
                path = resolved

            severity = Severity.parse(item.get("severity"), Severity.MINOR)
            if severity.rank > self.severity_cap.rank:
                severity = self.severity_cap

            category = Category.parse(item.get("category"), self.default_category)
            if category not in allowed:
                category = self.default_category

            findings.append(
                Finding(
                    title=title[:160],
                    body=body or title,
                    category=category,
                    severity=severity,
                    path=path,
                    line=_as_int(item.get("line")),
                    end_line=_as_int(item.get("end_line")),
                    evidence=_first_str(item, "evidence", "code", "snippet")[:300],
                    suggestion=_first_str(item, "suggestion", "fix", "recommendation")[:600],
                    agent=self.name,
                    source="llm",
                    confidence=_as_confidence(item.get("confidence")),
                )
            )
        return findings

    def _merge_summaries(self, summaries: Sequence[str], findings: Sequence[Finding]) -> str:
        if summaries:
            return " ".join(summaries)[:500]
        if findings:
            return f"{len(findings)} finding(s) raised."
        return "No issues found in the reviewed scope."


# --------------------------------------------------------------------------- #
# Parsing helpers
# --------------------------------------------------------------------------- #


def _first_str(item: dict, *keys: str) -> str:
    for key in keys:
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, (int, float)):
            return str(value)
    return ""


def _as_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        digits = "".join(ch for ch in value if ch.isdigit())
        return int(digits) if digits else None
    return None


def _as_confidence(value: object) -> float:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.5
    # Models sometimes answer on a 0-100 scale despite being asked for 0-1.
    if number > 1.0:
        number /= 100.0
    return min(max(number, 0.0), 1.0)


def _resolve_path(path: str, by_path: dict[str, FileDiff]) -> str | None:
    """Match a path the model shortened or prefixed.

    `src/vibekill/llm.py` reported as `llm.py` is a formatting slip, not a
    hallucination, and is worth recovering when it is unambiguous.
    """
    if not path:
        return None
    candidates = [p for p in by_path if p.endswith(path) or path.endswith(p)]
    if len(candidates) == 1:
        return candidates[0]
    basename = path.rsplit("/", 1)[-1]
    candidates = [p for p in by_path if p.rsplit("/", 1)[-1] == basename]
    return candidates[0] if len(candidates) == 1 else None


def evidence_for_categories(
    evidence: Iterable[Finding], categories: Iterable[Category]
) -> list[Finding]:
    """Filter helper used by the concrete agents."""
    wanted = set(categories)
    return [f for f in evidence if f.category in wanted]
