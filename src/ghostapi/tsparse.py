"""tree-sitter parsing for JavaScript and TypeScript. Parsing never runs the code."""

from __future__ import annotations

from functools import cache

import tree_sitter as ts
import tree_sitter_typescript as tst

Node = ts.Node

# The TSX grammar also reads plain JavaScript and JSX. Plain `.ts` needs its own grammar because
# `<T>value` casts look like JSX.
_TS_ONLY = (".ts", ".mts", ".cts")


@cache
def _parser(tsx: bool) -> ts.Parser:
    language = tst.language_tsx() if tsx else tst.language_typescript()
    return ts.Parser(ts.Language(language))


def parse(source: bytes, filename: str) -> ts.Tree:
    return _parser(not filename.endswith(_TS_ONLY)).parse(source)


def text(node: Node | None) -> str:
    if node is None or node.text is None:
        return ""
    return node.text.decode("utf-8", "replace")


def first_child(node: Node | None, kind: str) -> Node | None:
    if node is None:
        return None
    return next((c for c in node.named_children if c.type == kind), None)


def string_value(node: Node | None) -> str | None:
    """Value of a plain string literal; None for template strings or anything computed."""
    if node is None or node.type != "string":
        return None
    return "".join(text(c) for c in node.named_children if c.type == "string_fragment")


def line(node: Node) -> int:
    return node.start_point[0] + 1


def col(node: Node) -> int:
    return node.start_point[1]
