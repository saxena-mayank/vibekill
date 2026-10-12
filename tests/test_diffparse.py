"""Diff parser tests.

Line numbers are the parser's whole job: the evidence gate, every tool runner and
every agent citation depends on them being right. These tests pin that down.
"""

from __future__ import annotations

from vibekill.diffparse import (
    detect_language,
    filter_files,
    parse_unified_diff,
    path_matches_any,
    truncate_to_budget,
)

MODIFY_DIFF = """\
diff --git a/src/app.py b/src/app.py
index 1111111..2222222 100644
--- a/src/app.py
+++ b/src/app.py
@@ -10,6 +10,8 @@ def handler(request):
     name = request.get("name")
     if not name:
         return None
-    return lookup(name)
+    query = "SELECT * FROM t WHERE n = '" + name + "'"
+    rows = run(query)
+    return rows
 
 def other():
"""


def test_parses_file_and_change_type():
    files = parse_unified_diff(MODIFY_DIFF)
    assert len(files) == 1
    assert files[0].path == "src/app.py"
    assert files[0].change_type == "modified"
    assert files[0].language == "python"


def test_resolves_new_side_line_numbers():
    """Added lines must carry their real line number in the new file."""
    file_diff = parse_unified_diff(MODIFY_DIFF)[0]
    added = [(line.new_lineno, line.content) for line in file_diff.hunks[0].lines if line.kind == "add"]
    assert added == [
        (13, '    query = "SELECT * FROM t WHERE n = \'" + name + "\'"'),
        (14, "    rows = run(query)"),
        (15, "    return rows"),
    ]


def test_counts_additions_and_deletions():
    file_diff = parse_unified_diff(MODIFY_DIFF)[0]
    assert file_diff.additions == 3
    assert file_diff.deletions == 1


def test_context_lines_keep_both_line_numbers():
    file_diff = parse_unified_diff(MODIFY_DIFF)[0]
    first = file_diff.hunks[0].lines[0]
    assert first.kind == "ctx"
    assert first.old_lineno == 10
    assert first.new_lineno == 10


def test_line_at_reads_from_hunks_without_full_content():
    file_diff = parse_unified_diff(MODIFY_DIFF)[0]
    assert file_diff.line_at(14) == "    rows = run(query)"
    assert file_diff.line_at(9999) is None


def test_detects_new_and_deleted_files():
    diff = """\
diff --git a/new.py b/new.py
new file mode 100644
--- /dev/null
+++ b/new.py
@@ -0,0 +1,2 @@
+import os
+print(os.getcwd())
diff --git a/gone.py b/gone.py
deleted file mode 100644
--- a/gone.py
+++ /dev/null
@@ -1,1 +0,0 @@
-obsolete = True
"""
    files = parse_unified_diff(diff)
    assert [(f.path, f.change_type) for f in files] == [("new.py", "added"), ("gone.py", "deleted")]


def test_detects_rename():
    diff = """\
diff --git a/old/name.py b/new/name.py
similarity index 95%
rename from old/name.py
rename to new/name.py
--- a/old/name.py
+++ b/new/name.py
@@ -1,2 +1,2 @@
-x = 1
+x = 2
"""
    file_diff = parse_unified_diff(diff)[0]
    assert file_diff.change_type == "renamed"
    assert file_diff.path == "new/name.py"
    assert file_diff.old_path == "old/name.py"


def test_marks_binary_files():
    diff = """\
diff --git a/logo.png b/logo.png
index 1111111..2222222 100644
Binary files a/logo.png and b/logo.png differ
"""
    assert parse_unified_diff(diff)[0].is_binary is True


def test_multiple_hunks_track_offsets_independently():
    diff = """\
diff --git a/m.py b/m.py
--- a/m.py
+++ b/m.py
@@ -1,2 +1,3 @@
 a = 1
+b = 2
 c = 3
@@ -50,2 +51,3 @@
 x = 1
+y = 2
 z = 3
"""
    file_diff = parse_unified_diff(diff)[0]
    assert file_diff.added_line_numbers() == {2, 52}


def test_is_test_file_detection():
    cases = {
        "tests/test_app.py": True,
        "src/app_test.go": True,
        "web/Button.spec.ts": True,
        "src/__tests__/x.js": True,
        "src/app.py": False,
        "src/contest.py": False,
    }
    for path, expected in cases.items():
        files = parse_unified_diff(
            f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n@@ -1 +1 @@\n+x\n"
        )
        assert files[0].is_test_file is expected, path


class TestPathMatching:
    """`**/dist/**` must also match a top-level `dist/`, which plain fnmatch misses."""

    def test_recursive_pattern_matches_top_level(self):
        assert path_matches_any("dist/bundle.js", ["**/dist/**"])

    def test_recursive_pattern_matches_nested(self):
        assert path_matches_any("web/dist/bundle.js", ["**/dist/**"])

    def test_suffix_pattern(self):
        assert path_matches_any("web/vendor.min.js", ["**/*.min.js"])

    def test_non_match(self):
        assert not path_matches_any("src/app.py", ["**/dist/**", "**/*.min.js"])

    def test_does_not_match_substring_of_directory_name(self):
        assert not path_matches_any("src/distribution.py", ["**/dist/**"])


def test_filter_files_applies_excludes():
    diff = "".join(
        f"diff --git a/{p} b/{p}\n--- a/{p}\n+++ b/{p}\n@@ -1 +1 @@\n+x\n"
        for p in ["src/app.py", "node_modules/lib/index.js", "poetry.lock"]
    )
    kept, dropped = filter_files(
        parse_unified_diff(diff), exclude=["**/node_modules/**", "**/*.lock"]
    )
    assert [f.path for f in kept] == ["src/app.py"]
    assert set(dropped) == {"node_modules/lib/index.js", "poetry.lock"}


def test_truncate_to_budget_prefers_small_files():
    """A huge generated file must not crowd out a small important one."""
    big = [f"x = {i}" for i in range(500)]
    diff = (
        "diff --git a/small.py b/small.py\n--- a/small.py\n+++ b/small.py\n@@ -0,0 +1,1 @@\n+a = 1\n"
        + "diff --git a/big.py b/big.py\n--- a/big.py\n+++ b/big.py\n"
        + f"@@ -0,0 +1,{len(big)} @@\n"
        + "\n".join(f"+{line}" for line in big)
        + "\n"
    )
    kept, skipped = truncate_to_budget(parse_unified_diff(diff), max_files=10, max_chars=200)
    assert [f.path for f in kept] == ["small.py"]
    assert skipped == ["big.py"]


def test_detect_language_special_filenames():
    assert detect_language("Dockerfile") == "dockerfile"
    assert detect_language("ops/Makefile") == "make"
    assert detect_language("app/main.tsx") == "typescript"
    assert detect_language("README") == "text"
