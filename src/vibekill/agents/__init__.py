"""The reviewing agents.

`AGENT_CLASSES` is the registry the orchestrator builds its graph from. To add an
agent: subclass `Agent`, implement `system_prompt` and `relevant_evidence`, append
the class here, and add a matching field to `AgentsSettings` in `settings.py`.
Nothing else needs to change — `graph.py` wires nodes from this list.
"""

from .base import Agent
from .complexity import ComplexityAgent
from .quality import QualityAgent
from .security import SecurityAgent
from .testing import TestingAgent

AGENT_CLASSES: list[type[Agent]] = [
    SecurityAgent,
    QualityAgent,
    TestingAgent,
    ComplexityAgent,
]

AGENT_BY_NAME: dict[str, type[Agent]] = {cls.name: cls for cls in AGENT_CLASSES}

__all__ = [
    "Agent",
    "AGENT_CLASSES",
    "AGENT_BY_NAME",
    "SecurityAgent",
    "QualityAgent",
    "TestingAgent",
    "ComplexityAgent",
]
