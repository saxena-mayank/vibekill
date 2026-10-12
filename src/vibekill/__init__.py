"""vibekill — kills the vibe before your diff kills production.

Offline agentic code review, built for the failure modes of AI-generated code:
plausible-looking changes that pass review because nothing *looks* wrong.

Four agents review a git diff in parallel — security, code quality, testing, and
complexity/redundancy. Deterministic analysers run first and hand the agents their
output as evidence, so a review is useful even with no model available.

Typical use::

    from vibekill import Settings, run_review

    run = run_review(Settings.load(), base="main", head="HEAD")
    verdict = run.result.verdict

Layout:

    settings.py   configuration
    airgap.py     socket-level egress guard
    diffparse.py  unified diff parser
    sources.py    local git and GitHub PR adapters
    analysis/     deterministic checks (heuristics + external tools)
    llm.py        local model client
    agents/       the four reviewing agents
    graph.py      LangGraph orchestrator
    aggregate.py  evidence gate, dedup, severity policy
    report/       markdown, HTML, JSON, SARIF
    pipeline.py   the entry point the CLI, API and UI share

See docs/ARCHITECTURE.md for how these fit together.
"""

from ._version import __version__
from .models import (
    AgentResult,
    Category,
    Finding,
    ReviewContext,
    ReviewResult,
    Severity,
    Verdict,
)
from .pipeline import ReviewRun, probe_model, run_review, verify_airgap
from .settings import Settings

__all__ = [
    "__version__",
    "Settings",
    "run_review",
    "probe_model",
    "verify_airgap",
    "ReviewRun",
    "ReviewResult",
    "ReviewContext",
    "Finding",
    "AgentResult",
    "Severity",
    "Category",
    "Verdict",
]
