"""Deterministic scope signals. Facts about the change, never verdicts.

Each signal names the evidence it rests on. Python gets the structural ones (imports,
classes, function bodies) because it is the language with an AST here; other languages
only get the layout and size signals. Every check errs toward silence: a signal that
fires on reasonable code teaches reviewers to ignore all of them.

Known limits, stated so nobody assumes more than is checked:
* Links are import and name based. Dynamic dispatch, plugins loaded from config and
  reflection are invisible, so "no visible link" is not "unreachable".
* Duplicate detection compares whole function bodies after removing the name and the
  docstring. A copy with renamed variables is not detected.
* Only code in this change is examined. An existing helper elsewhere in the repository
  that the new code duplicates is not found.
"""
from __future__ import annotations

import ast
from collections.abc import Iterable
from dataclasses import dataclass

from ..models import FileDiff, FileGroup, ReviewContext, Signal
from .grouping import file_kind, module_name, python_imports

MIN_DUPLICATE_STATEMENTS = 3
_ABSTRACT_BASES = {"ABC", "Protocol"}
_FACTORY_SUFFIXES = ("Factory", "Builder", "Registry")

_INCIDENTAL_MESSAGES = {
    "incidental.formatting_only": (
        "formatting_only",
        "Only layout changed (whitespace or trailing commas). Behaviour should be identical.",
    ),
    "incidental.pure_rename": ("pure_rename", "The file was renamed and its contents are unchanged."),
    "incidental.moved_code": ("moved_code", "The changed lines were moved between files unchanged."),
}


@dataclass
class _Module:
    """One changed Python file, parsed."""

    file: FileDiff
    tree: ast.Module
    added: set[int]

    @property
    def path(self) -> str:
        return self.file.path


def compute_signals(
    context: ReviewContext,
    groups: list[FileGroup],
    *,
    max_files: int = 10,
    max_lines: int = 400,
) -> list[Signal]:
    """All signals for one change, ordered by path then code."""
    modules = _parse(context.files)
    found = _from_groups(groups)
    found += _isolated_files(modules, {g.path: g for g in groups})
    found += _abstractions(modules)
    found += _duplicate_functions(modules)
    found += _size(context, groups, max_files, max_lines)
    return sorted(found, key=lambda s: (s.path or "", s.line or 0, s.code))


# --------------------------------------------------------------------------- #
# From grouping
# --------------------------------------------------------------------------- #
def _from_groups(groups: Iterable[FileGroup]) -> list[Signal]:
    out = []
    for g in groups:
        if g.reason in _INCIDENTAL_MESSAGES:
            code, message = _INCIDENTAL_MESSAGES[g.reason]
            out.append(Signal(code=code, message=message, path=g.path, evidence=list(g.evidence)))
    return out


# --------------------------------------------------------------------------- #
# isolated_file
# --------------------------------------------------------------------------- #
def _isolated_files(modules: list[_Module], groups: dict[str, FileGroup]) -> list[Signal]:
    """Untraced Python source files with no import or name link to any other changed file."""
    sources = [m for m in modules if file_kind(m.file) == "source"]
    out = []
    for m in sources:
        group = groups.get(m.path)
        if group is None or group.group not in ("UNTRACED", "UNCLASSIFIED"):
            continue
        others = [o for o in sources if o is not m]
        if others and not any(_linked(m, o) for o in others):
            out.append(
                Signal(
                    code="isolated_file",
                    path=m.path,
                    message="Nothing else in this change imports or uses this file, and it uses nothing from them.",
                    evidence=[f"compared with {len(others)} other changed Python source file(s)"],
                )
            )
    return out


def _linked(a: _Module, b: _Module) -> bool:
    return _one_way(a, b) or _one_way(b, a)


def _one_way(user: _Module, used: _Module) -> bool:
    """Does `user` import `used`, or reference something `used` defines in this change?"""
    target = module_name(used.path)
    if target and any(i == target or i.endswith("." + target) for i in python_imports(user.file.new_content)):
        return True
    defined = {n.name for n in used.tree.body if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
               if n.lineno in used.added}
    return bool(defined & _referenced_names(user.tree))


def _referenced_names(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.ImportFrom):
            names.update(a.name for a in node.names)
    return names


