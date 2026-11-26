"""Group changed files by purpose: CORE, SUPPORTING, INCIDENTAL, UNTRACED.

Rules run in a fixed order and the first match wins. Every file records the rule that
placed it, so a reader can see why and a test can pin each rule.

1. INCIDENTAL  generated or lock path, pure rename, formatting-only edit, verbatim move
2. CORE        a non-test file the intent links to by path, symbol, or shared term
3. SUPPORTING  tests of core code, config/docs/wiring, files that import or are
               imported by core code (Python only)
4. UNTRACED    intent exists and nothing above matched

Without a usable intent (status `none` or `weak`) rules 2 and 4 cannot apply, because a
file cannot be "untraced" when there is nothing to trace it to. Such files are
UNCLASSIFIED.

Only Python gets import-level linking. Everything else is grouped by path, extension and
shared words, and the notes say so.
"""
from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field

from ..diffparse import detect_language
from ..models import GROUP_ORDER, FileDiff, FileGroup, Intent, IntentKind, ReviewContext
from .incidental import find_moved_code, is_formatting_only, is_pure_rename
from .intent import tokens

_KIND_LABEL = {
    "file": "the intent file",
    "title": "the PR title",
    "description": "the PR description",
    "commits": "the commit subjects",
}

# Directory and file words that say where code lives, not what it does.
_PATH_GENERIC = frozenset(
    {"src", "lib", "app", "main", "index", "util", "common", "core", "base", "init", "module",
     "helper", "conftest"}
)

_CONFIG_NAMES = frozenset(
    {"pyproject.toml", "setup.py", "setup.cfg", "pipfile", "makefile", "tox.ini", "dockerfile",
     "package.json", "tsconfig.json", "jenkinsfile", ".gitignore", ".dockerignore",
     ".editorconfig", ".gitlab-ci.yml"}
)
_CONFIG_EXTS = (".toml", ".ini", ".cfg", ".yaml", ".yml", ".json")
_DOC_EXTS = (".md", ".rst", ".txt", ".adoc")
_DEFINITION = re.compile(
    r"^\s*(?:export\s+)?(?:async\s+)?(?:def|class|function|func|fn|interface|struct|enum|trait)"
    r"\s+([A-Za-z_]\w*)",
    re.MULTILINE,
)


@dataclass
class _Facts:
    """What is known about one changed file, computed once."""

    file: FileDiff
    kind: str  # test | config | docs | wiring | source
    path_tokens: set[str]
    stem_tokens: set[str]
    defined_tokens: set[str]
    module: str | None = None
    imports: set[str] = field(default_factory=set)


def group_files(
    context: ReviewContext, intent: Intent, core_min_score: float = 0.5
) -> tuple[list[FileGroup], list[str]]:
    """Return one `FileGroup` per changed file, plus notes about the limits of the result."""
    usable = intent.status == "found"
    facts = {f.path: _facts(f) for f in context.files}
    moved = find_moved_code(context.files)
    groups: dict[str, FileGroup] = {}

    for path in context.excluded_files:
        groups[path] = FileGroup(
            path=path,
            group="INCIDENTAL",
            reason="incidental.generated_path",
            evidence=["generated, vendored or lock file; excluded from review by configuration"],
            language=detect_language(path),
        )

    # Pass 1: rules that depend on one file only.
    pending: list[_Facts] = []
    for fact in facts.values():
        placed = _incidental(fact, moved)
        if placed is None and usable:
            placed = _core(fact, intent, core_min_score)
        if placed is not None:
            groups[fact.file.path] = placed
        else:
            pending.append(fact)

    # Pass 2: rules that need to know which files are CORE.
    core = [facts[p] for p, g in groups.items() if g.group == "CORE" and p in facts]
    for fact in pending:
        groups[fact.file.path] = _supporting(fact, intent, core, usable) or _fallback(fact, usable)

    ordered = sorted(groups.values(), key=lambda g: (GROUP_ORDER.index(g.group), g.path))
    return ordered, _notes(context, intent, list(facts.values()))


