# Design: PR comprehension and scope checking

Status: proposed, awaiting approval. Branch: `feature/pr-comprehension`.

## Goal

Compare the DECLARED INTENT of a PR with the ACTUAL CHANGE, and show an approver
(who may not read the language) what changed, why it might be there, and which
changes have no visible link to the intent. Output is questions for the author,
not verdicts. Everything works with `--provider none`.

## Pipeline placement

```
source -> ReviewContext -> [scope analyser] -> evidence pack -> agents
       -> evidence gate -> ReviewResult.comprehension -> renderers
```

New package `src/vibekill/scope/`: `intent.py`, `grouping.py`, `signals.py`,
`explain.py` (templates), `questions.py`, `external.py` (trusted policy).
It is a pure function of `ReviewContext` plus intent text, so it is testable
without git or a model. The optional model step runs after it and can only reword.

## Intent sources and precedence

All sources are collected, and each term keeps a tag saying where it came from.
Strength of a link to a file, strongest first:

1. A file path or symbol named in the spec/task file (`--spec-file`, plain markdown;
   checkbox items and backticked paths are read, nothing Kiro-specific).
2. A path or symbol named in the PR title/description or ticket text.
3. Shared rare words between intent and a file's path or its added identifiers.
4. Commit subjects (weak; labelled as such).

If no intent text exists, state is `no_intent`. In that state the report still
shows INCIDENTAL and type-based SUPPORTING groups, but never assigns CORE or
UNTRACED. A file cannot be "untraced" with nothing to trace to.

Local mode has no PR body, so it is passed in (new flags below). GitHub mode reads it
from the API.

## Grouping rules (first match wins, in this order)

1. INCIDENTAL: lockfile or generated path (reuses `DEFAULT_EXCLUDES`), formatting-only
   edit, pure rename, or code moved verbatim between files.
2. CORE: non-test file with a strong link (levels 1-2) or a score above
   `core_min_score`.
3. SUPPORTING: test file whose name or imports point to a CORE file; config, packaging,
   CI, docs, `__init__.py` re-exports; a file that imports or is imported by a CORE file.
4. UNTRACED: intent exists, none of the above matched.

Each file records the rule that placed it ("reason code"), so the report can say why.
Non-Python files get path/extension rules only (1, 2 by path, 3 by type, 4), and the
report says so.

## Deterministic signals

| Signal | Computation |
|---|---|
| formatting_only | Per file, deleted vs added lines are equal after stripping whitespace, quote style and trailing commas (multiset compare). For Python, also `ast.dump` equality when old and new text are both available. |
| pure_rename | `change_type == renamed` and no hunks, or similarity is 100%. |
| moved_code | A run of 4 or more normalised lines deleted in one file and added verbatim in another. |
| isolated_file (Python) | A changed non-test file with no import edge or shared changed-symbol reference to any other changed file. Named "no visible link to other changed files". It does not claim to prove the call path; dynamic dispatch is invisible and the report says so. |
| single_impl_abstraction | Added class that is ABC/Protocol/has abstract methods, or named `*Factory`/`*Builder`/`*Registry`, with at most one subclass or product among the changed files. Wording says "among changed files" because the full repo is not parsed. |
| duplicate_helper | Reuses `_cross_file_duplicates`, plus a new top-level function whose normalised body matches an existing one, or whose name collides with a basename in `repo_file_index`. |
| size_vs_intent | Files changed, lines changed, and share of files in UNTRACED, compared to `max_files_advisory` / `max_lines_advisory`. Stated as numbers, not as "too big". |

Every signal is INFO severity and never affects the verdict or exit code. The
"could be one function" style claim is never emitted.

## Plain-language explanations and the click-to-explain view

Explanations come from templates keyed on reason code and change shape, for
example: "Adds a new function `create_user` (12 lines). It is named in the PR
description." or "Only whitespace changed. Behaviour is identical."

Three levels, each labelled by provenance:

- File: group, one-line what, one-line why-here, any signals, any question.
- Block: for Python, one AST unit (function, class, import group, statement run); for
  other languages, one hunk. This is the unit the info button attaches to.
- Line: only lines with their own deterministic fact get a marker (whitespace-only,
  import added, comment-only, moved). No button on every line, which would be noise.

Info button on a block opens a panel with four fixed parts:
1. What changed (template).
2. Why it may be here (the link that was found, or "no visible link found").
3. Question for the author, if any.
4. Model note (optional, labelled "model suggestion", must quote a diff line that the
   existing evidence gate has verified; dropped otherwise).

A "Technical detail" toggle shows the raw reason code and rule for developers.

## Report layout

