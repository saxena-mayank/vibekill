"""Report rendering: Markdown, HTML, JSON and SARIF.

All four read the same `ReviewResult`, so the terminal output and the HTML file
can never disagree about what was found.

Design notes:

* **HTML is self-contained.** Styles are inline and there are no external assets,
  because the report frequently has to open on a machine with no network, from a
  share drive or an email attachment.
* **Dropped and skipped counts are always shown.** A review that quietly discarded
  six findings looks identical to one that found nothing, and those are very
  different situations.
* **SARIF** exists so GitHub-connected deployments can upload results to code
  scanning and get inline annotations without vibekill posting comments itself.
"""

from __future__ import annotations

import html
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .._version import __version__ as VERSION
from ..models import ReviewResult, Severity, Verdict

VERDICT_LABELS = {
    Verdict.REQUEST_CHANGES: "Changes requested",
    Verdict.COMMENT: "Comments to address",
    Verdict.APPROVE: "No blocking issues",
}

SEVERITY_ICONS = {
    Severity.BLOCKER: "🛑",
    Severity.MAJOR: "⚠️",
    Severity.MINOR: "•",
    Severity.INFO: "·",
}


def _mode_label(result: ReviewResult) -> str:
    if result.context.mode == "github":
        return "GitHub-connected mode"
    if result.airgapped:
        return "Air-gapped local mode"
    return "Guarded local mode (external host allowed)"


# --------------------------------------------------------------------------- #
# Markdown
# --------------------------------------------------------------------------- #


def render_markdown(result: ReviewResult) -> str:
    """Terminal- and CI-friendly report. Also used as the GitHub review body."""
    meta = result.context.meta
    counts = result.counts()
    out: list[str] = []

    title = _one_line(meta.title or f"{meta.base_ref} → {meta.head_ref}")
    out.append(f"# Review: {title}")
    out.append("")
    scope = f"`{meta.root}` · `{meta.base_ref}` → `{meta.head_ref}`"
    if meta.pr_number:
        scope += f" · PR #{meta.pr_number}"
    out.append(scope)
    out.append("")
    out.append(
        f"**{VERDICT_LABELS[result.verdict]}** · {_mode_label(result)} · "
        f"{len(result.context.files)} file(s), +{result.context.additions}/-{result.context.deletions} · "
        f"{result.duration_s:.1f}s"
    )
    out.append("")
    out.append(
        f"| blocker | major | minor | info |\n|---|---|---|---|\n"
        f"| {counts['blocker']} | {counts['major']} | {counts['minor']} | {counts['info']} |"
    )
    out.append("")

    if result.dropped_unverified or result.deduped or result.suppressed:
        out.append(
            f"_{result.dropped_unverified} finding(s) discarded as unverifiable, "
            f"{result.deduped} merged as duplicates, {result.suppressed} below the "
            f"confidence floor._"
        )
        out.append("")

    for warning in result.warnings:
        out.append(f"> **Note:** {warning}")
        out.append("")

    if result.findings:
        out.append("## Findings")
        out.append("")
        for path, findings in result.iter_grouped_by_file():
            out.append(f"### `{path}`")
            out.append("")
            for finding in findings:
                icon = SEVERITY_ICONS[finding.severity]
                location = f":{finding.line}" if finding.line else ""
                out.append(f"#### {icon} {finding.title} `{finding.severity.value}`{location}")
                out.append("")
                out.append(finding.body)
                out.append("")
                if finding.evidence:
                    out.extend(_markdown_code_block(finding.evidence))
                    out.append("")
                if finding.suggestion:
                    out.append(f"**Fix:** {finding.suggestion}")
                    out.append("")
                details = [
                    finding.category.value,
                    f"via {finding.agent}",
                    f"confidence {finding.confidence:.2f}",
                ]
                if finding.rule_id:
                    details.append(f"`{finding.rule_id}`")
                if finding.corroborated_by:
                    details.append("corroborated by " + ", ".join(html.escape(s) for s in finding.corroborated_by))
                out.append("<sub>" + " · ".join(html.escape(d) for d in details) + "</sub>")
                out.append("")
    else:
        out.append("## Findings")
        out.append("")
        out.append("Nothing flagged in the reviewed scope.")
        out.append("")

    out.append("## Agents")
    out.append("")
    out.append("| Agent | Status | Findings | Time | Summary |")
    out.append("|---|---|---|---|---|")
    for agent in result.agent_results:
        status = "failed" if agent.error else ("skipped" if agent.skipped_reason else "ok")
        note = _one_line(agent.error or agent.skipped_reason or agent.summary or "").replace(
            "|", "\\|"
        )
        out.append(
            f"| {agent.name} | {status} | {len(agent.findings)} | {agent.duration_s:.1f}s | {note[:160]} |"
        )
    out.append("")
    out.append(
        f"<sub>vibekill {VERSION} · analysers: "
        f"{', '.join(result.tools_used) or 'none'} · advisory only</sub>"
    )
    return "\n".join(out)


def _one_line(text: str) -> str:
    return " ".join(text.split())


def _markdown_code_block(text: str) -> list[str]:
    """Choose a fence longer than any backtick run in untrusted model output."""
    fence = "```"
    while fence in text:
        fence += "`"
    return [fence, text, fence]


# --------------------------------------------------------------------------- #
# HTML
# --------------------------------------------------------------------------- #


