"""Inline diff builders shared by the scope tests. No git repository needed."""
from __future__ import annotations

from vibekill.diffparse import parse_unified_diff
from vibekill.models import RepoMeta, ReviewContext


def d_add(path: str, lines: list[str]) -> str:
    body = "\n".join(f"+{x}" for x in lines)
    return (
        f"diff --git a/{path} b/{path}\nnew file mode 100644\n--- /dev/null\n+++ b/{path}\n"
        f"@@ -0,0 +1,{len(lines)} @@\n{body}\n"
    )


def d_modify(path: str, old: list[str], new: list[str], before: list[str] | None = None) -> str:
    """Replace `old` with `new`. `before` are unchanged lines above, shown as context."""
    before = before or []
    body = [*(f" {x}" for x in before), *(f"-{x}" for x in old), *(f"+{x}" for x in new)]
    return (
        f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n"
        f"@@ -1,{len(before) + len(old)} +1,{len(before) + len(new)} @@\n" + "\n".join(body) + "\n"
    )


def d_delete(path: str, lines: list[str]) -> str:
    body = "\n".join(f"-{x}" for x in lines)
    return (
        f"diff --git a/{path} b/{path}\ndeleted file mode 100644\n--- a/{path}\n+++ /dev/null\n"
        f"@@ -1,{len(lines)} +0,0 @@\n{body}\n"
    )


def d_rename(old: str, new: str) -> str:
    return f"diff --git a/{old} b/{new}\nsimilarity index 100%\nrename from {old}\nrename to {new}\n"


def ctx(
    *diffs: str,
    excluded=(),
    contents: dict[str, str] | None = None,
    with_content: bool = False,
) -> ReviewContext:
    """A context from diffs. `with_content` fills `new_content` for added files."""
    files = parse_unified_diff("".join(diffs))
    for f in files:
        if contents and f.path in contents:
            f.new_content = contents[f.path]
        elif with_content and f.change_type == "added":
            f.new_content = f.added_source() + "\n"
    context = ReviewContext(meta=RepoMeta(root="/tmp/x"), files=files)
    context.excluded_files = list(excluded)
    return context