HTML ("What changed and why", placed above findings; self-contained, no CDN):

```
What changed and why                      Intent: PR title + description
Model: local (qwen2.5-coder:7b), no data left this machine
15 files, +412/-37   CORE 3 | SUPPORTING 4 | INCIDENTAL 5 | UNTRACED 3
[v] CORE - needed for the stated change (3)
    app/api/users.py   Adds the new endpoint named in the description.   [i]
      block: def create_user  (+12)        [i]  -> panel opens beside the diff
[>] SUPPORTING (4)   [>] INCIDENTAL (5)   [v] UNTRACED (3)
    utils/date.py   No visible link to the intent.
    Question: Why does this PR touch utils/date.py?
Missing rationale: the description has no per-file-group explanation (advisory).
```

Built with `<details>`/`<summary>` so it works with JavaScript off. A small inline
script (no network) only drives the side panel and expand-all. Diff data is embedded
and capped by `max_diff_chars`; over the cap, files fall back to file-level view and
the report says so.

PR comment (`review.comment.md`, one file, one comment):

```
### vibekill: what changed and why
Model: local (qwen2.5-coder:7b), no data left this machine
15 files, +412/-37. Grouped by stated intent ("Add GET /users endpoint").
| Group | Files |
| CORE | app/api/users.py |
| UNTRACED | utils/date.py, config/legacy.py |
Questions for the author
- Why does this PR touch `utils/date.py`? No visible link to the intent.
Note: advisory only. Grouping is path-level for non-Python files.
<!-- vibekill:comprehension -->
```

The marker comment is there so a later, opt-in step can update one comment. Posting to
GitHub is NOT in this design; see "Not building".

## Config (all additive, defaults shown)

```toml
[scope]
enabled = true
intent_file = ""            # also --spec-file
ticket_file = ""
core_min_score = 0.5
max_files_advisory = 10
max_lines_advisory = 400
require_rationale = false   # missing-rationale check is reported, never blocks
[external_model]            # read ONLY from trusted sources, see below
enabled = false
host = ""
allowed_hosts = []
```

Proposed new CLI flags (additive, none renamed): `--intent-title`, `--intent-body`,
`--intent-file`, `--spec-file`, `--ticket-file`, `--scope/--no-scope`, and a new
`pr-comment` value for `--format`. JSON gains an optional `comprehension` key under
the existing `vibekill/review/v1` schema; no existing key changes.

## External model trust model

- Default is off. Local mode is unchanged.
- Policy is read from, in order: (1) env `VIBEKILL_ADMIN_POLICY` pointing to a file
  outside the checkout, (2) the base-branch copy via `git show <base_sha>:vibekill.toml`.
  The working-tree (PR head) copy is never consulted for `[external_model]`. In
  GitHub mode the base copy is fetched from the API at `base.sha`.
- Only hosts in the trusted `allowed_hosts` are added to the guard for that run. No
  fallback: if the external model is unreachable, the run proceeds with no model and
  says so. It never retries against a different host.
- Every report states the mode and receiving host, e.g. "Model: external
  (api.example.com), diff sent".
- Test: a PR that edits `vibekill.toml` to enable external mode is ignored.

## Not building

- Hosted service, accounts, database, telemetry, dashboard.
- GitHub comment posting (opt-in, one updated comment, is a possible follow-up).
- Cross-language call graphs, semantic similarity, verdicts about design quality.
- Any blocking result from these checks.

## Process

Work happens on `feature/pr-comprehension`, one commit per step with passing tests,
merged to `main` by pull request. I will not push or merge without your go-ahead.
Dev environment: Python 3.14.4 in `.venv` (118 tests pass at baseline). The package
floor stays 3.11. CI and classifiers do not mention 3.14 yet; adding it is a one-line
follow-up if you want it.

## Risks and open questions

1. Existing weakness: local mode reads `vibekill.toml` from the PR head, so an author can
   already change `llm.base_url` and `allow_hosts`. Fixing it touches the egress path.
   Recommend a separate approved change; needs your decision.
2. Intent matching by shared words will miss synonyms. That biases toward UNTRACED false
   positives, which hurts precision. Mitigation: UNTRACED needs no link at all from
   levels 1-3, and the report words it as a question. The eval harness measures this.
3. isolated_file produces false positives for entry points and plugins loaded by
   config. Mitigation: config and registry files are SUPPORTING by rule 3.
4. Block-level view needs `new_content`, which exists in local mode but not in GitHub
   fetch-only mode. There it degrades to hunk-level blocks.
5. Open: should `--intent-body` accept `-` for stdin so CI can pass the PR body safely?
6. Open: confirm the flag names above before I add them.
