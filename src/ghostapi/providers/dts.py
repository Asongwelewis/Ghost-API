"""Build the API surface of one npm package version from its TypeScript declarations.

Only `package.json` and `.d.ts` files are read, with tree-sitter; nothing is executed. The
surface follows what the TypeScript compiler would see for imports from the package. Anything
this builder doesn't model (types from other packages, index signatures, wildcard re-exports
from outside the package, module augmentation) is marked `dynamic` so the engine stays silent.
"""

from __future__ import annotations

import json
import posixpath
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ghostapi import tsparse
from ghostapi.models import Param
from ghostapi.providers.surface import EXTERNAL, Node, Surface
from ghostapi.tsparse import Node as TsNode

DTS_SUFFIXES = (".d.ts", ".d.mts", ".d.cts")
MAX_SUBPATHS = 400  # deep-import entry points indexed per package
_JS_TO_DTS = {".js": ".d.ts", ".mjs": ".d.mts", ".cjs": ".d.cts", ".jsx": ".d.ts"}
_CONDITIONS = ("types", "typings", "import", "module", "node", "require", "default")


@dataclass
class _Decl:
    kind: str  # function | class | interface | namespace | enum | variable | type
    node: TsNode
    scope: _Scope | None = None  # body scope, for namespaces


@dataclass
class _Scope:
    """A declaration file, an ambient `declare module "x"`, or a namespace body."""

    id: str
    file: str
    parent: _Scope | None = None
    implicit_export: bool = False  # every declaration is exported (ambient bodies)
    decls: dict[str, list[_Decl]] = field(default_factory=dict)
    exports: dict[str, tuple[str, ...]] = field(default_factory=dict)
    imports: dict[str, tuple[str, str]] = field(default_factory=dict)  # local -> (spec, name)
    stars: list[str] = field(default_factory=list)  # export * from "spec"
    export_equals: str | None = None
    has_exports: bool = False

    def add(self, name: str, decl: _Decl) -> None:
        self.decls.setdefault(name, []).append(decl)


def specifier_name(package: str) -> str:
    """The module name a package provides: `@types/babel__core` -> `@babel/core`."""
    if package.startswith("@types/"):
        name = package.removeprefix("@types/")
        return "@" + name.replace("__", "/", 1) if "__" in name else name
    return package