# --------------------------------------------------------------------------- #
# single_implementation
# --------------------------------------------------------------------------- #
def _abstractions(modules: list[_Module]) -> list[Signal]:
    """New abstract classes with at most one implementation, and new one-product factories."""
    classes = [(m, n) for m in modules for n in ast.walk(m.tree) if isinstance(n, ast.ClassDef)]
    out = []
    for m, node in classes:
        if node.lineno not in m.added or file_kind(m.file) == "test":
            continue
        if _is_abstract(node):
            count = sum(1 for _, other in classes if node.name in _base_names(other))
            if count <= 1:
                out.append(_abstraction_signal(m, node, f"is a new abstract class with {count} implementation(s) in this change"))
        elif node.name.endswith(_FACTORY_SUFFIXES):
            made = _constructed_names(node)
            if len(made) <= 1:
                out.append(_abstraction_signal(m, node, f"is a new {_suffix(node.name).lower()} that creates {len(made)} kind(s) of object in this change"))
    return out


def _abstraction_signal(m: _Module, node: ast.ClassDef, what: str) -> Signal:
    return Signal(
        code="single_implementation",
        path=m.path,
        line=node.lineno,
        message=f"`{node.name}` {what}.",
        evidence=["counted among the files in this change only"],
    )


def _is_abstract(node: ast.ClassDef) -> bool:
    if _base_names(node) & _ABSTRACT_BASES:
        return True
    if any(isinstance(k.value, ast.Name) and k.value.id == "ABCMeta" for k in node.keywords):
        return True
    return any(
        _last_name(d) == "abstractmethod"
        for item in node.body
        if isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef)
        for d in item.decorator_list
    )


def _base_names(node: ast.ClassDef) -> set[str]:
    return {name for b in node.bases if (name := _last_name(b))}


def _last_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _constructed_names(node: ast.ClassDef) -> set[str]:
    """Capitalised names called inside the class: a cheap stand-in for 'classes it builds'."""
    return {
        n.func.id
        for n in ast.walk(node)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id[:1].isupper()
    }


def _suffix(name: str) -> str:
    return next(s for s in _FACTORY_SUFFIXES if name.endswith(s))


# --------------------------------------------------------------------------- #
# duplicate_function
# --------------------------------------------------------------------------- #
def _duplicate_functions(modules: list[_Module]) -> list[Signal]:
    """New functions whose body matches another function in this change."""
    seen: dict[str, list[tuple[_Module, ast.FunctionDef | ast.AsyncFunctionDef]]] = {}
    for m in modules:
        if file_kind(m.file) == "test":
            continue
        for node in ast.walk(m.tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and not node.name.startswith("__"):
                key = _body_key(node)
                if key:
                    seen.setdefault(key, []).append((m, node))
    out = []
    for members in seen.values():
        for m, node in members:
            if node.lineno not in m.added:
                continue
            twin = next(((om, on) for om, on in members if on is not node), None)
            if twin:
                out.append(
                    Signal(
                        code="duplicate_function",
                        path=m.path,
                        line=node.lineno,
                        message=f"`{node.name}` has the same body as `{twin[1].name}` in {twin[0].path}.",
                        evidence=["compared with other functions in this change only"],
                    )
                )
    return out


def _body_key(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    body = list(node.body)
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
        body = body[1:]
    if len(body) < MIN_DUPLICATE_STATEMENTS:
        return ""
    return "\n".join(ast.dump(stmt) for stmt in body)


# --------------------------------------------------------------------------- #
# size_vs_intent
# --------------------------------------------------------------------------- #
def _size(context: ReviewContext, groups: list[FileGroup], max_files: int, max_lines: int) -> list[Signal]:
    files = len(context.files)
    lines = context.additions + context.deletions
    if files <= max_files and lines <= max_lines:
        return []
    message = (
        f"This change touches {files} files and {lines} lines "
        f"(advisory thresholds: {max_files} files, {max_lines} lines)."
    )
    untraced = sum(1 for g in groups if g.group == "UNTRACED")
    if untraced:
        message += f" {untraced} of {len(groups)} files have no visible link to the declared intent."
    return [Signal(code="size_vs_intent", message=message)]


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #
def _parse(files: Iterable[FileDiff]) -> list[_Module]:
    modules = []
    for f in files:
        if f.language != "python" or not f.new_content or f.change_type == "deleted":
            continue
        try:
            tree = ast.parse(f.new_content)
        except (SyntaxError, ValueError, RecursionError):
            continue
        modules.append(_Module(file=f, tree=tree, added=f.added_line_numbers()))
    return modules
