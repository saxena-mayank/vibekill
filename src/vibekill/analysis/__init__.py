"""Deterministic analysis: the layer that runs before any model.

`heuristics` needs nothing but Python. `tools` wraps external analysers and is
entirely optional. Together they produce the evidence pack the agents reason
over, which is what lets a review still be useful with the model switched off.
"""

from .heuristics import run_heuristics
from .tools import ToolOutcome, restrict_to_changed_lines, run_tools

__all__ = ["run_heuristics", "run_tools", "ToolOutcome", "restrict_to_changed_lines"]
