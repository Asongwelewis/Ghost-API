"""Find external API usages in Python source with the stdlib `ast`."""

from __future__ import annotations

import ast
from collections.abc import Set
from dataclasses import dataclass, field
from pathlib import Path

from ghostapi.models import Usage


@dataclass
class FileScan:
    file: str
    usages: list[Usage] = field(default_factory=list)
    defined: set[str] = field(default_factory=set)  # qualnames of functions/classes in the file
    imported_roots: set[str] = field(default_factory=set)  # external top-level modules imported
    error: str | None = None


class _Visitor(ast.NodeVisitor):
    def __init__(self, scan: FileScan, skip_roots: Set[str]) -> None:
        self.scan = scan
        self.skip_roots = skip_roots
        self.aliases: dict[str, str] = {}  # local name -> dotted path it is bound to
        self.scope: list[str] = []

    def _external(self, dotted: str) -> bool:
        return dotted.split(".")[0] not in self.skip_roots

    def _add(self, target: str, node: ast.expr | ast.stmt, call: ast.Call | None = None) -> None:
        kwargs: tuple[str, ...] = ()
        n_positional = None
        has_star = False
        if call is not None:
            kwargs = tuple(k.arg for k in call.keywords if k.arg)
            n_positional = len(call.args)
            has_star = any(isinstance(a, ast.Starred) for a in call.args) or any(
                k.arg is None for k in call.keywords
            )
        self.scan.usages.append(
            Usage(
                target=target,
                module=target.split(".")[0],
                file=self.scan.file,
                line=node.lineno,
                col=node.col_offset,
                symbol=".".join(self.scope) or "<module>",
                kwargs=kwargs,
                n_positional=n_positional,
                has_star_args=has_star,
            )
        )

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            if not self._external(alias.name):
                continue
            root = alias.name.split(".")[0]
            if alias.asname:
                self.aliases[alias.asname] = alias.name
            else:
                self.aliases[root] = root  # `import a.b` binds `a`
            self.scan.imported_roots.add(root)
            self._add(alias.name, node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.level or not node.module or not self._external(node.module):
            return
        self.scan.imported_roots.add(node.module.split(".")[0])
        for alias in node.names:
            if alias.name == "*":
                continue
            path = f"{node.module}.{alias.name}"
            self.aliases[alias.asname or alias.name] = path
            self._add(path, node)

    def _visit_scope(self, node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> None:
        self.scope.append(node.name)
        self.scan.defined.add(".".join(self.scope))
        self.generic_visit(node)
        self.scope.pop()

    visit_FunctionDef = visit_AsyncFunctionDef = visit_ClassDef = _visit_scope

    def _resolve(self, node: ast.expr) -> str | None:
        """Dotted path for `alias.attr.attr`, or None when the chain doesn't start at an import."""
        parts: list[str] = []
        while isinstance(node, ast.Attribute):
            parts.append(node.attr)
            node = node.value
        if isinstance(node, ast.Name) and node.id in self.aliases:
            return ".".join([self.aliases[node.id], *reversed(parts)])
        return None

    def visit_Call(self, node: ast.Call) -> None:
        target = self._resolve(node.func)
        if target is None:
            self.generic_visit(node)
            return
        self._add(target, node, call=node)
        for arg in node.args:
            self.visit(arg)
        for keyword in node.keywords:
            self.visit(keyword.value)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        target = self._resolve(node)
        if target is None:
            self.generic_visit(node)
        else:
            self._add(target, node)

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Load) and node.id in self.aliases:
            self._add(self.aliases[node.id], node)


def scan_source(source: str, file: str, skip_roots: Set[str]) -> FileScan:
    scan = FileScan(file=file)
    try:
        tree = ast.parse(source, filename=file)
    except SyntaxError as exc:
        scan.error = f"line {exc.lineno}: {exc.msg}"
        return scan
    _Visitor(scan, skip_roots).visit(tree)
    return scan


def scan_file(path: Path, file: str, skip_roots: Set[str]) -> FileScan:
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return FileScan(file=file, error=str(exc))
    return scan_source(source, file, skip_roots)
