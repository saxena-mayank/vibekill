"""Deterministic detectors for changes that carry no behaviour: formatting, renames, moves.

These are conservative by design. A false "formatting only" tells a reviewer they can
skip code that changed behaviour, which is worse than missing a real formatting edit.
So each detector requires exact equality after a narrow normalisation, and anything
unusual (multi-line strings, reordered lines) counts as a real change.
"""
from __future__ import annotations

from collections.abc import Iterable

from ..models import FileDiff

MOVE_WINDOW = 4
# A window of trivial lines ("}", "pass", ")") is not evidence of a move.
MOVE_MIN_WINDOW_CHARS = 40
_MULTILINE_STRING_MARKERS = ('"""', "'''", "`")


def is_pure_rename(f: FileDiff) -> bool:
    """Renamed with no content change at all."""
    return f.change_type == "renamed" and not f.is_binary and f.additions == 0 and f.deletions == 0


def is_formatting_only(f: FileDiff) -> bool:
    """Every changed line differs from its counterpart only by layout.

    Layout means whitespace outside string literals and trailing commas. In Python,
    leading indentation is kept, because it decides which block a statement is in.
    Quote style, line wrapping and reordering are not recognised, so those edits are
    reported as real changes. That costs recall, not precision.
    """
    if f.is_binary or f.change_type not in ("modified", "renamed") or not f.hunks:
        return False
    if f.additions + f.deletions == 0:
        return False
    keep_indent = f.language == "python"
    for hunk in f.hunks:
        removed = [ln.content for ln in hunk.lines if ln.kind == "del"]
        added = [ln.content for ln in hunk.lines if ln.kind == "add"]
        if any(marker in text for text in removed + added for marker in _MULTILINE_STRING_MARKERS):
            return False
        if _layout_key(removed, keep_indent) != _layout_key(added, keep_indent):
            return False
    return True


def find_moved_code(files: Iterable[FileDiff]) -> dict[str, tuple[str, list[str]]]:
    """Files whose changed lines were moved verbatim to or from another changed file.

    Returns `path -> ("moved_in" | "moved_out", [other paths])`. A file qualifies only
    when at least `MOVE_WINDOW` lines changed and every one of them is covered by a
    matching run in another file. Import lines are ignored, because a move normally
    needs a changed import at each end.
    """
    files = [f for f in files if not f.is_binary]
    removed = {f.path: _runs(f, "del") for f in files}
    added = {f.path: _runs(f, "add") for f in files}
    removed_windows = _windows_by_path(removed)
    added_windows = _windows_by_path(added)
    result: dict[str, tuple[str, list[str]]] = {}
    for f in files:
        sources = _covering_paths(added[f.path], removed_windows, f.path)
        if sources is not None:
            result[f.path] = ("moved_in", sources)
            continue
        targets = _covering_paths(removed[f.path], added_windows, f.path)
        if targets is not None:
            result[f.path] = ("moved_out", targets)
    return result


# --------------------------------------------------------------------------- #
# Internals
# --------------------------------------------------------------------------- #
def _layout_key(lines: list[str], keep_indent: bool) -> list[str]:
    keys = [_squash(line, keep_indent) for line in lines]
    return [k for k in keys if k.strip()]


def _squash(text: str, keep_indent: bool) -> str:
    """Drop layout from one line while keeping string literals and Python indentation."""
    body = text.strip()
    indent = text[: len(text) - len(text.lstrip())] if keep_indent and body else ""
    chars: list[tuple[str, bool]] = []  # (character, inside a string literal)
    quote: str | None = None
    escaped = False
    for ch in body:
        if quote is not None:
            chars.append((ch, True))
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
            chars.append((ch, True))
        elif not ch.isspace():
            chars.append((ch, False))
    out: list[str] = []
    for i, (ch, in_string) in enumerate(chars):
        if ch == "," and not in_string:
            nxt = chars[i + 1] if i + 1 < len(chars) else None
            if nxt is None or (nxt[0] in ")]}" and not nxt[1]):
                continue
        out.append(ch)
    return indent + "".join(out)


def _move_key(text: str) -> str:
    return " ".join(text.split())


def _is_import(key: str) -> bool:
    return key.startswith(("import ", "from ")) and (" import " in key or key.startswith("import "))


def _runs(f: FileDiff, kind: str) -> list[list[str]]:
    """Per hunk, the changed lines of one kind, normalised, without blanks or imports."""
    runs: list[list[str]] = []
    for hunk in f.hunks:
        keys = [_move_key(ln.content) for ln in hunk.lines if ln.kind == kind]
        runs.append([k for k in keys if k and not _is_import(k)])
    return runs


def _windows(run: list[str]) -> Iterable[tuple[int, tuple[str, ...]]]:
    for i in range(len(run) - MOVE_WINDOW + 1):
        window = tuple(run[i : i + MOVE_WINDOW])
        if sum(len(x) for x in window) >= MOVE_MIN_WINDOW_CHARS:
            yield i, window


def _windows_by_path(runs_by_path: dict[str, list[list[str]]]) -> dict[tuple[str, ...], set[str]]:
    index: dict[tuple[str, ...], set[str]] = {}
    for path, runs in runs_by_path.items():
        for run in runs:
            for _, window in _windows(run):
                index.setdefault(window, set()).add(path)
    return index


def _covering_paths(
    runs: list[list[str]], other: dict[tuple[str, ...], set[str]], own_path: str
) -> list[str] | None:
    total = sum(len(r) for r in runs)
    if total < MOVE_WINDOW:
        return None
    partners: set[str] = set()
    for run in runs:
        covered = [False] * len(run)
        for start, window in _windows(run):
            paths = other.get(window, set()) - {own_path}
            if paths:
                partners |= paths
                for j in range(start, start + MOVE_WINDOW):
                    covered[j] = True
        if not all(covered):
            return None
    return sorted(partners) if partners else None
