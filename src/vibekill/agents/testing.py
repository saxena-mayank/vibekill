"""Testing agent: does the test suite actually constrain this change?"""

from __future__ import annotations

from collections.abc import Sequence

from ..models import Category, FileDiff, Finding, ReviewContext, Severity
from .base import Agent, evidence_for_categories


class TestingAgent(Agent):
    name = "testing"
    default_category = Category.TESTING
    severity_cap = Severity.MAJOR
    description = "Checks whether tests meaningfully cover the new behaviour."

    def allowed_categories(self) -> list[Category]:
        return [Category.TESTING]

    def relevant_evidence(self, evidence: Sequence[Finding]) -> list[Finding]:
        return evidence_for_categories(evidence, [Category.TESTING])

    def files_to_review(self, context: ReviewContext) -> list[FileDiff]:
        """Needs both sides: the new logic and whatever tests accompany it.

        Reviewing only test files would miss the most important question, which is
        what went untested.
        """
        return context.reviewable_files()

    def extra_context(self, context: ReviewContext, _chunk: Sequence[FileDiff]) -> str:
        """Tell the model whether this change ships any tests at all."""
        test_files = [f.path for f in context.files if f.is_test_file]
        source_files = [f.path for f in context.reviewable_files() if not f.is_test_file]
        lines = []
        if test_files:
            lines.append("Test files in this change: " + ", ".join(test_files))
        else:
            lines.append("This change contains no test files.")
        if source_files:
            lines.append("Non-test files changed: " + ", ".join(source_files[:30]))
        return "\n".join(lines)

    def system_prompt(self) -> str:
        return """
You are reviewing the tests that accompany a code change. The question you are
answering is narrow: **would these tests fail if the implementation were wrong?**

A test that passes regardless of behaviour is worse than no test, because it
raises the coverage number and makes everyone less likely to look. Generated
tests fail this way constantly.

Look for:

1. Assertions that cannot fail in practice: asserting a constant, only checking
   the result is not null, only checking a type, or asserting on a mock you
   configured in the same test so the assertion just confirms your own setup.
2. New behaviour with no test at all. Be specific: name the function and the case.
   A new error path, a new branch, or a new validation rule with no test is the
   finding worth reporting.
3. Error and edge cases skipped while the happy path is covered three times.
   Empty input, boundary values, the failure branch of anything that can fail.
4. Over-mocking: so much mocked that the test exercises the mocks rather than
   the code. If every collaborator is faked, the test proves nothing about
   integration.
5. Tests that will be flaky: dependence on wall-clock time, real network or
   filesystem, ordering between tests, or a `sleep` standing in for
   synchronisation.
6. Tests asserting implementation detail rather than behaviour, so that any
   refactor breaks them for no reason.

Calibration:

- Do not ask for coverage of trivial code such as a plain getter or a dataclass.
- Do not demand a specific framework or style; judge the test on whether it
  constrains behaviour.
- If the change is genuinely well tested, say so and return an empty findings
  array. If it needs no tests at all, for instance a documentation change, say
  that too.
""".strip()
