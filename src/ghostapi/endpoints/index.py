"""Assemble the project-wide picture: every endpoint it serves and every call it makes.

Routes are written relative to routers that other files mount (`app.include_router(...)`,
`app.use("/api", router)`, Django `include(...)`), so full paths need the whole project.
Where a mount can't be traced, the endpoint keeps `prefix_known=False` and matches by suffix
instead of being guessed.
"""

from __future__ import annotations

import posixpath
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path

import yaml

from ghostapi.config import Config
from ghostapi.endpoints import (
    ANY_METHOD,
    HTTP_METHODS,
    Endpoint,
    FileApi,
    HttpCall,
    join_paths,
    route_segments,
)
from ghostapi.endpoints import javascript as js_endpoints
from ghostapi.endpoints import python as py_endpoints
from ghostapi.extractors.javascript import is_source as is_js_source

_JS_EXTENSIONS = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".mts", ".cts")
_SPEC_NAMES = ("openapi.yaml", "openapi.yml", "openapi.json", "swagger.yaml", "swagger.json")
_SPEC_DIRS = ("", "docs", "api", "spec", "openapi")


@dataclass
class ApiIndex:
    endpoints: list[Endpoint] = field(default_factory=list)
    calls: list[HttpCall] = field(default_factory=list)
    provider_files: set[str] = field(default_factory=set)  # files that define routes


def python_module(rel: str) -> str | None:
    """Dotted module for a project file: `src/app/routers/orders.py` -> `app.routers.orders`."""
    if not rel.endswith(".py"):
        return None
    parts = rel[:-3].split("/")
    if parts[0] == "src" and len(parts) > 1:
        parts = parts[1:]
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts) if parts and all(p.isidentifier() for p in parts) else None


def scan_file(rel: str, source: str, config: Config, root: Path | None) -> FileApi:
    hosts, names = config.http.internal_hosts, config.http.base_url_names
    if rel.endswith(".py"):
        return py_endpoints.scan(source, rel, python_module(rel), hosts, names)
    return js_endpoints.scan(source, rel, hosts, names, root)


def build_index(
    root: Path,
    files: Iterable[str],
    config: Config,
    read: Callable[[str], str | None],
    declared: Iterable[Endpoint] = (),
) -> ApiIndex:
    """`files` are project-relative; `read` returns a file's source (or None to skip it)."""
    apis: list[FileApi] = []
    for rel in files:
        if not (rel.endswith(".py") or is_js_source(rel)):
            continue
        source = read(rel)
        if source is not None:
            apis.append(scan_file(rel, source, config, root))

    index = ApiIndex()
    index.provider_files = {a.file for a in apis if a.defines_routes}
    for api in apis:
        index.calls.extend(api.calls)
    index.endpoints.extend(_compose_routers([a for a in apis if a.file.endswith(".py")], _py_ref))
    index.endpoints.extend(
        _compose_routers([a for a in apis if not a.file.endswith(".py")], _js_ref)
    )
    index.endpoints.extend(_django(apis))
    index.endpoints.extend(_absolute(apis))
    index.endpoints.extend(openapi_endpoints(root, config))
    index.endpoints.extend(declared)
    return index


# Router composition (FastAPI, Flask, Express-style)

RouterId = tuple[str, str]  # (file, variable)


def _py_ref(
    api: FileApi, child: tuple[str, ...], by_module: Mapping[str, FileApi]
) -> RouterId | None:
    head, rest = child[0], child[1:]
    if head in api.objects and not rest:
        return (api.file, head)
    if head not in api.imports:
        return None
    module, name, level = api.imports[head]
    if level:
        base = (api.module or "").split(".")
        if not api.file.endswith("__init__.py"):
            base = base[:-1]
        base = base[: len(base) - (level - 1)] if level > 1 else base
        module = ".".join([*base, module] if module else base)
    if name is None:  # import pkg.routers.orders as orders; orders.router
        target = by_module.get(module)
        return (target.file, rest[0]) if target is not None and len(rest) == 1 else None
    if f"{module}.{name}" in by_module and len(rest) == 1:  # from app.routers import orders
        return (by_module[f"{module}.{name}"].file, rest[0])
    target = by_module.get(module)
    return (target.file, name) if target is not None and not rest else None