class DtsBuilder:
    def __init__(self, root: Path, module_name: str) -> None:
        self.root = root
        self.module = module_name
        self.files = {
            p.relative_to(root).as_posix(): p
            for p in root.rglob("*")
            if p.name.endswith(DTS_SUFFIXES) and "node_modules" not in p.parts
        }
        self.nodes: dict[str, Node] = {}
        self._scopes: dict[str, _Scope] = {}
        self._ambient: dict[str, _Scope] = {}  # declare module "x" { } blocks found so far
        self._ambient_scanned = False
        self._resolving: set[str] = set()  # guards `type A = B; type B = A`
        self._subpaths: dict[str, str] = {}  # specifier -> declaration file

    # entry points

    def build(self) -> Surface:
        package = _read_json(self.root / "package.json")
        self._subpaths = self._entry_points(package)
        roots: dict[str, str] = {}
        for specifier, file in self._subpaths.items():
            roots[f"{specifier}:"] = self._module_node(self._file_scope(file))
        for specifier, scope in self._ambient_modules().items():
            file = self._subpaths.get(specifier)
            # The entry file may be a script whose only job is `declare module "pkg" {...}`.
            if file is None or not self._is_module(self._file_scope(file)):
                roots[f"{specifier}:"] = self._module_node(scope)
        return Surface(roots, self.nodes)

    def _entry_points(self, package: dict[str, Any]) -> dict[str, str]:
        found: dict[str, str] = {}
        exports = package.get("exports")
        if isinstance(exports, str) or (
            isinstance(exports, dict) and not any(k.startswith(".") for k in exports)
        ):
            exports = {".": exports}
        if isinstance(exports, dict):
            for key, value in exports.items():
                if not key.startswith("."):
                    continue
                if "*" in key:
                    found.update(self._wildcard(key, value))
                elif file := self._condition_target(value):
                    found[self._specifier(key)] = file
        if self.module not in found:
            for candidate in (package.get("types"), package.get("typings"), package.get("main")):
                if isinstance(candidate, str) and (file := self._declaration_for(candidate)):
                    found[self.module] = file
                    break
            else:
                if file := self._declaration_for("index"):
                    found[self.module] = file
        if exports is None:  # no export map: every declaration file is a deep import
            for rel in sorted(self.files)[:MAX_SUBPATHS]:
                sub = _strip_dts(rel)
                sub = sub.removesuffix("/index") if sub != "index" else ""
                if sub:
                    found.setdefault(f"{self.module}/{sub}", rel)
        return found

    @staticmethod
    def _is_module(scope: _Scope) -> bool:
        return scope.has_exports or bool(scope.imports) or scope.export_equals is not None

    def _specifier(self, key: str) -> str:
        sub = key.removeprefix(".").strip("/")
        return f"{self.module}/{sub}" if sub else self.module

    def _wildcard(self, key: str, value: Any) -> dict[str, str]:
        target = self._condition_value(value)
        if not isinstance(target, str) or target.count("*") != 1:
            return {}
        head, tail = target.removeprefix("./").split("*")
        tails = [tail] if tail.endswith(DTS_SUFFIXES) else [
            tail[: -len(ext)] + dts for ext, dts in _JS_TO_DTS.items() if tail.endswith(ext)
        ]  # fmt: skip
        found = {}
        for rel in self.files:
            for t in tails:
                if rel.startswith(head) and rel.endswith(t) and len(found) < MAX_SUBPATHS:
                    found[self._specifier(key.replace("*", rel[len(head) : len(rel) - len(t)]))] = (
                        rel
                    )
        return found

    def _condition_value(self, value: Any) -> Any:
        if isinstance(value, list):
            return next((v for v in map(self._condition_value, value) if v), None)
        if not isinstance(value, dict):
            return value
        for condition in _CONDITIONS:
            picked = self._condition_value(value[condition]) if condition in value else None
            if isinstance(picked, str) and (
                condition in ("types", "typings") or self._declaration_for(picked)
            ):
                return picked
        return None

    def _condition_target(self, value: Any) -> str | None:
        target = self._condition_value(value)
        return self._declaration_for(target) if isinstance(target, str) else None

    def _declaration_for(self, path: str, base: str = "") -> str | None:
        """Declaration file for a path as written in package.json or an import."""
        rel = posixpath.normpath(posixpath.join(base, path.removeprefix("./")))
        if rel.startswith(".."):
            return None
        candidates = [rel]
        stem, ext = posixpath.splitext(rel)
        if ext in _JS_TO_DTS:
            candidates.append(stem + _JS_TO_DTS[ext])
        if ext in (".ts", ".mts", ".cts") and not rel.endswith(DTS_SUFFIXES):
            candidates.append(stem + ".d" + ext)
        candidates += [rel + s for s in DTS_SUFFIXES] + [f"{rel}/index{s}" for s in DTS_SUFFIXES]
        return next((c for c in candidates if c in self.files), None)

    # scopes

    def _file_scope(self, rel: str) -> _Scope:
        if rel not in self._scopes:
            scope = _Scope(id=rel, file=rel)
            self._scopes[rel] = scope
            tree = tsparse.parse(self.files[rel].read_bytes(), rel)
            self._fill(scope, tree.root_node.named_children)
        return self._scopes[rel]

    def _ambient_modules(self) -> dict[str, _Scope]:
        if not self._ambient_scanned:
            self._ambient_scanned = True
            for rel, path in self.files.items():
                if b"declare module" in path.read_bytes():
                    self._file_scope(rel)
        return self._ambient

    def _fill(self, scope: _Scope, statements: Iterable[TsNode]) -> None:
        for stmt in statements:
            if stmt.type == "export_statement":
                self._export(scope, stmt)
            elif stmt.type == "import_statement":
                self._import(scope, stmt)
            elif stmt.type == "expression_statement":  # tree-sitter puts nested namespaces here
                self._fill(scope, stmt.named_children)
            else:
                self._declare(scope, stmt)

    def _declare(self, scope: _Scope, node: TsNode, exported: bool = False) -> list[str]:
        """Record a declaration; returns the names it declares."""
        kind = node.type
        if kind == "ambient_declaration":
            names: list[str] = []
            for c in node.named_children:
                names += self._declare(scope, c, exported)
            return names
        if kind in ("function_signature", "function_declaration"):
            return self._named(scope, node, "function", exported)
        if kind in ("class_declaration", "abstract_class_declaration", "class"):
            return self._named(scope, node, "class", exported)
        if kind == "interface_declaration":
            return self._named(scope, node, "interface", exported)
        if kind == "enum_declaration":
            return self._named(scope, node, "enum", exported)
        if kind == "type_alias_declaration":
            return self._named(scope, node, "type", exported)
        if kind in ("lexical_declaration", "variable_declaration"):
            names = []
            for d in node.named_children:
                if d.type == "variable_declarator":
                    names += self._named(scope, d, "variable", exported)
            return names
        if kind in ("internal_module", "module"):
            name_node = node.child_by_field_name("name")
            body = node.child_by_field_name("body")
            if name_node is not None and name_node.type == "string":
                self._ambient_module(scope, tsparse.string_value(name_node), body)
                return []
            return self._namespace(scope, name_node, body, exported)
        return []

    def _named(self, scope: _Scope, node: TsNode, kind: str, exported: bool) -> list[str]:
        name = node.child_by_field_name("name")
        if name is None or name.type not in ("identifier", "type_identifier"):
            return []
        text = tsparse.text(name)
        scope.add(text, _Decl(kind, node))
        if exported:
            scope.exports[text] = ("local", text)
        return [text]

    def _namespace(
        self, scope: _Scope, name: TsNode | None, body: TsNode | None, exported: bool
    ) -> list[str]:
        if name is None:
            return []
        parts = tsparse.text(name).split(".")  # namespace A.B.C { }
        target, created = scope, []
        for i, part in enumerate(parts):
            existing = next((d for d in target.decls.get(part, []) if d.scope), None)
            if existing is None:
                inner = _Scope(id=f"{target.id}/ns:{part}", file=scope.file, parent=target)
                inner.implicit_export = True
                existing = _Decl("namespace", name, inner)
                target.add(part, existing)
            if i == 0:
                created.append(part)
                if exported:
                    scope.exports[part] = ("local", part)
            assert existing.scope is not None
            target = existing.scope
        if body is not None:
            # Declaration files are ambient, and ambient namespace members are always exported.
            self._fill(target, body.named_children)
        return created

    def _ambient_module(self, scope: _Scope, specifier: str | None, body: TsNode | None) -> None:
        # Only this package's own modules: `declare module "vue"` in a plugin augments vue.
        if specifier is None or not (
            specifier == self.module or specifier.startswith(self.module + "/")
        ):
            return
        inner = self._ambient.get(specifier)
        if inner is None:
            inner = _Scope(id=f"{scope.file}/module:{specifier}", file=scope.file, parent=scope)
            inner.implicit_export = True
            self._ambient[specifier] = inner
            self._scopes[inner.id] = inner
        if body is not None:
            if any(c.type == "export_statement" for c in body.named_children):
                inner.implicit_export = False
            self._fill(inner, body.named_children)

    def _export(self, scope: _Scope, node: TsNode) -> None:
        scope.has_exports = True
        tokens = [c.type for c in node.children]
        source = tsparse.string_value(node.child_by_field_name("source"))
        declaration = node.child_by_field_name("declaration")
        if declaration is not None:
            is_default = "default" in tokens
            names = self._declare(scope, declaration, exported=not is_default)
            if is_default and names:
                scope.exports["default"] = ("local", names[0])
            return
        value = node.child_by_field_name("value")
        if "default" in tokens:
            if value is not None and value.type == "identifier":
                scope.exports["default"] = ("local", tsparse.text(value))
            elif value is not None and value.type in ("class", "function_expression"):
                scope.add("default", _Decl("class" if value.type == "class" else "function", value))
                scope.exports["default"] = ("local", "default")
            else:
                scope.exports["default"] = ("external",)
            return
        if "=" in tokens:  # export = Name
            target = tsparse.first_child(node, "identifier")
            scope.export_equals = tsparse.text(target) if target is not None else None
            if target is None:
                scope.exports["default"] = ("external",)
            return
        if "namespace" in tokens and "as" in tokens:  # export as namespace X (UMD global)
            return
        clause = tsparse.first_child(node, "export_clause")
        namespace_export = next(
            (c for c in node.named_children if c.type == "namespace_export"), None
        )
        if clause is not None:
            for spec in clause.named_children:
                if spec.type != "export_specifier":
                    continue
                name = tsparse.text(spec.child_by_field_name("name"))
                alias = tsparse.text(spec.child_by_field_name("alias")) or name
                scope.exports[alias] = ("import", source, name) if source else ("local", name)
        elif namespace_export is not None and source:
            alias = next(
                (c for c in namespace_export.named_children if c.type == "identifier"), None
            )
            if alias is not None:
                scope.exports[tsparse.text(alias)] = ("module", source)
        elif source and "*" in tokens:
            scope.stars.append(source)

    def _import(self, scope: _Scope, node: TsNode) -> None:
        source = tsparse.string_value(node.child_by_field_name("source"))
        for part in node.named_children:
            if part.type == "import_require_clause":
                name = tsparse.first_child(part, "identifier")
                spec = tsparse.string_value(part.child_by_field_name("source"))
                if name is not None and spec:
                    scope.imports[tsparse.text(name)] = (spec, "=")
            if part.type != "import_clause" or not source:
                continue
            for c in part.named_children:
                if c.type == "identifier":
                    scope.imports[tsparse.text(c)] = (source, "default")
                elif c.type == "namespace_import":
                    alias = tsparse.first_child(c, "identifier")
                    if alias is not None:
                        scope.imports[tsparse.text(alias)] = (source, "*")
                elif c.type == "named_imports":
                    for spec_node in c.named_children:
                        if spec_node.type == "import_specifier":
                            name = tsparse.text(spec_node.child_by_field_name("name"))
                            alias = tsparse.text(spec_node.child_by_field_name("alias")) or name
                            scope.imports[alias] = (source, name)

    def _module_scope(self, from_scope: _Scope, specifier: str) -> _Scope | None:
        if specifier.startswith("."):
            base = posixpath.dirname(from_scope.file)
            file = self._declaration_for(specifier, base)
            return self._file_scope(file) if file else None
        if specifier in self._subpaths:
            return self._file_scope(self._subpaths[specifier])
        if specifier == self.module or specifier.startswith(self.module + "/"):
            return self._ambient_modules().get(specifier)
        return None  # another package

    # nodes

    def _module_node(self, scope: _Scope) -> str:
        if scope.id in self.nodes:
            return scope.id
        node = Node("module")
        self.nodes[scope.id] = node
        if scope.export_equals is not None:
            target = self._lookup(scope, scope.export_equals)
            node.members["default"] = target or EXTERNAL
            resolved = self.nodes.get(target or "")
            if resolved is None:
                node.dynamic = True
            else:
                node.members.update({k: v for k, v in resolved.members.items() if k != "default"})
                node.dynamic = resolved.dynamic
            return scope.id
        names = set(scope.exports)
        if scope.implicit_export:
            names |= set(scope.decls)
        for name in sorted(names):
            ref = scope.exports.get(name, ("local", name))
            node.members[name] = self._ref(scope, ref) or EXTERNAL
        for specifier in scope.stars:
            source = self._module_scope(scope, specifier)
            if source is None:
                node.dynamic = True  # re-exports a module outside this package
                continue
            source_node = self.nodes[self._module_node(source)]
            node.dynamic = node.dynamic or source_node.dynamic
            for name, target in source_node.members.items():
                if name != "default":
                    node.members.setdefault(name, target)
        if not node.members and not scope.has_exports and not scope.implicit_export:
            node.dynamic = True  # a global script, not a module: we can't tell what it exports
        return scope.id

    def _ref(self, scope: _Scope, ref: tuple[str, ...]) -> str | None:
        if ref[0] == "local":
            return self._lookup(scope, ref[1])
        if ref[0] == "import":
            return self._imported(scope, ref[1], ref[2])
        if ref[0] == "module":
            source = self._module_scope(scope, ref[1])
            return self._module_node(source) if source else None
        return None

    def _imported(self, scope: _Scope, specifier: str, name: str) -> str | None:
        source = self._module_scope(scope, specifier)
        if source is None:
            return None
        module_id = self._module_node(source)
        if name in ("*", "="):
            return module_id
        return self.nodes[module_id].members.get(name)

    def _lookup(self, scope: _Scope, name: str) -> str | None:
        """Node for a name visible from `scope`, or None if it comes from outside the package."""
        current: _Scope | None = scope
        while current is not None:
            if name in current.decls:
                return self._decl_node(current, name)
            if name in current.imports:
                target = self._imported(current, *current.imports[name])
                return None if target == EXTERNAL else target
            current = current.parent
        return None

    def _decl_node(self, scope: _Scope, name: str) -> str:
        node_id = f"{scope.id}#{name}"  # scope ids never contain "#", so this is unique
        if node_id in self.nodes:
            return node_id
        if node_id in self._resolving:
            return node_id if node_id in self.nodes else EXTERNAL
        decls = scope.decls[name]
        kinds = {d.kind for d in decls}

        # `declare const x: SomeInterface` is that interface for member access.
        if kinds in ({"variable"}, {"type"}):
            field_name = "type" if kinds == {"variable"} else "value"
            self._resolving.add(node_id)
            try:
                typed = self._type_node(scope, decls[0].node.child_by_field_name(field_name))
            finally:
                self._resolving.discard(node_id)
            if typed is not None and typed != EXTERNAL:
                return typed

        node = Node("attribute")
        self.nodes[node_id] = node
        callables = [d for d in decls if d.kind == "function"]
        if kinds & {"class", "interface", "enum"} or (callables and "namespace" in kinds):
            node.kind = "class"
        elif callables:
            node.kind = "function"
        elif "namespace" in kinds:
            node.kind = "module"

        signatures: list[TsNode] = [d.node for d in callables]
        for decl in decls:
            if decl.kind in ("class", "interface"):
                signatures += self._fill_type_members(scope, decl.node, node)
            elif decl.kind == "enum":
                body = decl.node.child_by_field_name("body")
                for member in body.named_children if body else []:
                    member_name = member.child_by_field_name("name") or member
                    node.members.setdefault(tsparse.text(member_name), EXTERNAL)
            elif decl.kind == "namespace" and decl.scope is not None:
                inner = self.nodes[self._module_node(decl.scope)]
                node.dynamic = node.dynamic or inner.dynamic
                for member, target in inner.members.items():
                    node.members.setdefault(member, target)
            elif decl.kind == "variable" and len(kinds) > 1:  # const merged with a namespace
                typed = self.nodes.get(
                    self._type_node(scope, decl.node.child_by_field_name("type")) or ""
                )
                if typed is None or typed.dynamic:
                    node.dynamic = True
                else:
                    for member, target in typed.members.items():
                        node.members.setdefault(member, target)
        node.params = _params_of(signatures)
        return node_id

    def _fill_type_members(self, scope: _Scope, decl: TsNode, node: Node) -> list[TsNode]:
        """Copy class/interface members into `node`; returns its call or construct signatures."""
        body = decl.child_by_field_name("body")
        signatures: list[TsNode] = []
        if body is not None:
            signatures = self._members(scope, body, node, f"{scope.id}@{decl.start_byte}")
        for base in _heritage(decl):
            base_id = self._type_node(scope, base)
            base_node = self.nodes.get(base_id or "")
            if base_node is None or base_node.dynamic:
                node.dynamic = True  # inherits members we can't see
                continue
            for member, target in base_node.members.items():
                node.members.setdefault(member, target)
        return signatures

    def _members(self, scope: _Scope, body: TsNode, node: Node, owner: str) -> list[TsNode]:
        signatures: list[TsNode] = []
        methods: dict[str, list[TsNode]] = {}
        for member in body.named_children:
            kind = member.type
            if kind == "index_signature":
                node.dynamic = True
            elif kind in ("call_signature", "construct_signature"):
                signatures.append(member)
            elif kind in (
                "method_signature", "method_definition", "abstract_method_signature",
            ):  # fmt: skip
                name = tsparse.text(member.child_by_field_name("name"))
                if name == "constructor":
                    signatures.append(member)
                elif name:
                    methods.setdefault(name, []).append(member)
            elif kind in ("property_signature", "public_field_definition"):
                name = tsparse.text(member.child_by_field_name("name"))
                if name:
                    typed = self._type_node(scope, member.child_by_field_name("type"))
                    node.members.setdefault(name, typed or EXTERNAL)
        for name, overloads in methods.items():
            member_id = f"{owner}.{name}"
            self.nodes[member_id] = Node("function", params=_params_of(overloads))
            node.members.setdefault(name, member_id)
        return signatures

    def _type_node(self, scope: _Scope, type_node: TsNode | None) -> str | None:
        """Node describing the members of a type, when the type is declared in this package."""
        if type_node is None:
            return None
        kind = type_node.type
        if kind in ("type_annotation", "parenthesized_type"):
            inner = next(iter(type_node.named_children), None)
            return self._type_node(scope, inner)
        if kind == "generic_type":
            return self._type_node(scope, type_node.child_by_field_name("name"))
        if kind in ("type_identifier", "identifier"):
            return self._lookup(scope, tsparse.text(type_node))
        if kind in ("nested_type_identifier", "nested_identifier", "member_expression"):
            parts = tsparse.text(type_node).split(".")
            target = self._lookup(scope, parts[0])
            for part in parts[1:]:
                target = self.nodes[target].members.get(part) if target in self.nodes else None
            return None if target == EXTERNAL else target
        if kind == "type_query":  # typeof x
            inner = next(iter(type_node.named_children), None)
            return self._type_node(scope, inner)
        if kind == "object_type":
            node_id = f"{scope.id}@{type_node.start_byte}"
            if node_id not in self.nodes:
                node = Node("class")
                self.nodes[node_id] = node
                node.params = _params_of(self._members(scope, type_node, node, node_id))
            return node_id
        if kind == "function_type":
            node_id = f"{scope.id}@{type_node.start_byte}"
            self.nodes.setdefault(node_id, Node("function", params=_params_of([type_node])))
            return node_id
        return None  # unions, literals, mapped types, ...: opaque


