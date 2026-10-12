"""Unified-diff parser.

Both source adapters end up here: local git produces a diff via `git diff`, and
the GitHub adapter downloads the PR's unified diff. Parsing one format keeps the
two modes honest, because a divergence in the diff layer is exactly the kind of
bug that would make the air-gapped review disagree with the hosted one.
"""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Iterable

from .models import DiffLine, FileDiff, Hunk

_HUNK_RE = re.compile(
    r"^@@ -(?P<old_start>\d+)(?:,(?P<old_count>\d+))? "
    r"\+(?P<new_start>\d+)(?:,(?P<new_count>\d+))? @@(?P<heading>.*)$"
)

# Extension -> language label. Used to route deterministic analysers and to tell
# the model what it is looking at.
LANGUAGE_BY_EXT = {
    "py": "python", "pyi": "python",
    "js": "javascript", "jsx": "javascript", "mjs": "javascript", "cjs": "javascript",
    "ts": "typescript", "tsx": "typescript", "mts": "typescript",
    "go": "go", "rs": "rust", "java": "java", "kt": "kotlin", "kts": "kotlin",
    "rb": "ruby", "php": "php", "cs": "csharp", "swift": "swift", "scala": "scala",
    "c": "c", "h": "c", "cc": "cpp", "cpp": "cpp", "cxx": "cpp", "hpp": "cpp", "hh": "cpp",
    "sh": "shell", "bash": "shell", "zsh": "shell", "ps1": "powershell",
    "sql": "sql", "graphql": "graphql", "gql": "graphql", "proto": "protobuf",
    "yml": "yaml", "yaml": "yaml", "json": "json", "toml": "toml", "ini": "ini",
    "tf": "terraform", "hcl": "hcl", "dockerfile": "dockerfile",
    "md": "markdown", "rst": "rst", "txt": "text",
    "html": "html", "htm": "html", "css": "css", "scss": "scss", "vue": "vue", "svelte": "svelte",
    "ex": "elixir", "exs": "elixir", "erl": "erlang", "clj": "clojure", "lua": "lua",
    "dart": "dart", "r": "r", "pl": "perl", "groovy": "groovy", "gradle": "groovy",
}

_BINARY_EXT = {
    "png", "jpg", "jpeg", "gif", "bmp", "ico", "webp", "pdf", "zip", "gz", "tar",
    "bz2", "xz", "7z", "jar", "war", "class", "so", "dylib", "dll", "exe", "bin",
    "wasm", "woff", "woff2", "ttf", "eot", "mp3", "mp4", "mov", "avi", "parquet",
    "db", "sqlite", "pyc", "pkl", "npy", "onnx", "safetensors",
}


def detect_language(path: str) -> str:
    name = path.rsplit("/", 1)[-1].lower()
    if name in ("dockerfile", "containerfile") or name.startswith("dockerfile."):
        return "dockerfile"
    if name in ("makefile", "gnumakefile"):
        return "make"
    if name in ("jenkinsfile",):
        return "groovy"
    if "." not in name:
        return "text"
    ext = name.rsplit(".", 1)[-1]
    return LANGUAGE_BY_EXT.get(ext, "unknown")


def looks_binary(path: str) -> bool:
    name = path.rsplit("/", 1)[-1].lower()
    return "." in name and name.rsplit(".", 1)[-1] in _BINARY_EXT


def parse_unified_diff(text: str) -> list[FileDiff]:
    """Parse `git diff` / GitHub `.diff` output into `FileDiff` objects."""
    files: list[FileDiff] = []
    current: FileDiff | None = None
    hunk: Hunk | None = None
    old_lineno = new_lineno = 0
    pending_old_path: str | None = None

    def close_file() -> None:
        nonlocal current, hunk
        if current is not None:
            files.append(current)
        current = None
        hunk = None

    lines = text.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        index += 1

        if line.startswith("diff --git "):
            close_file()
            pending_old_path = None
            a_path, b_path = _split_diff_git_header(line)
            path = b_path or a_path or ""
            current = FileDiff(
                path=path,
                old_path=a_path if a_path != b_path else None,
                language=detect_language(path),
                is_binary=looks_binary(path),
            )
            continue

        if current is None:
            # Diffs produced without the `diff --git` preamble (rare, e.g. some
            # `format-patch` variants) still start with ---/+++ pairs.
            if line.startswith("--- "):
                pending_old_path = _strip_prefix(line[4:].strip())
            elif line.startswith("+++ ") and pending_old_path is not None:
                new_path = _strip_prefix(line[4:].strip())
                path = new_path or pending_old_path
                current = FileDiff(
                    path=path,
                    old_path=pending_old_path if pending_old_path != path else None,
                    language=detect_language(path),
                    is_binary=looks_binary(path),
                )
                pending_old_path = None
            continue

        if line.startswith("new file mode"):
            current.change_type = "added"
            continue
        if line.startswith("deleted file mode"):
            current.change_type = "deleted"
            continue
        if line.startswith("rename from "):
            current.change_type = "renamed"
            current.old_path = line[len("rename from ") :].strip()
            continue
        if line.startswith("rename to "):
            current.change_type = "renamed"
            current.path = line[len("rename to ") :].strip()
            current.language = detect_language(current.path)
            continue
        if line.startswith("Binary files ") or line.startswith("GIT binary patch"):
            current.is_binary = True
            continue
        if line.startswith(("index ", "old mode", "new mode", "similarity index", "dissimilarity index")):
            continue
        if line.startswith("--- "):
            old = _strip_prefix(line[4:].strip())
            if old and old != "/dev/null":
                current.old_path = current.old_path or (old if old != current.path else None)
            continue
        if line.startswith("+++ "):
            new = _strip_prefix(line[4:].strip())
            if new and new != "/dev/null":
                current.path = new
                current.language = detect_language(new)
                current.is_binary = current.is_binary or looks_binary(new)
            continue

        match = _HUNK_RE.match(line)
        if match:
            hunk = Hunk(
                old_start=int(match.group("old_start")),
                old_count=int(match.group("old_count") or 1),
                new_start=int(match.group("new_start")),
                new_count=int(match.group("new_count") or 1),
                heading=match.group("heading").strip(),
            )
            current.hunks.append(hunk)
            old_lineno = hunk.old_start
            new_lineno = hunk.new_start
            continue

        if hunk is None:
            continue

        if line.startswith("\\"):  # "\ No newline at end of file"
            continue

        marker, content = (line[0], line[1:]) if line else (" ", "")
        if marker == "+":
            hunk.lines.append(DiffLine("add", content, None, new_lineno))
            current.additions += 1
            new_lineno += 1
        elif marker == "-":
            hunk.lines.append(DiffLine("del", content, old_lineno, None))
            current.deletions += 1
            old_lineno += 1
        elif marker == " ":
            hunk.lines.append(DiffLine("ctx", content, old_lineno, new_lineno))
            old_lineno += 1
            new_lineno += 1
        else:
            # Anything else means the hunk body has ended.
            hunk = None
            index -= 1
            continue

    close_file()
    return [f for f in files if f.path]