def _js_ref(
    api: FileApi, child: tuple[str, ...], by_module: Mapping[str, FileApi]
) -> RouterId | None:
    head = child[0]
    if head in api.objects and len(child) == 1:
        return (api.file, head)
    if head not in api.imports:
        return None
    specifier, name, _ = api.imports[head]
    if not specifier.startswith("."):
        return None
    base = posixpath.normpath(posixpath.join(posixpath.dirname(api.file), specifier))
    candidates = [base] + [base + ext for ext in _JS_EXTENSIONS]
    candidates += [f"{base}/index{ext}" for ext in _JS_EXTENSIONS]
    stem = posixpath.splitext(base)[0]
    candidates += [stem + ext for ext in _JS_EXTENSIONS]  # "./orders.js" written for orders.ts
    target = next((by_module[c] for c in candidates if c in by_module), None)
    if target is None:
        return None
    exported = "default" if name in ("default", "=", "*") else name or "default"
    if len(child) == 2 and name == "*":  # import * as orders; orders.router
        exported = child[1]
    local = target.exports.get(exported)
    if local is None and len(target.objects) == 1:
        local = next(iter(target.objects))
    return (target.file, local) if local in target.objects else None


def _compose_routers(
    apis: list[FileApi],
    resolve: Callable[[FileApi, tuple[str, ...], Mapping[str, FileApi]], RouterId | None],
) -> list[Endpoint]:
    by_file = {a.file: a for a in apis}
    by_module: dict[str, FileApi] = {}
    for api in apis:
        by_module[api.file] = api
        if api.module:
            by_module[api.module] = api

    # child router -> [(parent router, prefix given at the mount or None)]
    parents: dict[RouterId, list[tuple[RouterId, str | None]]] = {}
    for api in apis:
        for mount in api.mounts:
            child = resolve(api, mount.child, by_module)
            if child is not None and mount.parent in api.objects:
                parents.setdefault(child, []).append(((api.file, mount.parent), mount.prefix))

    def prefixes(router: RouterId, seen: frozenset[RouterId]) -> list[str] | None:
        """Every full prefix the router is served under; None when not traceable to an app."""
        obj = by_file[router[0]].objects[router[1]]
        if obj.kind == "app":
            return [obj.prefix]
        found: list[str] = []
        for parent, given in parents.get(router, []):
            if parent in seen:
                continue
            above = prefixes(parent, seen | {router})
            if above is None:
                continue
            own = obj.prefix if given is None or obj.framework == "fastapi" else ""
            # FastAPI adds the router's own prefix after include_router's; Flask's url_prefix
            # at registration replaces the blueprint's.
            mount_prefix = (
                (given or "") + own
                if obj.framework == "fastapi"
                else (given if given is not None else own)
            )
            found.extend(join_paths(p, mount_prefix) for p in above)
        return found or None

    endpoints: list[Endpoint] = []
    for api in apis:
        for route in api.routes:
            obj = api.objects[route.owner]
            full = prefixes((api.file, route.owner), frozenset())
            if full is None:  # mounted somewhere we can't see: keep its own prefix, match by suffix
                paths, known = [join_paths(obj.prefix, route.path)], False
            else:
                paths, known = [join_paths(p, route.path) for p in full], True
            for path in paths:
                endpoints.append(
                    Endpoint(
                        route.method,
                        route_segments(path),
                        api.file,
                        route.line,
                        route.symbol,
                        route.framework,
                        prefix_known=known,
                    )
                )
    return endpoints


# Django


def _regex_prefix(pattern: str) -> str:
    literal = re.split(r"[\\(\[.*+?${|]", pattern.lstrip("^"), maxsplit=1)[0]
    return literal + "/**"