def render_html(result: ReviewResult) -> str:
    from jinja2 import Environment, FileSystemLoader, select_autoescape

    env = Environment(
        loader=FileSystemLoader(str(Path(__file__).parent)),
        autoescape=select_autoescape(["html"]),
    )
    template = env.get_template("template.html")

    payload = result.to_dict()
    meta = result.context.meta
    model = next((a.model for a in result.agent_results if a.model and a.model != "none"), "none")

    category_counts: dict[str, int] = {}
    file_counts: dict[str, int] = {}
    for finding in result.findings:
        category_counts[finding.category.value] = category_counts.get(finding.category.value, 0) + 1
        path = finding.path or "(repository)"
        file_counts[path] = file_counts.get(path, 0) + 1

    def bars(counts: dict[str, int]) -> list[dict[str, Any]]:
        maximum = max(counts.values(), default=1)
        return [
            {"label": label, "count": count, "width": round(100 * count / maximum)}
            for label, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
        ]

    return template.render(
        title=meta.title or f"{meta.base_ref} → {meta.head_ref}",
        repo=payload["repo"],
        stats=payload["stats"],
        verdict=result.verdict.value,
        verdict_label=VERDICT_LABELS[result.verdict],
        mode_label=_mode_label(result),
        airgapped=result.airgapped,
        guard_enabled=result.policy.get("network_guard_enabled", result.airgapped),
        require_evidence=result.policy.get("require_evidence", True),
        fail_on=result.policy.get("fail_on", "blocker"),
        model=model,
        tools_used=result.tools_used,
        analyzers=payload["analyzers"],
        category_bars=bars(category_counts),
        file_bars=bars(dict(sorted(file_counts.items(), key=lambda item: -item[1])[:8])),
        duration=f"{result.duration_s:.1f}",
        generated_at=datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
        warnings=result.warnings,
        agents=payload["agents"],
        findings_by_file=[
            (path, [f.to_dict() for f in findings])
            for path, findings in result.iter_grouped_by_file()
        ],
        version=VERSION,
    )


# --------------------------------------------------------------------------- #
# JSON and SARIF
# --------------------------------------------------------------------------- #


def render_json(result: ReviewResult) -> str:
    return json.dumps(result.to_dict(), indent=2)


# SARIF wants a 4-level scale; ours maps cleanly onto three of them.
_SARIF_LEVEL = {
    Severity.BLOCKER: "error",
    Severity.MAJOR: "error",
    Severity.MINOR: "warning",
    Severity.INFO: "note",
}


def render_sarif(result: ReviewResult) -> str:
    """SARIF 2.1.0, for upload to GitHub code scanning.

    Lets a connected deployment get inline PR annotations through GitHub's own
    machinery instead of vibekill posting comments, which most orgs prefer.
    """
    rules: dict[str, dict[str, Any]] = {}
    results: list[dict[str, Any]] = []

    for finding in result.findings:
        rule_id = finding.rule_id or f"vibekill.{finding.agent}.{finding.category.value}"
        rules.setdefault(
            rule_id,
            {
                "id": rule_id,
                "name": rule_id.replace(".", "_"),
                "shortDescription": {"text": finding.title[:120]},
                "fullDescription": {"text": finding.body[:1000] or finding.title},
                "defaultConfiguration": {"level": _SARIF_LEVEL[finding.severity]},
                "properties": {"category": finding.category.value, "agent": finding.agent},
            },
        )
        entry: dict[str, Any] = {
            "ruleId": rule_id,
            "level": _SARIF_LEVEL[finding.severity],
            "message": {"text": f"{finding.title}\n\n{finding.body}".strip()},
            "partialFingerprints": {"vibekillFindingId/v1": finding.fingerprint()},
            "properties": {
                "severity": finding.severity.value,
                "confidence": round(finding.confidence, 3),
                "agent": finding.agent,
                "source": finding.source,
                "tags": finding.tags,
            },
        }
        if finding.path:
            region: dict[str, Any] = {}
            if finding.line:
                region["startLine"] = finding.line
                region["endLine"] = finding.end_line or finding.line
            entry["locations"] = [
                {
                    "physicalLocation": {
                        "artifactLocation": {"uri": finding.path},
                        **({"region": region} if region else {}),
                    }
                }
            ]
        results.append(entry)

    document = {
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "version": "2.1.0",
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": "vibekill",
                        "version": VERSION,
                        "rules": list(rules.values()),
                    }
                },
                "properties": {
                    "verdict": result.verdict.value,
                    "stats": result.to_dict()["stats"],
                    "policy": result.policy,
                    "warnings": result.warnings,
                },
                "results": results,
            }
        ],
    }
    return json.dumps(document, indent=2)


# --------------------------------------------------------------------------- #
# Writing
# --------------------------------------------------------------------------- #

RENDERERS = {
    "markdown": (render_markdown, "review.md"),
    "md": (render_markdown, "review.md"),
    "html": (render_html, "review.html"),
    "json": (render_json, "review.json"),
    "sarif": (render_sarif, "review.sarif"),
}


def write_reports(result: ReviewResult, output_dir: Path, formats: list[str]) -> dict[str, Path]:
    """Render the requested formats into `output_dir`. Returns format -> path."""
    unknown = [fmt for fmt in formats if fmt not in RENDERERS]
    if unknown:
        raise ValueError(f"unknown report format(s): {', '.join(unknown)}")

    output_dir.mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {}
    for fmt in dict.fromkeys(formats):  # de-duplicate, preserve order
        entry = RENDERERS.get(fmt)
        if entry is None:
            continue
        render, filename = entry
        path = output_dir / filename
        path.write_text(render(result), encoding="utf-8")
        written[fmt] = path
    return written
