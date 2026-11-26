# Design: PR comprehension and scope checking

Status: approved. Branch: `feature/pr-comprehension`.

## Goal

Compare the DECLARED INTENT of a PR with the ACTUAL CHANGE, and show an approver (who
may not read the language) what changed, why it might be there, and which changes have
no visible link to the intent. Output is questions for the author, not verdicts.
Everything works with `--provider none`. Air-gapped local mode stays the default.

## Pipeline

```
source -> ReviewContext(+intent) -> scope analyser -> ReviewResult.comprehension -> renderers
```

A new package `src/vibekill/scope/` (`intent`, `grouping`, `signals`, `explain`,
`questions`). It is a pure function of `ReviewContext`, so it needs no git and no model.
An optional model step runs afterwards and can only reword; its text must quote a diff
line that the existing evidence gate verifies. The typed `Comprehension` object is
the single source for JSON, HTML and the PR comment, so they cannot disagree.

## Intent

Sources, all optional: PR title and body (`--pr-title`, `--pr-body-file`, or
`VIBEKILL_PR_TITLE` / `VIBEKILL_PR_BODY`; GitHub mode reads them from the API), any
number of plain-markdown files (`--intent-file`, for a ticket or a spec such as
`.kiro/specs/**/tasks.md`), and commit subjects (weak).

Every term remembers its source. Link strength, strongest first: path or symbol named
in an intent file; path or symbol named in the title/body; shared rare words; commit
subjects. Status is `found`, `weak` (only commit subjects, or only generic words such as
"fix" or "update"), or `none`. With `none`, the report shows INCIDENTAL and type-based
SUPPORTING groups but never CORE or UNTRACED: a file cannot be untraced from nothing.

The PR body is untrusted text. It is only tokenised. HTML comments (PR template
placeholders) and fenced code are dropped before extraction.

## Grouping (first match wins)

1. INCIDENTAL: lock or generated path, formatting-only, pure rename, verbatim move.
2. CORE: non-test file with a strong link or a score above `core_min_score`.
3. SUPPORTING: tests that point at a CORE file; config, packaging, CI, docs,
   `__init__.py`; files importing or imported by a CORE file.
4. UNTRACED: intent exists and nothing above matched.

Each file stores its rule as a reason code. Non-Python files get path/extension rules
only and the report says so.

## Deterministic signals (all INFO, never affect verdict or exit code)

| Signal | Computation |
|---|---|
| formatting_only | Deleted and added lines equal after stripping whitespace, quotes, trailing commas. |
| pure_rename | Renamed, no hunks or 100% similarity. |
| moved_code | 4 or more normalised lines deleted in one file and added verbatim in another. |
| isolated_file (Python) | Changed non-test file with no import or changed-symbol link to another changed file. Worded "no visible link", not "off the call path". |
| single_impl_abstraction | Added ABC/Protocol or `*Factory`/`*Builder`/`*Registry` with at most one subclass or product among changed files. |
| duplicate_helper | Existing `_cross_file_duplicates`, plus a new function whose normalised body matches an existing one or whose name matches a path in `repo_file_index`. |
| size_vs_intent | Files and lines against `max_files_advisory` / `max_lines_advisory`, reported as numbers. |

## Explanations and the click-to-explain view

Templates keyed on reason code and change shape, for example "Adds a new function
`create_user` (12 lines). It is named in the PR description." Three levels: file; block
(one AST unit for Python, one hunk otherwise); line, only where a deterministic fact
exists (whitespace-only, import added, comment-only, moved).

The info button on a block opens a panel: what changed; why it may be here (the link
found, or "no visible link"); the question for the author; an optional model note
labelled as such. A "Technical detail" toggle shows reason codes. In GitHub fetch-only
mode there is no file content, so blocks fall back to hunks.

HTML is self-contained with `<details>`, works with JavaScript off, and an inline script
only drives the side panel. No CDN. The PR comment (`review.comment.md`) is one Markdown
file starting with the model disclosure line ("Model: local (...), no data left this
machine" or "Model: external (host), diff sent"), a group table and the questions. It
ends with a marker `<!-- vibekill:comprehension -->`.

## Config (additive, defaults)

```toml
[scope]
enabled = true
core_min_score = 0.5
max_files_advisory = 10
max_lines_advisory = 400
require_rationale = false   # reports missing per-group rationale; never blocks
[external_model]            # trusted sources only
enabled = false
allowed_hosts = []
```

CLI additions (approved): `--pr-title`, `--pr-body-file` (`-` is stdin), repeatable
`--intent-file`, `--scope/--no-scope`, and `pr-comment` as a `--format` value. JSON gains
an optional `comprehension` key under `vibekill/review/v1`.

## External model trust (lands after the already-merged config-trust fix)

Off by default. Policy comes only from the base ref or admin environment variables;
the PR head copy is never read. Only allowlisted hosts are added to the guard for that
run, with no fallback from local to external. Every report states mode and host. Test:
a PR editing `vibekill.toml` to enable external mode is ignored.

## Not building

Hosted service, accounts, database, telemetry; GitHub comment posting; cross-language
call graphs; semantic similarity; any blocking result from these checks.

## Process

One commit per step on this branch, tests passing before the next step, merged by PR.
The dev environment is Python 3.14.4. The package floor stays 3.11. CI gains a
non-required 3.14 job; the classifier waits until the analysers and UI extras are
verified on 3.14.

## Risks and open questions

1. Word matching misses synonyms, which pushes files into UNTRACED wrongly. UNTRACED is
   worded as a question, and the eval harness measures it.
2. isolated_file will flag entry points loaded by config. Config and registry files are
   SUPPORTING by rule.
3. The Streamlit UI still reads the repo config directly (follow-up from the config-trust PR).
4. Python 3.14 behaviour of the `analysers` and `ui` extras is unverified.
