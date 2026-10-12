# Contributing to vibekill

Contributions are welcome. This file exists so you can add something useful
without reading the whole codebase first.

The project's job is to catch what a human misses when skimming a tidy,
AI-generated diff. Keep that in mind when deciding whether something belongs: a
check that catches formatting is a linter's job, and we are not competing with
linters.

## Setup

Fork and clone the repository, then:

```bash
cd vibekill
python -m venv .venv && source .venv/bin/activate
pip install -e ".[ui,dev,analysers]"

make test    # 100+ tests, under two seconds, no network
make lint
make demo    # review a seeded repo full of deliberate defects
```

Read [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) first if you are changing
structure. For adding a check or an agent, this file is enough.

## The one thing to keep in mind

**Precision beats recall here.** A reviewer that reports things people disagree
with gets switched off, and then it catches nothing at all. When you add a check,
the bar is not "this is sometimes a problem" but "a reviewer seeing this would
agree it is worth a look". If a check fires on reasonable code, it is a bug even
when the underlying concern is real.

Corollary: a check that needs a comment explaining when to ignore it probably
needs a narrower condition instead.

## Add a heuristic

Highest-value contribution, because heuristics work with no model and no external
tools — the configuration many air-gapped users are stuck with.

One function in `src/vibekill/analysis/heuristics.py`:

```python
@check("resource_leak")
def check_unclosed_file(file_diff: FileDiff, context: ReviewContext) -> list[Finding]:
    """Flag open() outside a with-statement."""
    if file_diff.language != "python" or not file_diff.new_content:
        return []

    findings = []
    changed = file_diff.added_line_numbers()
    tree = ast.parse(file_diff.new_content)   # callers handle SyntaxError
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not _touches(node, changed):
            continue
        if getattr(node.func, "id", None) != "open":
            continue
        findings.append(_finding(
            file_diff,
            rule="correctness.unclosed_file",
            title="File opened outside a with-statement",
            body="If an exception is raised before close(), the handle leaks. "
                 "Use `with open(...) as f:` so cleanup happens on every path.",
            category=Category.CORRECTNESS,
            severity=Severity.MINOR,
            line=node.lineno,
        ))
    return findings
```

Rules:

- **Only report on changed lines.** Use `file_diff.added_line_numbers()` and the
  `_touches` helper. Reporting pre-existing code is the fastest way to lose users.
- **Skip test files** where the pattern is legitimate there (fake credentials,
  debug output). `file_diff.is_test_file`.
- **Explain the consequence, not the rule.** "Use a with-statement" is a lint
  message. "If an exception is raised before close(), the handle leaks" tells the
  reader why they should care.
- **Never raise.** `run_heuristics` catches exceptions, but a check that throws
  produces a noisy internal finding. Guard your inputs.
- **Add tests both ways**: one that fires, and one on reasonable code that must
  not fire. The second matters more.

## Add an agent

Subclass `Agent`, implement two methods, register it:

```python
# src/vibekill/agents/performance.py
class PerformanceAgent(Agent):
    name = "performance"
    default_category = Category.PERFORMANCE
    severity_cap = Severity.MAJOR        # only security may propose blocker
    description = "Finds changes that make hot paths materially slower."

    def allowed_categories(self) -> list[Category]:
        return [Category.PERFORMANCE]

    def relevant_evidence(self, evidence):
        return evidence_for_categories(evidence, [Category.PERFORMANCE])

    def system_prompt(self) -> str:
        return """
        You are reviewing a change for performance regressions...
        """.strip()
```

Then:

1. Append `PerformanceAgent` to `AGENT_CLASSES` in `agents/__init__.py`.
2. Add `performance: AgentSettings = AgentSettings()` to `AgentsSettings` in
   `settings.py`.

`graph.py` builds nodes from the registry, so nothing else changes.

### Writing an agent prompt

The prompts are the most consequential code in the project and the hardest to get
right. What has worked:

- **Rank the concerns.** An unordered list of twelve things produces twelve
  mediocre findings. Number them by importance.
- **Say what is not a finding, explicitly.** Most of a good prompt is calibration.
  "Do not report style unless it actively misleads" removes more noise than any
  positive instruction adds.
- **Demand a mechanism.** "Name the input that triggers this bug." A model that
  cannot name one usually invented the finding.
- **State that empty is a valid answer.** Without this, models pad. Say it in as
  many words as it takes.
- **Require a concrete alternative** for anything subjective. "This is too
  complex" is an opinion; "a dict lookup would replace these fifteen branches" is
  a finding.

Test prompt changes against `make demo` and the mock-model test before and after.
There is no evaluation harness yet — building one is an open task and would make
this much less guesswork.

## Add a source

Anything with a unified diff works: GitLab, Gerrit, Bitbucket, a patch file.

```python
class PatchFileSource:
    def __init__(self, settings: Settings, patch_path: Path) -> None: ...

    def fetch(self) -> ReviewContext:
        files = parse_unified_diff(Path(self.patch_path).read_text())
        return ReviewContext(meta=RepoMeta(root="."), files=files, mode="airgapped")
```

Wire it into `build_source`. If it needs network, add only its required hosts to
the connected-mode allowlist; never disable the guard for convenience.

## Add a tool runner

```python
def run_eslint(context: ReviewContext, settings: Settings) -> ToolOutcome:
    if not shutil.which("eslint"):
        return ToolOutcome("eslint", available=False, skipped_reason="eslint is not installed")
    ...
```

Register in `RUNNERS`. Requirements:

- A missing binary returns `available=False`, never raises.
- Use `_run()`, which applies the scrubbed environment.
- Pass whatever offline flags the tool has. If it phones home and cannot be told
  not to, it does not belong in air-gapped mode.

## Testing

```bash
make test
./.venv/bin/python -m pytest tests/test_heuristics.py -v
./.venv/bin/python -m pytest tests/ -k evidence
```

`tests/conftest.py` has `make_context(path, lines)`, which builds a
`ReviewContext` from inline source without needing a git repo. Most tests want it.

Tests must not touch the network or require a model. `tests/test_airgap.py`
enforces the first.

When you fix a bug, add the test that would have caught it. Several tests here
exist because they caught real bugs during the initial build — over-merging in
dedup, and a SQL pattern that missed f-strings.

## Style

- Ruff enforces the mechanical parts: `make lint`.
- Type hints on public functions.
- Docstrings explain **why**, not what. The code says what.
- Prefer a plain function over a class, and a class over a framework.
- New runtime dependencies need a reason. Every one has to be vendored into an
  air-gapped install by somebody.

## Pull requests

- One concern per PR.
- Say what you verified, not just what you changed.
- `make test lint` green.
- New behaviour comes with tests.
- If you changed a prompt, show before/after output on a real diff.

## Good first issues

- A heuristic for a language that only has regex coverage today (Go, Rust, Java).
- JS/TS AST checks via `tree-sitter`, matching the Python ones.
- The finding cache described in ARCHITECTURE.md's "Not built yet".
- A baseline file so existing repos can adopt this incrementally.
- The evaluation harness. Currently prompt tuning is guesswork, and this would fix
  that for everyone.
