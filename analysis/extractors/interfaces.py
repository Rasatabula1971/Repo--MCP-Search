"""
Interface extractor.

Python-only for MVP. Uses stdlib ast (no tree-sitter, no external deps).
Emits one 'interface' evidence item per public top-level function or
class in every .py file.

'Public' = name doesn't start with underscore. Reflects the Python
convention that _-prefixed names are private. Callers who want a
different convention filter downstream.

Nested functions and methods on classes are NOT emitted separately —
too noisy for Step 6, and the class evidence covers them at a coarser
locator. Step 7's interpretation layer can drill down if needed.
"""
from __future__ import annotations

import ast
from typing import Iterable, Iterator

from analysis.evidence import (
    LOCATOR_FILE_RANGE,
    EvidenceItem,
    SourceFile,
)


class InterfaceExtractor:
    name = "interfaces"

    def extract(self, files: Iterable[SourceFile]) -> Iterator[EvidenceItem]:
        for f in files:
            if not f.path.endswith(".py"):
                continue
            try:
                tree = ast.parse(f.content, filename=f.path)
            except SyntaxError:
                # A .py file that isn't valid Python is still evidence —
                # of a broken file. But it's not interface evidence; skip.
                continue
            yield from self._walk_module(tree, f)

    def _walk_module(
        self, tree: ast.Module, f: SourceFile
    ) -> Iterator[EvidenceItem]:
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name.startswith("_"):
                    continue
                yield self._function(node, f)
            elif isinstance(node, ast.ClassDef):
                if node.name.startswith("_"):
                    continue
                yield self._class(node, f)

    def _function(
        self, node: ast.FunctionDef | ast.AsyncFunctionDef, f: SourceFile
    ) -> EvidenceItem:
        return EvidenceItem(
            evidence_type="interface",
            locator_kind=LOCATOR_FILE_RANGE,
            locator={
                "path": f.path,
                "start_line": node.lineno,
                "end_line": node.end_lineno or node.lineno,
            },
            extracted_value={
                "kind": "async_function" if isinstance(
                    node, ast.AsyncFunctionDef
                ) else "function",
                "name": node.name,
                "signature": _format_signature(node.args),
                "returns": _format_annotation(node.returns),
                "language": "python",
            },
        )

    def _class(self, node: ast.ClassDef, f: SourceFile) -> EvidenceItem:
        method_names = sorted(
            child.name
            for child in node.body
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
            and not child.name.startswith("_")
        )
        return EvidenceItem(
            evidence_type="interface",
            locator_kind=LOCATOR_FILE_RANGE,
            locator={
                "path": f.path,
                "start_line": node.lineno,
                "end_line": node.end_lineno or node.lineno,
            },
            extracted_value={
                "kind": "class",
                "name": node.name,
                "bases": [_format_annotation(b) for b in node.bases],
                "public_methods": method_names,
                "language": "python",
            },
        )


# ---------------------------------------------------------------------------
# tiny formatters — no external code-formatting dep
# ---------------------------------------------------------------------------

def _format_signature(args: ast.arguments) -> str:
    """def foo(a, b=1, *args, c, **kw) -> 'a, b=1, *args, c, **kw'."""
    parts: list[str] = []
    pos_defaults_offset = len(args.args) - len(args.defaults)

    for i, arg in enumerate(args.args):
        s = arg.arg
        if arg.annotation is not None:
            s += f": {_format_annotation(arg.annotation)}"
        default_i = i - pos_defaults_offset
        if default_i >= 0:
            s += f"={_format_default(args.defaults[default_i])}"
        parts.append(s)

    if args.vararg:
        parts.append(f"*{args.vararg.arg}")

    kw_defaults_offset = len(args.kwonlyargs) - len(
        [d for d in args.kw_defaults if d is not None]
    )
    for i, arg in enumerate(args.kwonlyargs):
        s = arg.arg
        if arg.annotation is not None:
            s += f": {_format_annotation(arg.annotation)}"
        default = args.kw_defaults[i] if i < len(args.kw_defaults) else None
        if default is not None:
            s += f"={_format_default(default)}"
        parts.append(s)

    if args.kwarg:
        parts.append(f"**{args.kwarg.arg}")

    return ", ".join(parts)


def _format_annotation(node: ast.AST | None) -> str:
    if node is None:
        return ""
    try:
        return ast.unparse(node)
    except Exception:
        return "<unparsable>"


def _format_default(node: ast.AST) -> str:
    try:
        return ast.unparse(node)
    except Exception:
        return "<unparsable>"
