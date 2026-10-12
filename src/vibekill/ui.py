"""Streamlit interface for people who prefer not to use the CLI.

Run with ``vibekill ui``. Reviews execute in this process; repository paths are
therefore paths on the machine running Streamlit, not paths on the browser's
computer.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import streamlit as st

from vibekill.models import ReviewResult, Severity, Verdict
from vibekill.pipeline import SourceError, run_review
from vibekill.report import (
    VERSION,
    render_html,
    render_json,
    render_markdown,
    render_sarif,
)
from vibekill.settings import Settings, find_config_file

st.set_page_config(page_title="vibekill", page_icon="💀", layout="wide")

SEVERITY_COLORS = {
    "blocker": "#c2303a",
    "major": "#d2761b",
    "minor": "#3b6fd4",
    "info": "#6b7580",
}
VERDICT_TEXT = {
    Verdict.REQUEST_CHANGES: ("Changes requested", "#c2303a"),
    Verdict.COMMENT: ("Comments to address", "#d2761b"),
    Verdict.APPROVE: ("No blocking issues", "#2d8a4e"),
}


def review_form() -> tuple[bool, dict[str, Any]]:
    """Render the input form and return ``(submitted, values)``."""
    st.sidebar.title("💀 vibekill")
    st.sidebar.caption(f"Kills the vibe before your diff kills production · {VERSION}")

    mode = st.sidebar.radio(
        "Review source",
        ["airgapped", "github"],
        format_func=lambda value: "Local repository" if value == "airgapped" else "GitHub PR",
        help="Local mode blocks public egress. GitHub mode permits only GitHub and the configured model host.",
    )

    with st.sidebar.form("review", border=False):
        values: dict[str, Any] = {"mode": mode}
        if mode == "airgapped":
            st.subheader("1 · Repository")
            values["repo_path"] = st.text_input(
                "Path on this machine",
                value=".",
                help="The folder must be a Git repository on the machine running Streamlit.",
            )
            comparison = st.radio("Review", ["Commit range", "Working tree"])
            values["include_uncommitted"] = comparison == "Working tree"
            if values["include_uncommitted"]:
                values["base"], values["head"] = "HEAD", "HEAD"
                st.caption("Includes staged, unstaged, and untracked text files.")
            else:
                values["base"] = st.text_input("Base ref", value="HEAD~1")
                values["head"] = st.text_input("Head ref", value="HEAD")
        else:
            st.subheader("1 · Pull request")
            values["github_repo"] = st.text_input("Repository", placeholder="owner/name")
            values["pr_number"] = st.number_input("PR number", min_value=1, step=1, value=None)
            st.caption(
                "Fetch-only: Vibekill will not post to the PR. GITHUB_TOKEN is optional for "
                "public repositories and required for private ones."
            )

        st.subheader("2 · Analysis")
        provider = st.selectbox(
            "Model provider",
            ["auto", "none", "ollama", "openai"],
            format_func=lambda value: {
                "auto": "Auto-detect local model",
                "none": "Deterministic checks only (fastest)",
                "ollama": "Ollama",
                "openai": "OpenAI-compatible endpoint",
            }[value],
        )
        values["provider"] = provider
        if provider not in {"none", "auto"}:
            values["llm_base_url"] = st.text_input(
                "Model URL", value="http://127.0.0.1:11434"
            )
            values["model"] = st.text_input("Model", value="qwen2.5-coder:7b")

        values["fail_on"] = st.selectbox(
            "Request changes at",
            ["blocker", "major", "minor", "info"],
            help="The lowest severity that changes the verdict and CLI exit code.",
        )
        submitted = st.form_submit_button("Run review", type="primary", use_container_width=True)

    if mode == "airgapped":
        st.sidebar.info("🔒 Local mode blocks public egress by default.")
    else:
        st.sidebar.warning(
            "GitHub and the configured model endpoint are allowed. Use a loopback model URL "
            "to keep model processing local."
        )
    return submitted, values


def build_settings(values: dict[str, Any]) -> tuple[Settings, Path | None]:
    """Load repository policy first, then apply values selected in the form."""
    config_file: Path | None = None
    search_from: Path | None = None
    if values["mode"] == "airgapped":
        search_from = Path(values["repo_path"]).expanduser()
        config_file = find_config_file(search_from)

    overrides: dict[str, Any] = {
        "mode": values["mode"],
        "fail_on": values["fail_on"],
        "llm": {"provider": values["provider"]},
    }
    if values.get("llm_base_url"):
        overrides["llm"]["base_url"] = values["llm_base_url"]
    if values.get("model"):
        overrides["llm"]["model"] = values["model"]
    if values["mode"] == "github":
        overrides["github"] = {
            "repo": values.get("github_repo", ""),
            "pr_number": values.get("pr_number"),
        }
    return Settings.load(config_file, search_from=search_from, **overrides), config_file


def validate_inputs(values: dict[str, Any]) -> str | None:
    if values["mode"] == "github":
        if not re.fullmatch(r"[^/\s]+/[^/\s]+", values.get("github_repo", "")):
            return "Enter the GitHub repository as owner/name."
        if not values.get("pr_number"):
            return "Enter a pull request number."
        return None

    path = Path(values.get("repo_path", "")).expanduser()
    if not path.is_dir():
        return f"Repository path does not exist or is not a directory: {path}"
    if not (path / ".git").exists():
        return f"No .git directory found at {path}. Choose the repository root."
    if not values.get("include_uncommitted") and (
        not values.get("base") or not values.get("head")
    ):
        return "Both base and head refs are required."
    return None


def input_snapshot(values: dict[str, Any]) -> str:
    """Stable, secret-free identity used to detect stale displayed results."""
    safe = {key: value for key, value in values.items() if "token" not in key and "key" not in key}
    return json.dumps(safe, sort_keys=True, default=str)


def show_header(result: ReviewResult) -> None:
    label, color = VERDICT_TEXT[result.verdict]
    meta = result.context.meta
    scope = (
        f"{meta.root} · PR #{meta.pr_number} · {meta.base_ref} → {meta.head_ref}"
        if meta.pr_number
        else f"{meta.root} · {meta.base_ref} → {meta.head_ref}"
    )
    st.subheader(meta.title or "Code review")
    st.caption(scope)
    st.markdown(
        f"<span style='background:{color};color:white;padding:5px 12px;border-radius:5px;"
        f"font-weight:650'>{label}</span>",
        unsafe_allow_html=True,
    )

    counts = result.counts()
    columns = st.columns(6)
    for column, severity in zip(columns, ["blocker", "major", "minor", "info"], strict=True):
        column.metric(severity.title(), counts[severity])
    columns[4].metric("Files", len(result.context.files))
    columns[5].metric("Time", f"{result.duration_s:.1f}s")

    policy = result.policy
    st.caption(
        f"Threshold: {policy.get('fail_on', 'blocker')} · "
        f"{result.dropped_unverified} unverifiable dropped · "
        f"{result.deduped} duplicate(s) merged · {result.suppressed} low-confidence hidden"
    )
    for warning in result.warnings:
        st.warning(warning)


def show_overview(result: ReviewResult) -> None:
    left, right = st.columns(2)
    counts = result.counts()
    with left:
        st.markdown("#### Findings by severity")
        st.bar_chart(
            {"Findings": counts},
            horizontal=True,
            color="#d2761b",
            height=260,
        )
    with right:
        st.markdown("#### Findings by category")
        category_counts: dict[str, int] = {}
        for finding in result.findings:
            category_counts[finding.category.value] = category_counts.get(finding.category.value, 0) + 1
        st.bar_chart(
            {"Findings": category_counts or {"none": 0}},
            horizontal=True,
            color="#3b6fd4",
            height=260,
        )

    file_rows = []
    for path, findings in result.iter_grouped_by_file():
        worst = max(findings, key=lambda finding: finding.severity.rank).severity.value
        file_rows.append({"File": path, "Findings": len(findings), "Highest severity": worst})
    if file_rows:
        st.markdown("#### Files to review first")
        st.dataframe(file_rows, hide_index=True, use_container_width=True)

    reviewed = len(result.context.reviewable_files())
    skipped = len(result.context.skipped_files)
    excluded = len(result.context.excluded_files)
    agent_ok = sum(not agent.error and not agent.skipped_reason for agent in result.agent_results)
    st.caption(
        f"Coverage: {reviewed} file(s) reviewed, {skipped} skipped, {excluded} excluded · "
        f"{agent_ok}/{len(result.agent_results)} model agents completed · "
        f"analysers used: {', '.join(result.tools_used) or 'none'}"
    )


def show_findings(result: ReviewResult) -> None:
    if not result.findings:
        st.success("Nothing was flagged in the reviewed scope. Check Coverage below before merging.")
        return

    categories = sorted({finding.category.value for finding in result.findings})
    files = sorted({finding.path or "(repository)" for finding in result.findings})
    first, second, third = st.columns(3)
    severities = first.multiselect(
        "Severity", [severity.value for severity in Severity], default=[s.value for s in Severity]
    )
    selected_categories = second.multiselect("Category", categories, default=categories)
    selected_files = third.multiselect("File", files, default=files)

    visible = [
        finding
        for finding in result.findings
        if finding.severity.value in severities
        and finding.category.value in selected_categories
        and (finding.path or "(repository)") in selected_files
    ]
    st.caption(f"Showing {len(visible)} of {len(result.findings)} findings")

    current_file: str | None = None
    for finding in visible:
        if finding.path != current_file:
            current_file = finding.path
            st.markdown(f"#### `{current_file or '(repository)'}`")
        location = f":{finding.line}" if finding.line else ""
        with st.expander(
            f"{finding.severity.value.upper()} · {finding.title}{location}",
            expanded=finding.severity in {Severity.BLOCKER, Severity.MAJOR},
        ):
            color = SEVERITY_COLORS[finding.severity.value]
            st.markdown(
                f"<b style='color:{color}'>{finding.severity.value}</b> · "
                f"{finding.category.value} · via {finding.agent} · "
                f"confidence {finding.confidence:.2f}",
                unsafe_allow_html=True,
            )
            st.write(finding.body)
            if finding.evidence:
                st.code(finding.evidence, language=None)
            if finding.suggestion:
                st.info(f"**Suggested fix:** {finding.suggestion}")
            st.caption(finding.rule_id or "Model finding")


def show_execution(result: ReviewResult) -> None:
    st.markdown("#### Model agents")
    agent_rows = [
        {
            "Agent": agent.name,
            "Status": "failed" if agent.error else ("skipped" if agent.skipped_reason else "ok"),
            "Raw findings": len(agent.findings),
            "Time (s)": round(agent.duration_s, 1),
            "Detail": agent.error or agent.skipped_reason or agent.summary,
        }
        for agent in result.agent_results
    ]
    st.dataframe(agent_rows, hide_index=True, use_container_width=True)
    st.caption("Raw findings are shown before evidence filtering and cross-agent deduplication.")

    st.markdown("#### Deterministic analysers")
    if result.analyzer_results:
        rows = [
            {
                "Analyser": item["name"],
                "Status": "ok" if item["ran"] else ("failed" if item["error"] else "skipped"),
                "Findings": item["finding_count"],
                "Detail": item["error"] or item["skipped_reason"] or "Completed",
            }
            for item in result.analyzer_results
        ]
        st.dataframe(rows, hide_index=True, use_container_width=True)
    else:
        st.caption("No external analysers were configured. Built-in heuristics may still have run.")


def show_downloads(result: ReviewResult) -> None:
    st.write("Download a self-contained report. The HTML file works without internet access.")
    columns = st.columns(4)
    downloads = [
        ("HTML", render_html(result), "review.html", "text/html"),
        ("Markdown", render_markdown(result), "review.md", "text/markdown"),
        ("JSON", render_json(result), "review.json", "application/json"),
        ("SARIF", render_sarif(result), "review.sarif", "application/sarif+json"),
    ]
    for column, (label, data, filename, mime) in zip(columns, downloads, strict=True):
        column.download_button(label, data=data, file_name=filename, mime=mime, use_container_width=True)


def main() -> None:
    submitted, values = review_form()
    st.title("Kills the vibe before your diff kills production")
    if values["mode"] == "airgapped":
        st.caption("Review a local Git diff. Local mode blocks public egress by default.")
    else:
        st.caption(
            "Fetch a PR from GitHub and review it locally. Code is sent only to the configured model endpoint."
        )

    if submitted:
        error = validate_inputs(values)
        if error:
            st.error(error)
        else:
            try:
                settings, config_file = build_settings(values)
                with st.status("Fetching and analyzing the diff…", expanded=True) as status:
                    st.write(
                        f"Provider: {settings.llm.provider} · "
                        f"Policy: {config_file or 'defaults and environment'}"
                    )
                    run = run_review(
                        settings,
                        repo_path=values.get("repo_path", "."),
                        base=values.get("base", "HEAD~1"),
                        head=values.get("head", "HEAD"),
                        include_uncommitted=values.get("include_uncommitted", False),
                        write=False,
                    )
                    status.update(label="Review complete", state="complete")
                st.session_state.review_result = run.result
                st.session_state.review_input = input_snapshot(values)
            except SourceError as exc:
                st.error(f"Could not read the diff: {exc}")
            except Exception as exc:  # noqa: BLE001 - user-facing process boundary
                st.error(f"Review failed: {type(exc).__name__}: {exc}")

    result: ReviewResult | None = st.session_state.get("review_result")
    if result is None:
        st.info("Complete the three steps in the sidebar, then select **Run review**.")
        return

    if st.session_state.get("review_input") != input_snapshot(values):
        st.warning("Inputs changed after this review. Run it again before relying on these results.")

    show_header(result)
    overview, findings, execution, downloads = st.tabs(
        ["Overview", "Findings", "Coverage", "Download"]
    )
    with overview:
        show_overview(result)
    with findings:
        show_findings(result)
    with execution:
        show_execution(result)
    with downloads:
        show_downloads(result)


if __name__ == "__main__":
    main()
