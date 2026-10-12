"""Shared test fixtures.

Helpers here build a `ReviewContext` from an inline diff string so that tests do
not need a git repository. `make_context` is the one most tests want.
"""

from __future__ import annotations

import pytest

from vibekill.diffparse import parse_unified_diff
from vibekill.models import RepoMeta, ReviewContext
from vibekill.settings import Settings


def build_diff(path: str, added_lines: list[str], *, start: int = 1) -> str:
    """Produce a unified diff that adds `added_lines` to a new file."""
    count = len(added_lines)
    body = "\n".join(f"+{line}" for line in added_lines)
    return (
        f"diff --git a/{path} b/{path}\n"
        f"new file mode 100644\n"
        f"--- /dev/null\n"
        f"+++ b/{path}\n"
        f"@@ -0,0 +{start},{count} @@\n"
        f"{body}\n"
    )


def make_context(
    path: str,
    added_lines: list[str],
    *,
    attach_content: bool = True,
    repo_index: list[str] | None = None,
) -> ReviewContext:
    """A one-file `ReviewContext` whose file content matches the added lines.

    `attach_content` populates `new_content`, which the Python AST checks and the
    evidence gate both rely on.
    """
    files = parse_unified_diff(build_diff(path, added_lines))
    if attach_content:
        for file_diff in files:
            file_diff.new_content = "\n".join(added_lines) + "\n"
    return ReviewContext(
        meta=RepoMeta(root="/tmp/fake-repo", base_ref="main", head_ref="HEAD"),
        files=files,
        mode="airgapped",
        repo_file_index=repo_index or [],
    )


@pytest.fixture
def settings() -> Settings:
    """Settings with the model disabled, so tests never touch the network."""
    return Settings(llm={"provider": "none"}, tools={"enabled": False})
