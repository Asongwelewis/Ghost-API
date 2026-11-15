"""Routes (FastAPI, Flask, Django/DRF) and HTTP calls (requests, httpx) in Python source."""

from __future__ import annotations

import ast
from collections.abc import Iterable

from ghostapi.endpoints import (
    DYNAMIC,
    HTTP_METHODS,
    DjangoPattern,
    FileApi,
    HttpCall,
    Mount,
    RawRoute,
    RouterObject,
    internal_path,
)
from ghostapi.extractors import quiet_parsing

_FACTORIES = {
    "FastAPI": ("app", "fastapi"),
    "APIRouter": ("router", "fastapi"),
    "Flask": ("app", "flask"),
    "Blueprint": ("router", "flask"),
}
_ROUTE_METHODS = {m.lower() for m in HTTP_METHODS}
_CLIENT_FACTORIES = {"Session", "Client", "AsyncClient"}
_HTTP_LIBRARIES = {"requests", "httpx"}
_DRF_ROUTERS = {"DefaultRouter", "SimpleRouter"}


def _name(node: ast.expr) -> str | None:
    """`FastAPI` for both `FastAPI(...)` and `fastapi.FastAPI(...)`."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _parts(node: ast.expr) -> tuple[str, ...] | None:
    """`orders.router` -> ("orders", "router")."""
    if isinstance(node, ast.Name):
        return (node.id,)
    if isinstance(node, ast.Attribute):
        base = _parts(node.value)
        return None if base is None else (*base, node.attr)
    return None


def _str(node: ast.expr | None) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _keyword(call: ast.Call, name: str) -> ast.expr | None:
    return next((k.value for k in call.keywords if k.arg == name), None)


def _methods(node: ast.expr | None) -> list[str] | None:
    if isinstance(node, ast.List | ast.Tuple | ast.Set):
        values = [_str(e) for e in node.elts]
        if all(values):
            return [v.upper() for v in values if v]
    return None


class _Visitor(ast.NodeVisitor):
    def __init__(self, api: FileApi, hosts: tuple[str, ...], base_names: tuple[str, ...]):
        self.api = api
        self.hosts = hosts
        self.base_names = base_names
        self.scope: list[str] = []
        self.constants: dict[str, str] = {}  # module-level string constants
        self.libraries: dict[str, str] = {}  # local alias -> "requests" | "httpx"
        self.functions: dict[str, tuple[str, str]] = {}  # `from requests import get`
        self.clients: dict[str, str | None] = {}  # client variable -> base URL ("" = none)

    # imports and module-level state

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            local = alias.asname or alias.name.split(".")[0]
            self.api.imports[local] = (alias.name if alias.asname else local, None, 0)
            if alias.name in _HTTP_LIBRARIES:
                self.libraries[local] = alias.name

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for alias in node.names:
            local = alias.asname or alias.name
            self.api.imports[local] = (node.module or "", alias.name, node.level)
            if node.module in _HTTP_LIBRARIES and not node.level:
                self.functions[local] = (node.module, alias.name)

    def visit_Assign(self, node: ast.Assign) -> None:
        self._assign(node.targets, node.value)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if node.value is not None:
            self._assign([node.target], node.value)
        self.generic_visit(node)

    def _assign(self, targets: list[ast.expr], value: ast.expr) -> None:
        names = [t.id for t in targets if isinstance(t, ast.Name)]
        if not self.scope and (text := self._string(value)) is not None and DYNAMIC not in text:
            self.constants.update(dict.fromkeys(names, text))
        if any(isinstance(t, ast.Name) and t.id == "ROOT_URLCONF" for t in targets):
            self.api.django_root = _str(value)
        if isinstance(value, ast.Call):
            for name in names:
                self._factory(name, value)
        if any(isinstance(t, ast.Name) and t.id == "urlpatterns" for t in targets):
            self._urlpatterns(value)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        if isinstance(node.target, ast.Name) and node.target.id == "urlpatterns":
            self._urlpatterns(node.value)
        self.generic_visit(node)

    def _factory(self, name: str, call: ast.Call) -> None:
        factory = _name(call.func)
        if factory in _FACTORIES:
            kind, framework = _FACTORIES[factory]
            prefix_node = _keyword(call, "prefix" if framework == "fastapi" else "url_prefix")
            self.api.objects[name] = RouterObject(kind, framework, _str(prefix_node) or "")
        elif factory in _DRF_ROUTERS:
            self.api.drf_routers.setdefault(name, [])
        elif factory in _CLIENT_FACTORIES and self._is_http_library(call.func):
            self.clients[name] = self._base_url(call)

    def _is_http_library(self, func: ast.expr) -> bool:
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            return func.value.id in self.libraries
        return isinstance(func, ast.Name) and func.id in self.functions

    def _base_url(self, call: ast.Call) -> str | None:
        node = _keyword(call, "base_url")
        if node is None:
            return ""
        return self._string(node)

    # scopes

    def _visit_scope(self, node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> None:
        if not isinstance(node, ast.ClassDef):
            for decorator in node.decorator_list:
                self._route_decorator(decorator, node.name, decorator.lineno)
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    visit_FunctionDef = visit_AsyncFunctionDef = visit_ClassDef = _visit_scope

    def visit_With(self, node: ast.With | ast.AsyncWith) -> None:
        for item in node.items:
            if isinstance(item.context_expr, ast.Call) and isinstance(item.optional_vars, ast.Name):
                self._factory(item.optional_vars.id, item.context_expr)
        self.generic_visit(node)

    visit_AsyncWith = visit_With

    # routes

    def _route_decorator(self, decorator: ast.expr, handler: str, line: int) -> None:
        if not isinstance(decorator, ast.Call) or not isinstance(decorator.func, ast.Attribute):
            return
        owner = decorator.func.value
        if not isinstance(owner, ast.Name) or owner.id not in self.api.objects:
            return
        obj = self.api.objects[owner.id]
        verb = decorator.func.attr
        path = _str(decorator.args[0]) if decorator.args else _str(_keyword(decorator, "path"))
        if path is None:
            return
        if verb in _ROUTE_METHODS:
            methods = [verb.upper()]
        elif verb in ("route", "api_route"):
            methods = _methods(_keyword(decorator, "methods")) or ["GET"]
        else:
            return
        symbol = ".".join([*self.scope, handler])
        for method in methods:
            self.api.routes.append(RawRoute(owner.id, method, path, line, symbol, obj.framework))

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            owner = func.value.id
            if owner in self.api.objects:
                self._object_call(owner, func.attr, node)
            elif owner in self.api.drf_routers and func.attr == "register" and node.args:
                prefix = _str(node.args[0])
                if prefix is not None:
                    self.api.drf_routers[owner].append((prefix, node.lineno))
            elif owner in self.clients or owner in self.libraries:
                self._http_call(node)
        elif isinstance(func, ast.Name) and func.id in self.functions:
            self._http_call(node)
        self.generic_visit(node)

    def _object_call(self, owner: str, verb: str, node: ast.Call) -> None:
        if verb in ("include_router", "register_blueprint", "mount") and node.args:
            child = _parts(node.args[-1] if verb == "mount" else node.args[0])
            key = "url_prefix" if verb == "register_blueprint" else "prefix"
            prefix = _str(node.args[0]) if verb == "mount" else _str(_keyword(node, key))
            if child is not None:
                self.api.mounts.append(Mount(owner, child, prefix, node.lineno))
        elif verb in ("add_api_route", "add_url_rule") and node.args:
            path = _str(node.args[0])
            methods = _methods(_keyword(node, "methods")) or ["GET"]
            if path is not None:
                framework = self.api.objects[owner].framework
                symbol = ".".join(self.scope) or None
                for method in methods:
                    self.api.routes.append(
                        RawRoute(owner, method, path, node.lineno, symbol, framework)
                    )

    # Django URL patterns

    def _urlpatterns(self, value: ast.expr) -> None:
        if isinstance(value, ast.Attribute) and value.attr == "urls":  # += router.urls
            if isinstance(value.value, ast.Name):
                self.api.django.append(DjangoPattern("", "router", value.value.id, value.lineno))
            return
        if not isinstance(value, ast.List | ast.Tuple):
            return
        for item in value.elts:
            if not isinstance(item, ast.Call) or _name(item.func) not in ("path", "re_path", "url"):
                continue
            route = _str(item.args[0]) if item.args else None
            if route is None or len(item.args) < 2:
                continue
            regex = _name(item.func) in ("re_path", "url")
            target = item.args[1]
            pattern = self._django_target(route, target, item.lineno, regex)
            if pattern is not None:
                self.api.django.append(pattern)

    def _django_target(
        self, route: str, target: ast.expr, line: int, regex: bool
    ) -> DjangoPattern | None:
        if isinstance(target, ast.Call) and _name(target.func) == "include" and target.args:
            arg = target.args[0]
            if isinstance(arg, ast.Tuple) and arg.elts:
                arg = arg.elts[0]
            if (module := _str(arg)) is not None:
                return DjangoPattern(route, "include", module, line, regex=regex)
            if (
                isinstance(arg, ast.Attribute)
                and arg.attr == "urls"
                and isinstance(arg.value, ast.Name)
            ):
                return DjangoPattern(route, "router", arg.value.id, line, regex=regex)
            return DjangoPattern(route, "include", "", line, regex=regex)  # unknown target
        symbol = ".".join(_parts(target) or ()) or None
        return DjangoPattern(route, "view", "", line, symbol, regex)

    # HTTP calls

    def _string(self, node: ast.expr) -> str | None:
        """Static value of a string expression; runtime parts become `{}`."""
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.JoinedStr):
            out = []
            for value in node.values:
                text = _str(value) if isinstance(value, ast.Constant) else None
                if text is None and isinstance(value, ast.FormattedValue):
                    inner = value.value
                    text = self.constants.get(inner.id) if isinstance(inner, ast.Name) else None
                    if text is None:
                        text = self._base_name(inner) or DYNAMIC
                out.append(text)
            return "".join(out)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left = self._string(node.left)
            if left is None:
                left = self._base_name(node.left)
            if left is None:
                return None  # an unknown start could be any host
            right = self._string(node.right)
            return left + (DYNAMIC if right is None else right)
        if isinstance(node, ast.Name):
            return self.constants.get(node.id) or self._base_name(node)
        return None

    def _base_name(self, node: ast.expr) -> str | None:
        """A configured base-URL expression (e.g. `API_URL`) stands for the API root."""
        return "" if ast.unparse(node) in self.base_names else None

    def _http_call(self, node: ast.Call) -> None:
        func = node.func
        base: str | None = ""
        if isinstance(func, ast.Name):
            library, verb = self.functions[func.id]
            if verb in _CLIENT_FACTORIES:
                return
        else:
            assert isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name)
            verb = func.attr
            if func.value.id in self.clients:
                base = self.clients[func.value.id]
                library = "client"
            elif func.value.id in self.libraries:
                library = self.libraries[func.value.id]
            else:
                return
        args = list(node.args)
        if verb == "request":
            method_node = args.pop(0) if args else _keyword(node, "method")
            method = (_str(method_node) or "").upper() or None
        elif verb in _ROUTE_METHODS:
            method = verb.upper()
        else:
            return
        url_node = args[0] if args else _keyword(node, "url")
        url = self._string(url_node) if url_node is not None else None
        if url is None or base is None:
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
                node.lineno,
                node.col_offset,
                ".".join(self.scope) or "<module>",
                library,
            )
        )


def scan(
    source: str,
    file: str,
    module: str | None,
    internal_hosts: Iterable[str],
    base_url_names: Iterable[str] = (),
) -> FileApi:
    api = FileApi(file=file, module=module)
    try:
        with quiet_parsing():
            tree = ast.parse(source, filename=file)
    except SyntaxError:
        return api
    _Visitor(api, tuple(internal_hosts), tuple(base_url_names)).visit(tree)
    return api
