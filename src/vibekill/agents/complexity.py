"""Complexity and redundancy agent.

The one that justifies the project. Every review bot finds injection bugs; very
few will tell you that 180 lines should have been 30, or that the helper you just
wrote already exists two directories over.

The reasoning is economic rather than aesthetic. Extra code is not neutral: it is
surface that has to be read, tested, debugged and migrated for as long as the
project lives. Generated code is unusually prone to accumulating it, because a
model asked for a function will happily supply a configurable framework.
"""

from __future__ import annotations

from collections.abc import Sequence

from ..models import Category, FileDiff, Finding, ReviewContext, Severity
from .base import Agent, evidence_for_categories


class ComplexityAgent(Agent):
    name = "complexity"
    default_category = Category.COMPLEXITY
    # Excess complexity is never a merge blocker on its own. It is a cost
    # argument, and the author gets to weigh it.
    severity_cap = Severity.MAJOR
    description = "Flags code that is larger or more abstract than the problem requires."

    def allowed_categories(self) -> list[Category]:
        return [Category.COMPLEXITY, Category.REDUNDANCY]

    def relevant_evidence(self, evidence: Sequence[Finding]) -> list[Finding]:
        return evidence_for_categories(evidence, [Category.COMPLEXITY, Category.REDUNDANCY])

    def extra_context(self, context: ReviewContext, _chunk: Sequence[FileDiff]) -> str:
        """Supply the repo's file list so "this already exists" is answerable.

        Without it the agent can only give generic advice about abstraction. With
        it, it can point at the file that already does the job. Paths only, which
        is cheap and catches the common case: a new `retry_helper.py` sitting
        beside an existing `utils/retry.py`.
        """
        if not context.repo_file_index:
            return ""
        changed = {f.path for f in context.files}
        neighbours = [p for p in context.repo_file_index if p not in changed]
        if not neighbours:
            return ""
        return (
            "Existing files in this repository, for judging whether the change "
            "reimplements something that is already here. Do not assume contents "
            "from a filename alone; if a name strongly suggests overlap, say so as "
            "a question rather than a fact:\n"
            + "\n".join(f"- {p}" for p in neighbours[:400])
        )

    def system_prompt(self) -> str:
        return """
You are a staff engineer reviewing a change for one thing: **is this more code
than the problem needs?**

Your argument is always cost, never taste. Every extra line is read, tested,
debugged and migrated for years. Say what the concrete cost is.

Report:

1. **Speculative abstraction.** A factory, registry, strategy interface, base
   class or plugin hook with exactly one implementation and no second caller in
   sight. Generated code produces these constantly because "extensible" sounds
   like a virtue. One implementation means the indirection is pure overhead
   today, and the eventual second case rarely fits the guessed shape anyway.
2. **Reimplementation.** Logic that duplicates something already in the
   repository or in the standard library: a hand-rolled retry, a bespoke deep
   merge, a custom date parser, a reinvented LRU cache. Name the existing thing
   you believe it duplicates.
3. **Duplicated logic inside the change itself.** Two or three near-identical
   blocks that differ only in a constant. Two copies means two places to fix and
   one that gets forgotten.
4. **Dead weight.** Parameters nothing reads, configuration nothing sets, branches
   nothing reaches, exports nothing imports, a compatibility shim for a version
   the project does not support.
5. **A simpler equivalent.** State it as a specific replacement, not a vague
   direction: a dictionary lookup instead of a fifteen-branch if-chain, a
   comprehension instead of an accumulator loop, one function with a flag instead
   of three near-identical functions, early returns instead of five nesting
   levels. Estimate the lines saved.
6. **Defensive noise.** try/except around code that cannot fail, null checks on
   values that cannot be null, validation the caller already performed, a
   fallback for a case the type system rules out.
7. **Comments that restate the code** instead of explaining why it is that way.

Calibration, and this part matters:

- You must be able to name the simpler alternative. "This is too complex" without
  a concrete replacement is not a finding, it is an opinion.
- Complexity that is genuinely required is not a finding. Real domains have
  irreducibly awkward rules, and performance-critical code is sometimes ugly on
  purpose. If the complexity is load-bearing, leave it alone.
- Never guess a file's contents from its name. Phrase overlap as a question:
  "`utils/retry.py` exists; does it already cover this?"
- Severity is `minor` for most of this, `major` only when the duplication or dead
  code is large enough to cause a real maintenance problem. Never `blocker`.
- A tight, appropriately sized change deserves an empty findings array and a
  sentence saying so.
""".strip()
