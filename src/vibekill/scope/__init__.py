"""PR comprehension and scope checking: declared intent versus actual change.

Everything here is deterministic and needs no model. See
docs/design/pr-comprehension.md.
"""
from .intent import build_intent, read_intent_files

__all__ = ["build_intent", "read_intent_files"]