def _split_diff_git_header(line: str) -> tuple[str, str]:
    """Extract the a/ and b/ paths from a `diff --git` line.

    Paths containing spaces make this ambiguous. Git quotes such paths, which we
    handle; for the unquoted-with-spaces case we fall back to splitting on the
    ` b/` separator, which is correct for every path that does not itself
    contain the literal sequence " b/".
    """
    body = line[len("diff --git ") :]
    if body.startswith('"'):
        parts = re.findall(r'"((?:[^"\\]|\\.)*)"|(\S+)', body)
        flat = [p[0] or p[1] for p in parts]
        if len(flat) >= 2:
            return _strip_prefix(flat[0]), _strip_prefix(flat[1])
    separator = body.rfind(" b/")
    if separator != -1:
        return _strip_prefix(body[:separator].strip()), _strip_prefix(body[separator + 1 :].strip())
    tokens = body.split()
    if len(tokens) >= 2:
        return _strip_prefix(tokens[0]), _strip_prefix(tokens[-1])
    return "", ""


def _strip_prefix(path: str) -> str:
    path = path.strip().strip('"')
    if path == "/dev/null":
        return path
    for prefix in ("a/", "b/", "i/", "w/", "c/", "o/"):
        if path.startswith(prefix):
            return path[len(prefix) :]
    return path


# --------------------------------------------------------------------------- #
# Filtering
# --------------------------------------------------------------------------- #


def path_matches_any(path: str, patterns: Iterable[str]) -> bool:
    """Glob match that treats `**/x` as also matching a top-level `x`.

    Plain `fnmatch` does not: `fnmatch("dist/a.js", "**/dist/**")` is False,
    which would silently let generated output through the exclude list.
    """
    for pattern in patterns:
        if fnmatch.fnmatch(path, pattern):
            return True
        if pattern.startswith("**/") and fnmatch.fnmatch(path, pattern[3:]):
            return True
        if pattern.endswith("/**") and fnmatch.fnmatch(path, pattern[:-3]):
            return True
        # Directory patterns should match anything beneath them.
        stem = pattern.removeprefix("**/").removesuffix("/**")
        if stem and (path == stem or path.startswith(stem.rstrip("/") + "/") or f"/{stem.strip('/')}/" in f"/{path}"):
            return True
    return False


def filter_files(
    files: list[FileDiff],
    *,
    include: Iterable[str] = (),
    exclude: Iterable[str] = (),
) -> tuple[list[FileDiff], list[str]]:
    """Apply include/exclude globs. Returns (kept, excluded_paths)."""
    include = list(include)
    exclude = list(exclude)
    kept: list[FileDiff] = []
    dropped: list[str] = []
    for f in files:
        if include and not path_matches_any(f.path, include):
            dropped.append(f.path)
            continue
        if exclude and path_matches_any(f.path, exclude):
            dropped.append(f.path)
            continue
        kept.append(f)
    return kept, dropped


def truncate_to_budget(files: list[FileDiff], max_files: int, max_chars: int) -> tuple[list[FileDiff], list[str]]:
    """Cap the review surface, preferring small focused files over huge ones.

    A 40k-line generated migration should not crowd out the twelve lines of auth
    logic in the same PR, so files are taken smallest-first by diff size.
    """
    ordered = sorted(files, key=lambda f: f.additions + f.deletions)
    kept: list[FileDiff] = []
    skipped: list[str] = []
    total = 0
    for f in ordered:
        rendered = len(f.render())
        if len(kept) >= max_files or (total + rendered > max_chars and kept):
            skipped.append(f.path)
            continue
        kept.append(f)
        total += rendered
    # Restore the original ordering so reports read like the diff.
    order = {f.path: i for i, f in enumerate(files)}
    kept.sort(key=lambda f: order.get(f.path, 0))
    return kept, skipped
