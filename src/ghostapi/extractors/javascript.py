"""Find npm package usages in JavaScript/TypeScript source with tree-sitter.

Targets use the npm path form from `ghostapi.paths`: `react:useState`, `axios:default.get`,
`react-dom/client:` for a module itself.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from ghostapi import tsparse
from ghostapi.extractors import FileScan
from ghostapi.models import Usage
from ghostapi.paths import child
from ghostapi.tsparse import Node

SOURCE_SUFFIXES = (".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts")
_NOT_SOURCE = (".d.ts", ".d.mts", ".d.cts", ".min.js")

NODE_BUILTINS = frozenset({
    "assert", "async_hooks", "buffer", "child_process", "cluster", "console", "constants",
    "crypto", "dgram", "diagnostics_channel", "dns", "domain", "events", "fs", "http", "http2",
    "https", "inspector", "module", "net", "os", "path", "perf_hooks", "process", "punycode",
    "querystring", "readline", "repl", "stream", "string_decoder", "sys", "test", "timers", "tls",
    "trace_events", "tty", "url", "util", "v8", "vm", "wasi", "worker_threads", "zlib",
})  # fmt: skip
_SCOPES = {
    "function_declaration", "generator_function_declaration", "class_declaration",
    "abstract_class_declaration", "method_definition",
}  # fmt: skip
_FUNCTION_VALUES = {"arrow_function", "function_expression", "function", "class"}


def is_source(path: Path | str) -> bool:
    name = str(path)
    return name.endswith(SOURCE_SUFFIXES) and not name.endswith(_NOT_SOURCE)


def package_of(specifier: str) -> str | None:
    """npm package a module specifier belongs to; None for relative paths, aliases, schemes."""
    if not specifier or specifier.startswith((".", "/", "#", "~", "@/")) or ":" in specifier:
        return None
    parts = specifier.split("/")
    if specifier.startswith("@"):
        return "/".join(parts[:2]) if len(parts) > 1 and parts[1] else None
    return None if parts[0] in NODE_BUILTINS else parts[0]


class _Visitor:
    def __init__(self, scan: FileScan, is_external: Callable[[str], bool]) -> None:
        self.scan = scan
        self.is_external = is_external
        self.aliases: dict[str, str] = {}  # local name -> target
        self.scope: list[str] = []

    def _add(self, target: str, node: Node, args: Node | None = None) -> None:
        n_positional = None
        has_spread = False
        if args is not None:
            values = [a for a in args.named_children if a.type != "comment"]
            n_positional = len(values)
            has_spread = any(a.type == "spread_element" for a in values)
        self.scan.usages.append(
            Usage(
                target=target,
                module=package_of(target.split(":", 1)[0]) or target.split(":", 1)[0],
                file=self.scan.file,
                line=tsparse.line(node),
                col=tsparse.col(node),
                symbol=".".join(self.scope) or "<module>",
                n_positional=n_positional,
                has_star_args=has_spread,
            )
        )

    def _module(self, specifier: str | None) -> str | None:
        """`react:` for an external specifier, else None."""
        if specifier is None:
            return None
        package = package_of(specifier)
        if package is None or not self.is_external(package):
            return None
        self.scan.imported_roots.add(package)
        return f"{specifier}:"

    def _bind(self, local: str, target: str, node: Node) -> None:
        self.aliases[local] = target
        self._add(target, node)

    # imports and re-exports

    def _import(self, node: Node) -> None:
        module = self._module(tsparse.string_value(node.child_by_field_name("source")))
        if module is None:
            return
        clause = tsparse.first_child(node, "import_clause")
        if clause is None:
            require = next(
                (c for c in node.named_children if c.type == "import_require_clause"), None
            )
            if require is not None:  # import x = require("m")
                name = tsparse.first_child(require, "identifier")
                if name is not None:
                    self._bind(tsparse.text(name), module, node)
            else:
                self._add(module, node)  # side-effect import
            return
        for part in clause.named_children:
            if part.type == "identifier":
                self._bind(tsparse.text(part), child(module, "default"), node)
            elif part.type == "namespace_import":
                name = tsparse.first_child(part, "identifier")
                if name is not None:
                    self._bind(tsparse.text(name), module, node)
            elif part.type == "named_imports":
                for spec in part.named_children:
                    if spec.type != "import_specifier":
                        continue
                    name = spec.child_by_field_name("name")
                    alias = spec.child_by_field_name("alias") or name
                    self._bind(tsparse.text(alias), child(module, tsparse.text(name)), node)

    def _reexport(self, node: Node) -> bool:
        module = self._module(tsparse.string_value(node.child_by_field_name("source")))
        if module is None:
            return False
        clause = tsparse.first_child(node, "export_clause")
        if clause is None:
            self._add(module, node)  # export * from "m"
            return True
        for spec in clause.named_children:
            if spec.type == "export_specifier":
                self._add(child(module, tsparse.text(spec.child_by_field_name("name"))), node)
        return True

    def _require(self, declarator: Node) -> bool:
        """`const x = require("m")` and `const { a, b: c } = require("m")`."""
        value = declarator.child_by_field_name("value")
        if value is None or value.type != "call_expression":
            return False
        fn = value.child_by_field_name("function")
        args = value.child_by_field_name("arguments")
        if tsparse.text(fn) != "require" or args is None or args.named_child_count != 1:
            return False
        module = self._module(tsparse.string_value(args.named_children[0]))
        if module is None:
            return False
        name = declarator.child_by_field_name("name")
        if name is None:
            return False
        if name.type == "identifier":
            self._bind(tsparse.text(name), module, declarator)
        elif name.type == "object_pattern":
            for prop in name.named_children:
                if prop.type == "shorthand_property_identifier_pattern":
                    self._bind(tsparse.text(prop), child(module, tsparse.text(prop)), declarator)
                elif prop.type == "pair_pattern":
                    key, local = prop.child_by_field_name("key"), prop.child_by_field_name("value")
                    if local is not None and local.type == "identifier":
                        member = child(module, tsparse.text(key))
                        self._bind(tsparse.text(local), member, declarator)
        else:
            self._add(module, declarator)
        return True

    # references

    def _resolve(self, node: Node | None) -> str | None:
        if node is None:
            return None
        if node.type in ("identifier", "type_identifier"):
            return self.aliases.get(tsparse.text(node))
        if node.type == "member_expression":
            base = self._resolve(node.child_by_field_name("object"))
            prop = node.child_by_field_name("property")
            if base is None or prop is None or prop.type != "property_identifier":
                return None
            return child(base, tsparse.text(prop))
        if node.type == "nested_type_identifier":
            base = self._resolve(node.child_by_field_name("module"))
            name = node.child_by_field_name("name")
            return None if base is None or name is None else child(base, tsparse.text(name))
        if node.type == "generic_type":
            return self._resolve(node.child_by_field_name("name"))
        return None

    def visit(self, node: Node, depth: int = 0) -> None:
        if depth > 400:  # pathological nesting; stop rather than overflow the stack
            return
        kind = node.type
        if kind == "import_statement":
            self._import(node)
            return
        if kind == "export_statement" and self._reexport(node):
            return
        if kind == "variable_declarator" and self._require(node):
            return

        if kind in ("call_expression", "new_expression"):
            callee = node.child_by_field_name(
                "function" if kind == "call_expression" else "constructor"
            )
            target = self._resolve(callee)
            if target is not None and callee is not None:
                self._add(target, callee, node.child_by_field_name("arguments"))
                for arg in (node.child_by_field_name("arguments") or node).named_children:
                    self.visit(arg, depth + 1)
                return
        elif kind in ("member_expression", "nested_type_identifier", "type_identifier"):
            target = self._resolve(node)
            if target is not None:
                self._add(target, node)
                return
        elif kind in ("jsx_opening_element", "jsx_self_closing_element"):
            name = node.child_by_field_name("name")
            target = self._resolve(name)
            if target is not None and name is not None:
                self._add(target, name)

        pushed = self._enter_scope(node)
        for c in node.named_children:
            self.visit(c, depth + 1)
        if pushed:
            self.scope.pop()

    def _enter_scope(self, node: Node) -> bool:
        name = None
        if node.type in _SCOPES:
            name = node.child_by_field_name("name")
        elif node.type == "variable_declarator":
            value = node.child_by_field_name("value")
            if value is not None and value.type in _FUNCTION_VALUES:
                name = node.child_by_field_name("name")
        if name is None or name.type not in (
            "identifier", "type_identifier", "property_identifier",
        ):  # fmt: skip
            return False
        self.scope.append(tsparse.text(name))
        self.scan.defined.add(".".join(self.scope))
        return True


def scan_source(source: str, file: str, is_external: Callable[[str], bool]) -> FileScan:
    scan = FileScan(file=file, kind="npm")
    tree = tsparse.parse(source.encode("utf-8"), file)
    # tree-sitter recovers from syntax errors, so a half-edited file still yields its imports.
    visitor = _Visitor(scan, is_external)
    for node in tree.root_node.named_children:  # imports first: they can follow their uses
        if node.type == "import_statement":
            visitor.visit(node)
    for node in tree.root_node.named_children:
        if node.type != "import_statement":
            visitor.visit(node)
    return scan


def scan_file(path: Path, file: str, is_external: Callable[[str], bool]) -> FileScan:
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return FileScan(file=file, kind="npm", error=str(exc))
    return scan_source(source, file, is_external)
