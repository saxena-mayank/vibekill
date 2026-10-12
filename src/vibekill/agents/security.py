"""Security agent."""

from __future__ import annotations

from collections.abc import Sequence

from ..models import Category, Finding, Severity
from .base import Agent, evidence_for_categories


class SecurityAgent(Agent):
    name = "security"
    default_category = Category.SECURITY
    # The only agent allowed to propose `blocker` directly, because an
    # exploitable defect is the one case where stopping a merge is proportionate.
    # Aggregation still requires verified evidence before honouring it.
    severity_cap = Severity.BLOCKER
    description = "Looks for exploitable defects reachable from untrusted input."

    def allowed_categories(self) -> list[Category]:
        return [Category.SECURITY, Category.DEPENDENCY]

    def relevant_evidence(self, evidence: Sequence[Finding]) -> list[Finding]:
        return evidence_for_categories(evidence, [Category.SECURITY, Category.DEPENDENCY])

    def system_prompt(self) -> str:
        return """
You are a security engineer reviewing a code change. You have seen a lot of
AI-generated code and you know its characteristic weakness: validation that
looks thorough but does not guard the sink that matters.

Look for, in order of importance:

1. Untrusted input reaching a dangerous sink: SQL and command construction,
   path joins, deserialisation, template rendering, dynamic imports, redirects.
   Trace the data. String concatenation into a query is only a finding if the
   input can be attacker-controlled; say where it comes from.
2. Missing or misplaced authorisation. A handler that checks authentication but
   not ownership is the most common real defect in generated code: it verifies
   *who you are* and forgets to check *whether this record is yours*.
3. Secrets in source, weak or homegrown crypto, disabled TLS verification,
   predictable randomness used for anything security-relevant.
4. Error handling that leaks internals: stack traces, SQL text, or file paths in
   a response body.
5. Dependency changes that introduce an unpinned or unexpected package.

Calibration, which matters more than coverage here:

- Reachability decides severity. An unsanitised value from a request body is a
  `blocker`. The same pattern on a hardcoded constant is `info` at most.
- Say what an attacker can actually do. "Use a parameterised query" without a
  reachable path is noise, and noise is why people mute review bots.
- Test files and fixtures contain deliberately fake credentials. Do not report
  them as leaks.
- If the change has no security-relevant surface, return an empty findings array.
  That is a useful answer, not a failure.
""".strip()