# --------------------------------------------------------------------------- #
# Rule 1: INCIDENTAL
# --------------------------------------------------------------------------- #
def _incidental(fact: _Facts, moved: dict[str, tuple[str, list[str]]]) -> FileGroup | None:
    f = fact.file
    if is_pure_rename(f):
        return _make(fact, "INCIDENTAL", "incidental.pure_rename", [f"renamed from {f.old_path}; no content change"])
    if is_formatting_only(f):
        return _make(
            fact,
            "INCIDENTAL",
            "incidental.formatting_only",
            ["every changed line differs only by whitespace or trailing commas"],
        )
    if f.path in moved:
        direction, others = moved[f.path]
        verb = "came from" if direction == "moved_in" else "went to"
        return _make(
            fact,
            "INCIDENTAL",
            "incidental.moved_code",
            [f"the changed lines {verb} {', '.join(others[:3])} unchanged"],
        )
    return None


# --------------------------------------------------------------------------- #
# Rule 2: CORE
# --------------------------------------------------------------------------- #
def _core(fact: _Facts, intent: Intent, min_score: float) -> FileGroup | None:
    if fact.kind == "test":
        return None
    f = fact.file
    named = _named_path(f, intent)
    if named:
        path, kind = named
        return _make(fact, "CORE", "core.path_named", [f"`{path}` is named in {_KIND_LABEL[kind]}"])
    if fact.kind != "source":
        return None
    added = f.added_source()
    for symbol, kind in intent.symbols.items():
        short = symbol.rsplit(".", 1)[-1]
        if kind != "commits" and len(short) >= 3 and re.search(rf"\b{re.escape(short)}\b", added):
            return _make(
                fact, "CORE", "core.symbol_named",
                [f"`{short}` is named in {_KIND_LABEL[kind]} and appears in this change"],
            )
    wanted = {t: k for t, k in intent.terms.items() if k != "commits"}
    path_hits = sorted(fact.path_tokens & wanted.keys())
    name_hits = sorted(fact.defined_tokens & wanted.keys())
    score = 0.5 * bool(path_hits) + 0.5 * bool(name_hits)
    if score > 0 and score >= min_score:
        evidence = [f'"{t}" is in the file path and in {_KIND_LABEL[wanted[t]]}' for t in path_hits[:3]]
        evidence += [f'"{t}" is in a name defined here and in {_KIND_LABEL[wanted[t]]}' for t in name_hits[:3]]
        return _make(fact, "CORE", "core.term_match", evidence)
    return None


def _named_path(f: FileDiff, intent: Intent) -> tuple[str, IntentKind] | None:
    candidates = {f.path, *([f.old_path] if f.old_path else [])}
    for raw, kind in intent.paths.items():
        if kind == "commits":
            continue
        named = raw.lstrip("./").rstrip("/")
        if not named:
            continue
        is_directory = "." not in named.rsplit("/", 1)[-1]
        for path in candidates:
            if is_directory:
                if path.startswith(named + "/") or f"/{named}/" in path:
                    return raw, kind
            elif path == named or path.endswith("/" + named):
                return raw, kind
    return None


# --------------------------------------------------------------------------- #
# Rule 3: SUPPORTING
# --------------------------------------------------------------------------- #
def _supporting(
    fact: _Facts, intent: Intent, core: list[_Facts], usable: bool
) -> FileGroup | None:
    if fact.kind == "test":
        if not usable:
            return None
        for c in core:
            overlap = fact.stem_tokens & c.stem_tokens
            if overlap:
                return _make(fact, "SUPPORTING", "supporting.test_of_core",
                             [f"name matches core file {c.file.path}"])
            if c.module and _imports(fact, c.module):
                return _make(fact, "SUPPORTING", "supporting.test_of_core",
                             [f"imports core module {c.module}"])
        named = _named_path(fact.file, intent)
        if named:
            return _make(fact, "SUPPORTING", "supporting.test_linked_to_intent",
                         [f"`{named[0]}` is named in {_KIND_LABEL[named[1]]}"])
        wanted = {t for t, k in intent.terms.items() if k != "commits"}
        hits = sorted(fact.path_tokens & wanted)
        if hits:
            return _make(fact, "SUPPORTING", "supporting.test_linked_to_intent",
                         [f'"{hits[0]}" is in the test path and in the intent'])
        return None
    if fact.kind == "config":
        return _make(fact, "SUPPORTING", "supporting.config", ["configuration, packaging or CI file"])
    if fact.kind == "docs":
        return _make(fact, "SUPPORTING", "supporting.docs", ["documentation file"])
    if fact.kind == "wiring":
        return _make(fact, "SUPPORTING", "supporting.wiring", ["package __init__ file (re-exports and wiring)"])
    if usable and fact.file.language == "python":
        for c in core:
            if c.module and _imports(fact, c.module):
                return _make(fact, "SUPPORTING", "supporting.imports_core",
                             [f"imports core module {c.module}"])
            if fact.module and _imports(c, fact.module):
                return _make(fact, "SUPPORTING", "supporting.imported_by_core",
                             [f"imported by core file {c.file.path}"])
    return None


