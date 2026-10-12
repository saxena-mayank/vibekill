"""The orchestrator, built as a LangGraph state graph.

Shape::

    START -> analyse -> ┬-> security   ─┐
                        ├-> quality    ─┤
                        ├-> testing    ─┼-> aggregate -> END
                        └-> complexity ─┘

`analyse` runs the deterministic layer first so the agents receive its output as
evidence. The four agent nodes have no edges between them, so LangGraph schedules
them in a single super-step and they execute concurrently.

Concurrency, stated honestly: the agent nodes really do run at the same time, but
their model calls pass through a semaphore inside `LLMClient` (default 2). One
local runtime does not go faster with four callers waiting on it — the weights
cross the memory bus per token regardless. The parallelism that always pays off
is the deterministic layer, which is separate processes doing CPU work.

Concurrent writes to shared state need a reducer, so `agent_results` is annotated
with `operator.add` and each node appends exactly one entry. Without that, four
nodes writing the same key would overwrite each other and three results would
vanish.
"""

from __future__ import annotations

import logging
import operator
import time
from collections.abc import Sequence
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, START, StateGraph

from .agents import AGENT_CLASSES, Agent
from .aggregate import aggregate
from .analysis import run_heuristics, run_tools
from .analysis.tools import ToolOutcome, restrict_to_changed_lines
from .llm import LLMClient, build_client
from .models import AgentResult, Finding, ReviewContext, ReviewResult
from .settings import Settings

logger = logging.getLogger("vibekill.graph")


class ReviewState(TypedDict, total=False):
    """State passed between graph nodes.

    `agent_results` uses an additive reducer because the four agent nodes write it
    concurrently. Every other key is written by exactly one node.
    """

    context: ReviewContext
    evidence: list[Finding]
    tool_outcomes: list[ToolOutcome]
    agent_results: Annotated[list[AgentResult], operator.add]
    warnings: Annotated[list[str], operator.add]
    result: ReviewResult
    started_at: float


def build_graph(settings: Settings, client: LLMClient, agents: Sequence[Agent] | None = None):
    """Compile the review graph.

    `agents` is injectable so tests can run the graph with a single stub agent.
    """
    active = list(agents) if agents is not None else [cls(settings, client) for cls in AGENT_CLASSES]

    graph = StateGraph(ReviewState)
    graph.add_node("analyse", _make_analyse_node(settings))
    graph.add_edge(START, "analyse")

    for agent in active:
        graph.add_node(agent.name, _make_agent_node(agent))
        # No edges between agent nodes: same super-step, run concurrently.
        graph.add_edge("analyse", agent.name)

    graph.add_node("aggregate", _make_aggregate_node(settings, client))
    for agent in active:
        # Every agent must report before aggregation; LangGraph joins here.
        graph.add_edge(agent.name, "aggregate")
    graph.add_edge("aggregate", END)

    return graph.compile()


# --------------------------------------------------------------------------- #
# Nodes
# --------------------------------------------------------------------------- #


def _make_analyse_node(settings: Settings):
    """Deterministic analysis. Produces the evidence pack the agents reason over."""

    def analyse(state: ReviewState) -> dict[str, Any]:
        context = state["context"]
        warnings: list[str] = []

        heuristic_findings: list[Finding] = []
        if settings.tools.heuristics:
            heuristic_findings = run_heuristics(context)
            if settings.tools.changed_lines_only:
                heuristic_findings = restrict_to_changed_lines(heuristic_findings, context)

        tool_findings, outcomes = run_tools(context, settings)

        for outcome in outcomes:
            if outcome.error:
                warnings.append(f"{outcome.name} failed: {outcome.error}")
            elif outcome.skipped_reason:
                # Not a warning: a locked-down environment legitimately has none
                # of these installed, and the review is still valid without them.
                logger.info("%s skipped: %s", outcome.name, outcome.skipped_reason)

        evidence = heuristic_findings + tool_findings
        logger.info(
            "deterministic analysis produced %d finding(s) across %d file(s)",
            len(evidence),
            len(context.reviewable_files()),
        )
        return {"evidence": evidence, "tool_outcomes": outcomes, "warnings": warnings}

    return analyse


def _make_agent_node(agent: Agent):
    """Wrap one agent as a node that never raises.

    An agent that crashes must degrade to a recorded failure, not abort the
    review. A partial review is useful; a traceback is not.
    """

    def node(state: ReviewState) -> dict[str, Any]:
        context = state["context"]
        evidence = state.get("evidence", [])
        try:
            result = agent.run(context, evidence)
        except Exception as exc:  # noqa: BLE001 - deliberate containment boundary
            logger.exception("%s agent crashed", agent.name)
            result = AgentResult(
                name=agent.name,
                error=f"{type(exc).__name__}: {exc}",
                summary="This agent failed; its findings are missing from this review.",
            )
        return {"agent_results": [result]}

    return node


def _make_aggregate_node(settings: Settings, client: LLMClient):
    """Evidence gate, dedup, severity policy, verdict."""

    def node(state: ReviewState) -> dict[str, Any]:
        context = state["context"]
        evidence = state.get("evidence", [])
        agent_results = list(state.get("agent_results", []))
        outcomes = state.get("tool_outcomes", [])
        warnings = list(state.get("warnings", []))

        # Model usage is tracked centrally on the client, so distribute it for
        # reporting rather than threading counters through every call site.
        total_calls = client.usage.calls
        if agent_results and total_calls:
            share = total_calls // len(agent_results)
            for result in agent_results:
                result.llm_calls = share

        if not client.available:
            warnings.append(
                "No local model was reachable, so this review covers deterministic analysis only. "
                f"Configure llm.base_url (currently {settings.llm.base_url}) to enable the agents."
            )

        result = aggregate(
            context,
            agent_results,
            evidence,
            settings,
            duration_s=time.monotonic() - state.get("started_at", time.monotonic()),
            tools_used=[o.name for o in outcomes if o.ran] + (["heuristics"] if settings.tools.heuristics else []),
            analyzer_results=[outcome.to_dict() for outcome in outcomes],
            warnings=warnings,
        )
        return {"result": result}

    return node


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def review(
    context: ReviewContext,
    settings: Settings,
    *,
    client: LLMClient | None = None,
) -> ReviewResult:
    """Run a full review over an already-fetched context.

    The single function the CLI, the API and the UI all call, so the three
    surfaces cannot drift in behaviour.
    """
    client = client or build_client(settings.llm)
    graph = build_graph(settings, client)

    logger.info(
        "reviewing %d file(s) with %s", len(context.reviewable_files()), client.describe()
    )
    final_state = graph.invoke(
        {"context": context, "started_at": time.monotonic()},
        # One super-step for analysis, one for the agents, one for aggregation.
        # A low ceiling turns an accidental cycle into a fast error.
        {"recursion_limit": 12},
    )
    result: ReviewResult = final_state["result"]
    return result