def _heritage(decl: TsNode) -> list[TsNode]:
    bases: list[TsNode] = []
    for c in decl.named_children:
        if c.type == "extends_type_clause":  # interface X extends A, B
            bases += [t for t in c.named_children]
        elif c.type == "class_heritage":
            for clause in c.named_children:
                if clause.type == "extends_clause":
                    value = clause.child_by_field_name("value")
                    if value is not None:
                        bases.append(value)
    return bases


def _params_of(signatures: list[TsNode]) -> tuple[Param, ...] | None:
    """Parameters of the single signature; None when overloaded or unknown."""
    if len(signatures) != 1:
        return None
    formal = signatures[0].child_by_field_name("parameters")
    if formal is None:
        return None
    params = []
    for i, p in enumerate(formal.named_children):
        pattern = p.child_by_field_name("pattern")
        if pattern is None:
            continue
        if pattern.type == "this":
            continue  # `this` parameter is not passed
        if pattern.type == "rest_pattern":
            params.append(Param(f"arg{i}", "var_positional"))
            continue
        name = tsparse.text(pattern) if pattern.type == "identifier" else f"arg{i}"
        required = p.type == "required_parameter" and p.child_by_field_name("value") is None
        params.append(Param(name, "positional_only", required=required))
    return tuple(params)


def _strip_dts(rel: str) -> str:
    for suffix in DTS_SUFFIXES:
        if rel.endswith(suffix):
            return rel[: -len(suffix)]
    return rel


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def build_dts_surface(root: Path, package: str) -> Surface | None:
    surface = DtsBuilder(root, specifier_name(package)).build()
    return surface if surface.roots else None