def _imports(fact: _Facts, module: str) -> bool:
    return any(i == module or i.endswith("." + module) for i in fact.imports)


# --------------------------------------------------------------------------- #
# Rule 4 and the no-intent state
# --------------------------------------------------------------------------- #
def _fallback(fact: _Facts, usable: bool) -> FileGroup:
    if usable:
        return _make(fact, "UNTRACED", "untraced.no_link", ["no visible link to the declared intent"])
    return _make(fact, "UNCLASSIFIED", "unclassified.no_intent", ["no declared intent to compare against"])


# --------------------------------------------------------------------------- #
# Facts and helpers
# --------------------------------------------------------------------------- #
def _make(fact: _Facts, group: str, reason: str, evidence: list[str]) -> FileGroup:
    f = fact.file
    return FileGroup(
        path=f.path,
        group=group,  # type: ignore[arg-type]
        reason=reason,
        evidence=evidence,
        old_path=f.old_path,
        language=f.language,
        additions=f.additions,
        deletions=f.deletions,
    )


def _facts(f: FileDiff) -> _Facts:
    kind = _kind(f)
    stem = f.path.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    fact = _Facts(
        file=f,
        kind=kind,
        path_tokens=tokens(f.path.rsplit(".", 1)[0]) - _tokens_of(_PATH_GENERIC),
        stem_tokens=tokens(stem) - _tokens_of(_PATH_GENERIC),
        defined_tokens=set(),
    )
    for name in _DEFINITION.findall(f.added_source()):
        fact.defined_tokens |= tokens(name)
    if f.language == "python":
        fact.module = _module_name(f.path)
        fact.imports = _python_imports(f.new_content)
    return fact


def _tokens_of(words: frozenset[str]) -> set[str]:
    return {t for w in words for t in tokens(w)} | set(words)


def _kind(f: FileDiff) -> str:
    name = f.path.rsplit("/", 1)[-1].lower()
    low = f.path.lower()
    if f.is_test_file or name == "conftest.py":
        return "test"
    if name == "__init__.py":
        return "wiring"
    if (
        name in _CONFIG_NAMES
        or name.startswith(("requirements", "dockerfile", "docker-compose"))
        or low.startswith((".github/", ".gitlab/", ".circleci/"))
        or name.endswith(_CONFIG_EXTS)
    ):
        return "config"
    if name.endswith(_DOC_EXTS) or low.startswith("docs/") or name in ("license", "notice"):
        return "docs"
    return "source"


def _module_name(path: str) -> str | None:
    if not path.endswith(".py"):
        return None
    parts = path[:-3].split("/")
    if parts and parts[0] == "src":
        parts = parts[1:]
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts) or None


def _python_imports(source: str | None) -> set[str]:
    if not source:
        return set()
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError, RecursionError):
        return set()
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            found.add(node.module)
            found.update(f"{node.module}.{alias.name}" for alias in node.names)
    return found


def _notes(context: ReviewContext, intent: Intent, facts: list[_Facts]) -> list[str]:
    notes: list[str] = []
    if intent.status == "none":
        notes.append(
            "No declared intent was found, so files are not marked as core or untraced. "
            "Pass --pr-title, --pr-body-file or --intent-file."
        )
    elif intent.status == "weak":
        notes.append(
            "Only commit subjects were available as intent, which is too weak to call any file "
            "untraced. Pass --pr-title, --pr-body-file or --intent-file."
        )
    others = sorted(
        {
            x.file.language
            for x in facts
            if x.kind in ("source", "test") and x.file.language not in ("python", "unknown")
        }
    )
    if others:
        notes.append(
            f"Grouping for {', '.join(others)} files uses path and extension only; "
            "import-based linking is Python-only."
        )
    blind = [x for x in facts if x.file.language == "python" and x.file.new_content is None
             and x.file.change_type != "deleted"]
    if blind:
        notes.append(
            f"Import-based linking was skipped for {len(blind)} Python file(s) because file "
            "contents were not available (for example in GitHub fetch-only mode)."
        )
    if context.skipped_files:
        notes.append(
            f"{len(context.skipped_files)} file(s) were skipped for size and are not grouped."
        )
    return notes
