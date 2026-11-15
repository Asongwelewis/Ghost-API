"""Routes (Express-style routers, Next.js, NestJS) and HTTP calls (fetch, axios) in JS/TS."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from ghostapi import tsparse
from ghostapi.endpoints import (
    ANY_METHOD,
    DYNAMIC,
    HTTP_METHODS,
    Endpoint,
    FileApi,
    HttpCall,
    Mount,
    RawRoute,
    RouterObject,
    internal_path,
    route_segments,
)
from ghostapi.tsparse import Node

_ROUTE_METHODS = {m.lower() for m in HTTP_METHODS} | {"all"}
_APP_FACTORIES = {"express": "express", "fastify": "fastify", "Fastify": "fastify"}
_NEW_APPS = {"Hono": "hono", "Elysia": "elysia"}
_ROUTER_SOURCES = {"express", "@koa/router", "koa-router", "router"}
_FETCH_NAMES = {"fetch", "$fetch", "ofetch", "useFetch"}
_NEST_METHODS = {"Get", "Post", "Put", "Patch", "Delete", "Options", "Head", "All"}
_NEXT_CONFIGS = ("next.config.js", "next.config.mjs", "next.config.ts", "next.config.cjs")
_SCOPES = {"function_declaration", "class_declaration", "method_definition"}


def _text(node: Node | None) -> str:
    return tsparse.text(node)


def _args(call: Node) -> list[Node]:
    args = call.child_by_field_name("arguments")
    return [a for a in (args.named_children if args else []) if a.type != "comment"]


def _property(obj: Node | None, name: str) -> Node | None:
    """Value of `name: value` in an object literal."""
    if obj is None or obj.type != "object":
        return None
    for pair in obj.named_children:
        if pair.type == "pair":
            key = pair.child_by_field_name("key")
            if key is not None and (_text(key) == name or tsparse.string_value(key) == name):
                return pair.child_by_field_name("value")
    return None


def _member_parts(node: Node | None) -> tuple[str, ...] | None:
    if node is None:
        return None
    if node.type == "identifier":
        return (_text(node),)
    if node.type == "member_expression":
        base = _member_parts(node.child_by_field_name("object"))
        prop = node.child_by_field_name("property")
        return None if base is None or prop is None else (*base, _text(prop))
    if node.type == "call_expression":  # koa: router.routes()
        fn = node.child_by_field_name("function")
        if fn is not None and fn.type == "member_expression":
            return _member_parts(fn.child_by_field_name("object"))
    return None


class _Visitor:
    def __init__(self, api: FileApi, hosts: tuple[str, ...], base_names: tuple[str, ...]):
        self.api = api
        self.hosts = hosts
        self.base_names = base_names
        self.constants: dict[str, str] = {}
        self.axios: set[str] = set()  # names bound to the axios module
        self.clients: dict[str, str | None] = {}  # axios instances -> baseURL
        self.scope: list[str] = []

    # module structure

    def collect_module(self, root: Node) -> None:
        for stmt in root.named_children:
            node = stmt
            if stmt.type == "export_statement":
                self._export(stmt)
                node = stmt.child_by_field_name("declaration") or stmt
            if node.type == "import_statement":
                self._import(node)
            if node.type in ("lexical_declaration", "variable_declaration"):
                for d in node.named_children:
                    if d.type == "variable_declarator":
                        self._declare(d, top_level=True)
            if node.type == "expression_statement":
                self._commonjs_export(node)

    def _import(self, node: Node) -> None:
        source = tsparse.string_value(node.child_by_field_name("source"))
        clause = tsparse.first_child(node, "import_clause")
        if source is None or clause is None:
            return
        for part in clause.named_children:
            if part.type == "identifier":
                self._bind(_text(part), source, "default")
            elif part.type == "namespace_import":
                alias = tsparse.first_child(part, "identifier")
                if alias is not None:
                    self._bind(_text(alias), source, "*")
            elif part.type == "named_imports":
                for spec in part.named_children:
                    if spec.type == "import_specifier":
                        name = _text(spec.child_by_field_name("name"))
                        alias = _text(spec.child_by_field_name("alias")) or name
                        self._bind(alias, source, name)

    def _bind(self, local: str, source: str, name: str) -> None:
        self.api.imports[local] = (source, name, 0)
        if source == "axios" and name in ("default", "*", "="):
            self.axios.add(local)

    def _export(self, stmt: Node) -> None:
        tokens = [c.type for c in stmt.children]
        value = stmt.child_by_field_name("value")
        if "default" in tokens and value is not None and value.type == "identifier":
            self.api.exports["default"] = _text(value)
        declaration = stmt.child_by_field_name("declaration")
        if declaration is not None and declaration.type in ("lexical_declaration",):
            for d in declaration.named_children:
                name = d.child_by_field_name("name") if d.type == "variable_declarator" else None
                if name is not None and name.type == "identifier":
                    self.api.exports[_text(name)] = _text(name)
        clause = tsparse.first_child(stmt, "export_clause")
        if clause is not None and stmt.child_by_field_name("source") is None:
            for spec in clause.named_children:
                if spec.type == "export_specifier":
                    name = _text(spec.child_by_field_name("name"))
                    alias = _text(spec.child_by_field_name("alias")) or name
                    self.api.exports[alias] = name

    def _commonjs_export(self, stmt: Node) -> None:
        expr = next(iter(stmt.named_children), None)
        if expr is None or expr.type != "assignment_expression":
            return
        left, right = expr.child_by_field_name("left"), expr.child_by_field_name("right")
        target = _member_parts(left)
        if right is None or right.type != "identifier" or target is None:
            return
        if target == ("module", "exports"):
            self.api.exports["default"] = _text(right)
        elif len(target) == 2 and target[0] == "exports":
            self.api.exports[target[1]] = _text(right)
        elif len(target) == 3 and target[:2] == ("module", "exports"):
            self.api.exports[target[2]] = _text(right)

    def _declare(self, declarator: Node, top_level: bool) -> None:
        name_node = declarator.child_by_field_name("name")
        value = declarator.child_by_field_name("value")
        if name_node is None or value is None:
            return
        if name_node.type == "object_pattern":  # const { Router } = require("express")
            source = self._require_source(value)
            if source is not None:
                for prop in name_node.named_children:
                    if prop.type == "shorthand_property_identifier_pattern":
                        self._bind(_text(prop), source, _text(prop))
            return
        if name_node.type != "identifier":
            return
        name = _text(name_node)
        if (source := self._require_source(value)) is not None:
            self._bind(name, source, "=")
            return
        if top_level and (text := self._string(value)) is not None and DYNAMIC not in text:
            self.constants[name] = text
        obj = self._router_object(value)
        if obj is not None:
            self.api.objects[name] = obj
        base = self._axios_create(value)
        if base is not False:
            self.clients[name] = base  # type: ignore[assignment]

    def _require_source(self, value: Node) -> str | None:
        if (
            value.type != "call_expression"
            or _text(value.child_by_field_name("function")) != "require"
        ):
            return None
        args = _args(value)
        return tsparse.string_value(args[0]) if args else None

    def _router_object(self, value: Node) -> RouterObject | None:
        prefix = ""
        node = value
        # Hono: new Hono().basePath("/api")
        fn = node.child_by_field_name("function") if node.type == "call_expression" else None
        if (
            fn is not None
            and fn.type == "member_expression"
            and _text(fn.child_by_field_name("property")) == "basePath"
        ):
            args = _args(node)
            prefix = (self._string(args[0]) if args else None) or ""
            node = fn.child_by_field_name("object") or node
        if node.type == "call_expression":
            callee = node.child_by_field_name("function")
            callee_text = _text(callee)
            if callee_text in _APP_FACTORIES and self._imported_from(
                callee_text, ("express", "fastify")
            ):
                return RouterObject("app", _APP_FACTORIES[callee_text], prefix)
            if callee_text in ("express.Router", "Router") and self._router_import(callee_text):
                return RouterObject("router", "express", prefix)
        if node.type == "new_expression":
            ctor = _text(node.child_by_field_name("constructor"))
            args = _args(node)
            if ctor in _NEW_APPS:
                return RouterObject("app", _NEW_APPS[ctor], prefix)
            if ctor == "Router" and self._router_import(ctor):
                options = args[0] if args else None
                own = self._string(_property(options, "prefix")) if options else None
                return RouterObject("router", "koa", own or prefix)
        return None

    def _imported_from(self, name: str, sources: Iterable[str]) -> bool:
        root = name.split(".")[0]
        bound = self.api.imports.get(root)
        return bound is not None and bound[0] in sources

    def _router_import(self, callee: str) -> bool:
        root = callee.split(".")[0]
        bound = self.api.imports.get(root)
        return bound is not None and bound[0] in _ROUTER_SOURCES

    def _axios_create(self, value: Node) -> str | None | bool:
        """baseURL of `axios.create({...})` ("" when absent, None when computed); False if not."""
        if value.type != "call_expression":
            return False
        fn = value.child_by_field_name("function")
        parts = _member_parts(fn)
        if parts is None or len(parts) != 2 or parts[0] not in self.axios or parts[1] != "create":
            return False
        args = _args(value)
        base = _property(args[0], "baseURL") if args else None
        return "" if base is None else self._string(base)

    # code

    def visit(self, node: Node, depth: int = 0) -> None:
        if depth > 400:
            return
        if node.type == "call_expression":
            self._call(node)
        elif node.type in ("class_declaration", "abstract_class_declaration"):
            self._nest_controller(node)
        if node.type == "variable_declarator" and self.scope:
            self._declare(node, top_level=False)
        pushed = self._enter_scope(node)
        for child in node.named_children:
            self.visit(child, depth + 1)
        if pushed:
            self.scope.pop()

    def _enter_scope(self, node: Node) -> bool:
        name = None
        if node.type in _SCOPES:
            name = node.child_by_field_name("name")
        elif node.type == "variable_declarator":
            value = node.child_by_field_name("value")
            if value is not None and value.type in ("arrow_function", "function_expression"):
                name = node.child_by_field_name("name")
        if name is None:
            return False
        self.scope.append(_text(name))
        return True

    def _call(self, node: Node) -> None:
        fn = node.child_by_field_name("function")
        if fn is None:
            return
        if fn.type == "member_expression":
            obj = fn.child_by_field_name("object")
            verb = _text(fn.child_by_field_name("property"))
            owner = _text(obj) if obj is not None and obj.type == "identifier" else None
            if owner in self.api.objects:
                self._object_call(owner, verb, node)
            elif verb in _ROUTE_METHODS and obj is not None and obj.type == "call_expression":
                self._route_chain(node, verb)
            if verb == "setGlobalPrefix" and (args := _args(node)):
                self.api.nest_prefix = self._string(args[0])
            elif verb == "enableVersioning":
                self.api.nest_versioned = True
            if owner in self.axios or owner in self.clients:
                self._axios_call(node, owner, verb)
            elif _text(fn) in ("window.fetch", "globalThis.fetch"):
                self._fetch_call(node)
        elif fn.type == "identifier":
            name = _text(fn)
            if name in _FETCH_NAMES:
                self._fetch_call(node)
            elif name in self.axios:
                self._axios_call(node, name, None)

    def _object_call(self, owner: str, verb: str, node: Node) -> None:
        args = _args(node)
        if verb in _ROUTE_METHODS and args:
            path = self._string(args[0])
            if path is not None and DYNAMIC not in path:
                method = ANY_METHOD if verb == "all" else verb.upper()
                framework = self.api.objects[owner].framework
                self.api.routes.append(
                    RawRoute(owner, method, path, tsparse.line(node), self._symbol(), framework)
                )
        elif verb in ("use", "route") and args:
            prefix = (
                self._string(args[0]) if args[0].type in ("string", "template_string") else None
            )
            children = args[1:] if prefix is not None else args
            for child in children:
                parts = _member_parts(child)
                if parts is not None:
                    self.api.mounts.append(Mount(owner, parts, prefix, tsparse.line(node)))

    def _route_chain(self, node: Node, verb: str) -> None:
        """router.route("/orders").get(list).post(create)"""
        methods = [verb]
        current = node.child_by_field_name("function")
        while current is not None and current.type == "member_expression":
            inner = current.child_by_field_name("object")
            if inner is None or inner.type != "call_expression":
                return
            fn = inner.child_by_field_name("function")
            if fn is None or fn.type != "member_expression":
                return
            prop = _text(fn.child_by_field_name("property"))
            base = fn.child_by_field_name("object")
            if prop == "route" and base is not None and _text(base) in self.api.objects:
                args = _args(inner)
                path = self._string(args[0]) if args else None
                if path is not None and DYNAMIC not in path:
                    owner = _text(base)
                    framework = self.api.objects[owner].framework
                    for m in methods:
                        method = ANY_METHOD if m == "all" else m.upper()
                        self.api.routes.append(
                            RawRoute(
                                owner, method, path, tsparse.line(inner), self._symbol(), framework
                            )
                        )
                return
            if prop not in _ROUTE_METHODS:
                return
            methods.append(prop)
            current = fn

    def _symbol(self) -> str | None:
        return ".".join(self.scope) or None

    # NestJS

    def _nest_controller(self, cls: Node) -> None:
        decorators = [c for c in cls.named_children if c.type == "decorator"]
        parent = cls.parent
        if parent is not None and parent.type == "export_statement":
            decorators += [c for c in parent.named_children if c.type == "decorator"]
        prefix = None
        for decorator in decorators:
            call = tsparse.first_child(decorator, "call_expression")
            if call is not None and _text(call.child_by_field_name("function")) == "Controller":
                args = _args(call)
                first = args[0] if args else None
                if first is None:
                    prefix = ""
                elif first.type == "object":
                    prefix = self._string(_property(first, "path")) or ""
                else:
                    prefix = self._string(first)
        if prefix is None:
            return
        body = cls.child_by_field_name("body")
        class_name = _text(cls.child_by_field_name("name"))
        pending: list[Node] = []
        for member in body.named_children if body else []:
            if member.type == "decorator":
                pending.append(member)
                continue
            if member.type == "method_definition":
                for decorator in pending:
                    self._nest_route(
                        decorator,
                        prefix,
                        f"{class_name}.{_text(member.child_by_field_name('name'))}",
                    )
            pending = []

    def _nest_route(self, decorator: Node, prefix: str, symbol: str) -> None:
        call = tsparse.first_child(decorator, "call_expression")
        if call is None:
            return
        verb = _text(call.child_by_field_name("function"))
        if verb not in _NEST_METHODS:
            return
        args = _args(call)
        path = (self._string(args[0]) if args else "") or ""
        method = ANY_METHOD if verb == "All" else verb.upper()
        segments = route_segments(f"{prefix}/{path}")
        self.api.absolute.append(
            Endpoint(method, segments, self.api.file, tsparse.line(call), symbol, "nestjs")
        )

    # HTTP calls

    def _string(self, node: Node | None) -> str | None:
        if node is None:
            return None
        kind = node.type
        if kind == "string":
            return tsparse.string_value(node)
        if kind == "template_string":
            out = []
            for part in node.named_children:
                if part.type == "string_fragment":
                    out.append(_text(part))
                elif part.type == "template_substitution":
                    inner = next(iter(part.named_children), None)
                    value = self.constants.get(_text(inner)) if inner is not None else None
                    if value is None:
                        value = "" if _text(inner) in self.base_names else DYNAMIC
                    out.append(value)
            return "".join(out)
        if kind == "binary_expression" and _text(node.child_by_field_name("operator")) == "+":
            left = self._string(node.child_by_field_name("left"))
            if left is None:
                return None
            right = self._string(node.child_by_field_name("right"))
            return left + (DYNAMIC if right is None else right)
        if kind == "parenthesized_expression":
            return self._string(next(iter(node.named_children), None))
        text = _text(node)
        if text in self.base_names:
            return ""
        if kind == "identifier":
            return self.constants.get(text)
        return None

    def _record(
        self, method: str | None, url: str | None, node: Node, client: str, base: str = ""
    ) -> None:
        if url is None:
            return
        if base and not url.startswith(("http://", "https://")):
            url = base.rstrip("/") + "/" + url.lstrip("/")
        segments = internal_path(url, self.hosts)
        if segments is None:
            return
        self.api.calls.append(
            HttpCall(
                method,
                segments,
                self.api.file,
                tsparse.line(node),
                tsparse.col(node),
                self._symbol() or "<module>",
                client,
            )
        )

    def _fetch_call(self, node: Node) -> None:
        args = _args(node)
        if not args:
            return
        method: str | None = "GET"
        if len(args) > 1:
            options = args[1]
            if options.type != "object":
                method = None  # options computed at runtime
            elif (value := _property(options, "method")) is not None:
                method = (self._string(value) or "").upper() or None
        self._record(method, self._string(args[0]), node, "fetch")

    def _axios_call(self, node: Node, owner: str, verb: str | None) -> None:
        base = self.clients.get(owner, "")
        if base is None:
            return  # baseURL computed at runtime
        args = _args(node)
        if verb in _ROUTE_METHODS - {"all"} and args:
            self._record(verb.upper(), self._string(args[0]), node, "axios", base)
        elif verb in (None, "request") and args:
            config = args[0] if args[0].type == "object" else (args[1] if len(args) > 1 else None)
            url = (
                self._string(args[0])
                if args[0].type != "object"
                else self._string(_property(config, "url"))
            )
            method_node = _property(config, "method") if config is not None else None
            method = (self._string(method_node) or "").upper() or None if method_node else "GET"
            self._record(method, url, node, "axios", base)


def _next_routes(api: FileApi, rel: str, root: Path | None, exported: set[str]) -> None:
    """Next.js file-system routes: app/**/route.ts and pages/api/**."""
    parts = rel.split("/")
    stem = parts[-1].rsplit(".", 1)[0]
    for i, part in enumerate(parts[:-1]):
        project = Path(*parts[:i]) if i else Path()
        if part == "src":
            continue
        base = project.parent if project.name == "src" else project
        if root is not None and not any((root / base / c).is_file() for c in _NEXT_CONFIGS):
            continue
        if part == "app" and stem == "route":
            dirs = [d for d in parts[i + 1 : -1] if not d.startswith(("(", "@", "_"))]
            for method in sorted(exported & set(HTTP_METHODS)):
                api.absolute.append(
                    Endpoint(method, route_segments("/".join(dirs)), rel, 1, method, "nextjs")
                )
            return
        if part == "pages" and i + 1 < len(parts) - 1 and parts[i + 1] == "api":
            if parts[-1].startswith("_"):
                return
            dirs = parts[i + 1 : -1] + ([] if stem == "index" else [stem])
            api.absolute.append(
                Endpoint(ANY_METHOD, route_segments("/".join(dirs)), rel, 1, None, "nextjs")
            )
            return


def _exported_names(root: Node) -> set[str]:
    names: set[str] = set()
    for stmt in root.named_children:
        if stmt.type != "export_statement":
            continue
        declaration = stmt.child_by_field_name("declaration")
        if declaration is not None:
            if declaration.type == "function_declaration":
                names.add(_text(declaration.child_by_field_name("name")))
            for d in declaration.named_children:
                if d.type == "variable_declarator":
                    names.add(_text(d.child_by_field_name("name")))
        clause = tsparse.first_child(stmt, "export_clause")
        for spec in clause.named_children if clause else []:
            if spec.type == "export_specifier":
                alias = spec.child_by_field_name("alias") or spec.child_by_field_name("name")
                names.add(_text(alias))
    return names


def scan(
    source: str,
    file: str,
    internal_hosts: Iterable[str],
    base_url_names: Iterable[str] = (),
    root: Path | None = None,
) -> FileApi:
    api = FileApi(file=file)
    tree = tsparse.parse(source.encode("utf-8"), file)
    visitor = _Visitor(api, tuple(internal_hosts), tuple(base_url_names))
    visitor.collect_module(tree.root_node)
    visitor.visit(tree.root_node)
    _next_routes(api, file, root, _exported_names(tree.root_node))
    return api