def _django(apis: list[FileApi]) -> list[Endpoint]:
    modules = {a.module: a for a in apis if a.module and a.django}
    if not modules:
        return []
    roots = [a.django_root for a in apis if a.django_root and a.django_root in modules]
    if not roots:
        included = {p.ref for a in modules.values() for p in a.django if p.kind == "include"}
        roots = [m for m in modules if m not in included]
    endpoints: list[Endpoint] = []

    def walk(module: str, prefix: str, seen: frozenset[str]) -> None:
        api = modules[module]
        for pattern in api.django:
            route = _regex_prefix(pattern.route) if pattern.regex else pattern.route
            full = join_paths(prefix, route)
            if pattern.kind == "include":
                if pattern.ref in modules and pattern.ref not in seen:
                    walk(pattern.ref, full, seen | {pattern.ref})
                elif pattern.ref not in modules:  # third-party or unknown: anything below it
                    endpoints.append(
                        Endpoint(
                            ANY_METHOD,
                            route_segments(full + "/**"),
                            api.file,
                            pattern.line,
                            None,
                            "django",
                        )
                    )
            elif pattern.kind == "router":
                for registered, line in api.drf_routers.get(pattern.ref, []):
                    base = join_paths(full, registered)
                    for path in (base, base + "/**"):
                        endpoints.append(
                            Endpoint(
                                ANY_METHOD,
                                route_segments(path),
                                api.file,
                                line,
                                None,
                                "django-rest-framework",
                            )
                        )
            else:
                endpoints.append(
                    Endpoint(
                        ANY_METHOD,
                        route_segments(full),
                        api.file,
                        pattern.line,
                        pattern.symbol,
                        "django",
                    )
                )

    for root_module in roots:
        walk(root_module, "", frozenset({root_module}))
    return endpoints


# Next.js, NestJS


def _absolute(apis: list[FileApi]) -> list[Endpoint]:
    nest_prefix = next((a.nest_prefix for a in apis if a.nest_prefix), "") or ""
    versioned = any(a.nest_versioned for a in apis)
    endpoints: list[Endpoint] = []
    for api in apis:
        for endpoint in api.absolute:
            if endpoint.framework == "nestjs":
                path = join_paths(nest_prefix, "/".join(endpoint.segments))
                endpoint = replace(
                    endpoint, segments=route_segments(path), prefix_known=not versioned
                )
            endpoints.append(endpoint)
    return endpoints


# OpenAPI


def openapi_files(root: Path, config: Config) -> list[Path]:
    if config.http.openapi:
        return [root / p for p in config.http.openapi if (root / p).is_file()]
    return [root / d / n for d in _SPEC_DIRS for n in _SPEC_NAMES if (root / d / n).is_file()]


def openapi_endpoints(root: Path, config: Config) -> list[Endpoint]:
    endpoints: list[Endpoint] = []
    for path in openapi_files(root, config):
        try:
            text = path.read_text(encoding="utf-8")
            spec = yaml.safe_load(text)
        except (OSError, UnicodeDecodeError, yaml.YAMLError):
            continue
        if not isinstance(spec, dict) or not isinstance(spec.get("paths"), dict):
            continue
        base = ""
        servers = spec.get("servers")
        if isinstance(servers, list) and servers and isinstance(servers[0], dict):
            url = str(servers[0].get("url", ""))
            base = "/" + url.split("://", 1)[1].partition("/")[2] if "://" in url else url
        rel = path.relative_to(root).as_posix()
        lines = text.splitlines()
        for route, operations in spec["paths"].items():
            if not isinstance(operations, dict):
                continue
            line = next((i + 1 for i, s in enumerate(lines) if route in s), None)
            for method in operations:
                if method.upper() in HTTP_METHODS:
                    segments = route_segments(join_paths(base, str(route)))
                    endpoints.append(
                        Endpoint(
                            method.upper(), segments, rel, line, None, "openapi", source="openapi"
                        )
                    )
    return endpoints


def declared_endpoints(provided: Iterable) -> list[Endpoint]:
    """Endpoints agents declared with `ghostapi provide` (registry.ProvidedEndpoint)."""
    endpoints = []
    for item in provided:
        method = ANY_METHOD if item.method == "*" else item.method
        endpoints.append(
            Endpoint(
                method,
                route_segments(item.path),
                item.file or ".ghost/provides",
                None,
                item.symbol,
                "declared",
                source="declared",
                owner=item.owner,
            )
        )
    return endpoints
