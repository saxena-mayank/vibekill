"""Turn a PR title, description and optional markdown files into matchable intent.

The text is untrusted: a PR author writes it. It is only tokenised, never
executed, and it is bounded in size so a huge body cannot stall a review.

Design choices worth knowing:

* Terms are kept per source so a later step can say *where* a link came from.
* Generic words ("fix", "update", "tests") are dropped. A title like "fix stuff"
  declares no intent, and pretending it does would make every file look untraced.
* HTML comments and fenced code are removed first. PR templates are full of
  placeholder comments that would otherwise become "intent".
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from pathlib import Path

from ..models import INTENT_KIND_RANK, Intent, IntentKind, IntentSource

MAX_SOURCE_CHARS = 50_000
MAX_FILE_BYTES = 200_000
MAX_TERMS = 500
MAX_TASKS = 50

_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_FENCE = re.compile(r"```.*?(?:```|\Z)", re.DOTALL)
_URL = re.compile(r"https?://\S+")
_BACKTICK = re.compile(r"`([^`\n]+)`")
_CHECKBOX = re.compile(r"^\s*[-*]\s+\[[ xX]\]\s+(.*\S)\s*$")

_EXTENSIONS = (
    "py|pyi|js|jsx|ts|tsx|go|rs|java|kt|rb|php|c|h|cc|cpp|hpp|cs|swift|sql|sh|"
    "toml|yaml|yml|json|md|rst|txt|html|css|ini|cfg|lock"
)
_PATH = re.compile(
    rf"(?<![\w/.\-])(?:[\w.\-]+/)+[\w.\-]*\w|(?<![\w/.\-])[\w\-]+\.(?:{_EXTENSIONS})\b"
)
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_.]*")
_WORD = re.compile(r"[A-Za-z][A-Za-z0-9]*")
_CAMEL = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+")

def _words(block: str) -> frozenset[str]:
    return frozenset(block.split())


# Words that carry no information about WHAT a change is for.
_GENERIC = _words(
    """
    add adds added adding fix fixes fixed fixing update updates updated updating change
    changes changed remove removes removed refactor cleanup clean improve improves
    improved implement implements implemented support make makes use uses used new
    minor various misc stuff things thing wip todo pr pull request issue bug feature
    code file files test tests testing step steps task tasks requirement requirements
    work works working some more also now just only get set edit edits edited modify
    modifies modified touch touches touched
    """
)
_STOPWORDS = _words(
    """
    a an and are as at be been but by can for from has have if in into is it its not of
    on or our so than that the their then there these this those to was we were when
    which while will with would you your should must may might do does done
    """
)


def build_intent(
    *,
    title: str = "",
    body: str = "",
    intent_texts: Sequence[tuple[str, str]] = (),
    commit_subjects: Iterable[str] = (),
) -> Intent:
    """Build intent from whatever was supplied. Empty input gives status `none`.

    `intent_texts` is `(label, text)` pairs, usually from `read_intent_files`.
    """
    intent = Intent()
    # Order does not decide strength; `_record` keeps the strongest kind per term.
    for label, text in intent_texts:
        _add_source(intent, "file", label, text)
        intent.tasks.extend(_tasks(text))
    if title.strip():
        _add_source(intent, "title", "PR title", title)
    if body.strip():
        _add_source(intent, "description", "PR description", body)
    commits = [c for c in commit_subjects if c and c.strip()]
    if commits:
        _add_source(intent, "commits", "commit subjects", "\n".join(commits))

    intent.tasks = intent.tasks[:MAX_TASKS]
    intent.status = _status(intent)
    return intent


def read_intent_files(paths: Iterable[Path | str]) -> tuple[list[tuple[str, str]], list[str]]:
    """Read markdown files. Problems become notes, never exceptions."""
    texts: list[tuple[str, str]] = []
    notes: list[str] = []
    for raw in paths:
        path = Path(raw)
        try:
            if not path.is_file():
                notes.append(f"intent file {path} was not found or is not a file")
                continue
            if path.stat().st_size > MAX_FILE_BYTES:
                notes.append(f"intent file {path} is too large (over {MAX_FILE_BYTES} bytes) and was skipped")
                continue
            texts.append((str(path), path.read_bytes().decode("utf-8", errors="replace")))
        except OSError as exc:
            notes.append(f"intent file {path} could not be read: {exc.strerror or exc}")
    return texts, notes


# --------------------------------------------------------------------------- #
# Internals
# --------------------------------------------------------------------------- #
def _status(intent: Intent) -> str:
    kinds = {k for mapping in (intent.terms, intent.paths, intent.symbols) for k in mapping.values()}
    if kinds - {"commits"}:
        return "found"
    return "weak" if "commits" in kinds else "none"


def _add_source(intent: Intent, kind: IntentKind, label: str, text: str) -> None:
    if len(text) > MAX_SOURCE_CHARS:
        text = text[:MAX_SOURCE_CHARS]
        intent.notes.append(f"{label} was truncated to {MAX_SOURCE_CHARS} characters")
    intent.sources.append(IntentSource(kind=kind, label=label, chars=len(text)))

    cleaned = _FENCE.sub(" ", _HTML_COMMENT.sub(" ", text))
    cleaned = _URL.sub(" ", cleaned)

    for span in _BACKTICK.findall(cleaned):
        span = span.strip().rstrip("()")
        if _PATH.fullmatch(span):
            _record(intent.paths, span, kind)
        elif _IDENT.fullmatch(span):
            _record(intent.symbols, span, kind)
            for word in _split_identifier(span):
                _record_word(intent, word, kind)
    prose = _BACKTICK.sub(" ", cleaned)
    for match in _PATH.finditer(prose):
        _record(intent.paths, match.group(0).rstrip(".,;:)"), kind)
    for token in _WORD.findall(prose):
        for word in _split_identifier(token):
            _record_word(intent, word, kind)


def _record_word(intent: Intent, word: str, kind: IntentKind) -> None:
    term = _normalise_word(word)
    if term is None:
        return
    if term not in intent.terms and len(intent.terms) >= MAX_TERMS:
        return
    _record(intent.terms, term, kind)


def _record(mapping: dict[str, IntentKind], key: str, kind: IntentKind) -> None:
    existing = mapping.get(key)
    if existing is None or INTENT_KIND_RANK[kind] > INTENT_KIND_RANK[existing]:
        mapping[key] = kind


def _split_identifier(token: str) -> list[str]:
    parts: list[str] = []
    for piece in re.split(r"[_.]+", token):
        parts.extend(_CAMEL.findall(piece))
    return parts


def _normalise_word(word: str) -> str | None:
    low = word.lower()
    if len(low) < 3 or low.isdigit():
        return None
    if len(low) > 4 and low.endswith("ies"):
        low = low[:-3] + "y"
    elif len(low) > 4 and low.endswith("s") and not low.endswith("ss"):
        low = low[:-1]
    if low in _STOPWORDS or low in _GENERIC:
        return None
    return low


def tokens(text: str) -> set[str]:
    """Meaningful lower-case terms in an identifier, path or sentence.

    Uses the same normalisation as intent terms so the two sides are comparable.
    """
    out: set[str] = set()
    for raw in _WORD.findall(text):
        for word in _split_identifier(raw):
            term = _normalise_word(word)
            if term:
                out.add(term)
    return out


def _tasks(text: str) -> list[str]:
    cleaned = _FENCE.sub(" ", _HTML_COMMENT.sub(" ", text))
    return [m.group(1) for line in cleaned.splitlines() if (m := _CHECKBOX.match(line))]
