"""The one entry point every surface calls.

The CLI, the FastAPI service and the Streamlit UI all go through `run_review`.
That is deliberate: three surfaces calling three slightly different code paths is
how "it works in the CLI but not the API" bugs are born.

Responsibilities, in order: engage the air-gap, fetch the diff, run the graph,
write reports.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from .airgap import AirgapGuard, EgressBlocked, engage
from .graph import review as run_graph
from .llm import LLMClient, build_client
from .models import Comprehension, ReviewResult
from .report import write_reports
from .scope.grouping import group_files
from .scope.intent import build_intent, read_intent_files
from .settings import Settings
from .sources import SourceError, build_source

logger = logging.getLogger("vibekill.pipeline")

# Socket monkey-patching is process-global. Serializing complete reviews makes
# its lifecycle deterministic and also prevents several API requests from
# overwhelming one local model. Multi-worker deployments should use separate
# processes, which gives each worker its own guard.
_REVIEW_LOCK = threading.Lock()


@dataclass
class ReviewRun:
    """A completed review plus where its artefacts landed."""

    result: ReviewResult
    reports: dict[str, Path] = field(default_factory=dict)
    egress_violations: list[str] = field(default_factory=list)

    @property
    def exit_code(self) -> int:
        """0 when nothing met the failure threshold, 1 when something did."""
        from .models import Verdict

        return 1 if self.result.verdict is Verdict.REQUEST_CHANGES else 0


def run_review(
    settings: Settings,
    *,
    repo_path: Path | str = ".",
    base: str = "HEAD~1",
    head: str = "HEAD",
    include_uncommitted: bool = False,
    write: bool = True,
    output_dir: Path | None = None,
    formats: list[str] | None = None,
    pr_title: str | None = None,
    pr_body: str | None = None,
    intent_files: Sequence[Path | str] = (),
) -> ReviewRun:
    """Fetch, analyze, and render one review.

    `pr_title`, `pr_body` and `intent_files` declare what the change is for. Local git
    has no PR description, so the caller supplies it; GitHub mode reads it from the API.

    Reviews are serialized because the air-gap guard patches process-global
    sockets. Run multiple service processes for parallel repository reviews.
    """
    with _REVIEW_LOCK:
        return _run_review_locked(
            settings,
            repo_path=repo_path,
            base=base,
            head=head,
            include_uncommitted=include_uncommitted,
            write=write,
            output_dir=output_dir,
            formats=formats,
            pr_title=pr_title,
            pr_body=pr_body,
            intent_files=intent_files,
        )


def _run_review_locked(
    settings: Settings,
    *,
    repo_path: Path | str,
    base: str,
    head: str,
    include_uncommitted: bool,
    write: bool,
    output_dir: Path | None,
    formats: list[str] | None,
    pr_title: str | None,
    pr_body: str | None,
    intent_files: Sequence[Path | str],
) -> ReviewRun:
    guard: AirgapGuard | None = None
    if settings.enforce_airgap:
        guard = engage(settings.allow_hosts)
        logger.info("air-gap engaged; allowed hosts: %s", ", ".join(sorted(guard.allow_hosts)))
        if not settings.llm_host_allowed():
            logger.warning(
                "llm.base_url host %r is not in allow_hosts, so the model will be unreachable. "
                "Add it to allow_hosts or switch to --mode github.",
                settings.llm_host(),
            )

    try:
        source = build_source(
            settings,
            repo_path=repo_path,
            base=base,
            head=head,
            include_uncommitted=include_uncommitted,
        )
        context = source.fetch()

        if not context.reviewable_files():
            logger.warning("no reviewable files in this diff")

        intent_notes: list[str] = []
        if settings.scope.enabled:
            intent_notes = _attach_scope(context, settings, pr_title, pr_body, intent_files)
        client = build_client(settings.llm)
        result = run_graph(context, settings, client=client)
        result.warnings.extend(intent_notes)

        result.warnings.extend(settings.config_warnings)
        run = ReviewRun(result=result)
        if guard and guard.violations:
            # Something tried to reach the network during an air-gapped review.
            # Not fatal, since the attempt was blocked, but the operator needs
            # to know which component did it.
            run.egress_violations = [str(v) for v in guard.violations]
            result.warnings.append(
                f"{len(guard.violations)} blocked egress attempt(s) during this review: "
                + "; ".join(run.egress_violations[:5])
            )

        if write:
            target = Path(output_dir or settings.output_dir)
            if not target.is_absolute():
                root = Path(context.meta.root)
                target = (root / target) if root.is_dir() else target
            run.reports = write_reports(result, target, formats or ["markdown", "html", "json"])
            logger.info("wrote %s", ", ".join(str(p) for p in run.reports.values()))

        return run
    finally:
        if guard is not None:
            guard.release()


def _attach_scope(
    context,
    settings: Settings,
    pr_title: str | None,
    pr_body: str | None,
    intent_files: Sequence[Path | str],
) -> list[str]:
    """Build intent, group the changed files, store both on the context. Returns warnings."""
    meta = context.meta
    # In local mode `meta.title` is only the latest commit subject, which is not a
    # declared title. It is already in `commit_subjects`, so do not count it twice.
    title = pr_title if pr_title is not None else (meta.title if context.mode == "github" else "")
    body = pr_body if pr_body is not None else meta.description
    texts, read_notes = read_intent_files(intent_files)
    intent = build_intent(
        title=title,
        body=body,
        intent_texts=texts,
        commit_subjects=meta.commit_subjects,
    )
    intent.notes.extend(read_notes)
    files, notes = group_files(context, intent, settings.scope.core_min_score)
    context.comprehension = Comprehension(intent=intent, files=files, notes=notes)
    return list(read_notes)


def probe_model(settings: Settings) -> LLMClient:
    """Probe model availability under the same network policy as a review."""
    with _REVIEW_LOCK:
        guard = engage(settings.allow_hosts) if settings.enforce_airgap else None
        try:
            return build_client(settings.llm)
        finally:
            if guard is not None:
                guard.release()


def verify_airgap(settings: Settings) -> tuple[bool, str]:
    """Prove that the configured guard refuses public egress."""
    with _REVIEW_LOCK:
        return _verify_airgap_locked(settings)


def _verify_airgap_locked(settings: Settings) -> tuple[bool, str]:
    guard = engage(settings.allow_hosts)
    try:
        import socket

        try:
            socket.create_connection(("example.com", 443), timeout=3)
        except EgressBlocked:
            return True, (
                "a connection to example.com:443 was refused by the guard before any "
                "packet left the process"
            )
        except OSError as exc:
            # The connection failed for some other reason, so the guard was not
            # what stopped it. That is not proof of anything.
            return False, (
                f"connection failed before the guard could refuse it ({exc}); "
                "air-gap enforcement is unproven here"
            )
        return False, "a connection to example.com:443 succeeded: egress is NOT blocked"
    finally:
        guard.release()


__all__ = ["ReviewRun", "run_review", "probe_model", "verify_airgap", "SourceError"]
