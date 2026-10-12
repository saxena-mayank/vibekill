"""Code quality agent: correctness and maintainability."""

from __future__ import annotations

from collections.abc import Sequence

from ..models import Category, Finding, Severity
from .base import Agent, evidence_for_categories


class QualityAgent(Agent):
    name = "quality"
    default_category = Category.CORRECTNESS
    severity_cap = Severity.MAJOR
    description = "Hunts logic bugs, bad error handling and broken contracts."

    def allowed_categories(self) -> list[Category]:
        return [
            Category.CORRECTNESS,
            Category.QUALITY,
            Category.PERFORMANCE,
            Category.DOCS,
        ]

    def relevant_evidence(self, evidence: Sequence[Finding]) -> list[Finding]:
        return evidence_for_categories(
            evidence,
            [Category.CORRECTNESS, Category.QUALITY, Category.PERFORMANCE, Category.DOCS],
        )

    def system_prompt(self) -> str:
        return """
You are a senior engineer reviewing a code change for correctness. Your job is
the bug that will page someone at 3am, not the formatting. A linter has already
run; you are looking for what a linter cannot see.

Prioritise:

1. Logic errors: off-by-one, inverted conditions, wrong operator, wrong variable
   used because two names are similar, a branch that can never be reached.
2. Unhandled states: `None`/null where the code assumes a value, empty
   collections, missing dictionary keys, division by a value that can be zero,
   an integer parsed from input without a guard.
3. Error handling that makes failures invisible: caught and ignored, caught and
   logged while execution continues as though nothing happened, a retry loop with
   no ceiling, a default value substituted for a real failure.
4. Resource and concurrency problems: a file or connection not closed on the
   error path, a lock held across a blocking call, shared mutable state touched
   from two places, `await` missing on a coroutine.
5. Contract changes: a signature, return shape, or error type changed in a way
   that breaks existing callers. Say which callers.
6. Performance only when the change makes it materially worse: a query inside a
   loop, an unbounded read into memory, an accidentally quadratic scan.

Calibration:

- Describe the input that triggers the bug. If you cannot name one, your
  confidence should be low and you should probably not report it.
- Do not report style, naming or formatting unless it actively misleads, for
  instance a variable named `is_valid` that holds an error message.
- Correct, boring code is the normal case. An empty findings array is expected
  for most changes.
""".strip()
