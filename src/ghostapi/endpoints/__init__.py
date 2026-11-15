"""The HTTP endpoints a project serves, and the HTTP calls its code makes.

When several agents build one product, they create APIs for each other: one adds
`POST /api/orders`, another writes the frontend that calls it. Ghost reads both sides from the
code (route definitions and fetch/axios/requests/httpx calls) and checks that every call still
lands on a route that exists. Nothing here runs the code.

Paths are compared segment by segment. Route templates from every framework normalise to the
same form: literal segments, `{name}` for one dynamic segment, `**` for "the rest of the path".
Calls use `{}` for a segment whose value is only known at runtime.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

HTTP_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS")
ANY_METHOD = "*"
DYNAMIC = "{}"  # call segment known only at runtime
REST = "**"  # route segment matching the rest of the path

_CATCH_ALL = re.compile(r"^(\*+|\(\.\*\)|\[\[?\.\.\.\w+\]\]?|\{\w+:path\}|<path:\w+>|:\w+[*+])$")
_PARAM = re.compile(
    r"^(?::(?P<express>\w+)(?:\([^)]*\))?"  # Express, NestJS: :id, :id(\d+)
    r"|\{(?P<brace>\w+)(?::[^}]*)?\}"  # FastAPI, OpenAPI: {id}, {id:int}
    r"|<(?:\w+:)?(?P<angle>\w+)>"  # Flask, Django: <id>, <int:id>
    r"|\[(?P<next>\w+)\])$"  # Next.js: [id]
)
_OPTIONAL = re.compile(r"^:\w+\?$")


@dataclass(frozen=True)
class Endpoint:
    method: str  # GET, POST, ... or * when any method is accepted
    segments: tuple[str, ...]
    file: str
    line: int | None = None
    symbol: str | None = None  # handler
    framework: str = ""
    prefix_known: bool = True  # False: mounted somewhere we couldn't trace; match by suffix
    source: str = "code"  # code | openapi | declared
    owner: str | None = None

    @property
    def path(self) -> str:
        return "/" + "/".join(self.segments)

    @property
    def display(self) -> str:
        prefix = "" if self.prefix_known else "..."
        return f"{self.method} {prefix}{self.path}"

    def where(self) -> str:
        return f"{self.file}:{self.line}" if self.line else self.file


@dataclass(frozen=True)
class HttpCall:
    method: str | None  # None when the method is computed at runtime
    segments: tuple[str, ...]
    file: str
    line: int
    col: int = 0
    symbol: str = "<module>"
    client: str = ""  # fetch, axios, requests, httpx, ...

    @property
    def path(self) -> str:
        return "/" + "/".join(self.segments)

    @property
    def display(self) -> str:
        return f"{self.method or '?'} {self.path}"


@dataclass
class RawRoute:
    """A route as written: relative to the router/app object it hangs off."""

    owner: str  # local variable of the app/router
    method: str
    path: str
    line: int
    symbol: str | None
    framework: str


@dataclass
class RouterObject:
    kind: str  # app | router
    framework: str
    prefix: str = ""


@dataclass
class Mount:
    """`app.include_router(orders.router, prefix="/orders")` and friends."""

    parent: str  # local variable of the app/router that mounts
    child: tuple[str, ...]  # expression naming the mounted router, e.g. ("orders", "router")
    prefix: str | None  # None: the child keeps its own prefix
    line: int


@dataclass
class DjangoPattern:
    route: str
    kind: str  # view | include | router
    ref: str  # dotted module for include, router variable for router
    line: int
    symbol: str | None = None
    regex: bool = False


@dataclass
class FileApi:
    """Everything one source file contributes to the project's API picture."""

    file: str
    module: str | None = None  # dotted module (Python) or None
    objects: dict[str, RouterObject] = field(default_factory=dict)
    routes: list[RawRoute] = field(default_factory=list)
    mounts: list[Mount] = field(default_factory=list)
    # local name -> (module or specifier, imported name, relative-import level)
    imports: dict[str, tuple[str, str | None, int]] = field(default_factory=dict)
    exports: dict[str, str] = field(default_factory=dict)  # exported name -> local variable
    absolute: list[Endpoint] = field(default_factory=list)  # full path known (Next.js, NestJS)
    calls: list[HttpCall] = field(default_factory=list)
    django: list[DjangoPattern] = field(default_factory=list)
    drf_routers: dict[str, list[tuple[str, int]]] = field(default_factory=dict)
    django_root: str | None = None  # ROOT_URLCONF set in this file
    nest_prefix: str | None = None  # app.setGlobalPrefix(...) seen in this file
    nest_versioned: bool = False

    @property
    def defines_routes(self) -> bool:
        return bool(self.routes or self.absolute or self.django or self.drf_routers)


def route_segments(template: str) -> tuple[str, ...]:
    """Normalise a route template from any framework: `/orders/:id`, `/orders/{id}`,
    `/orders/<int:id>`, `app/orders/[id]` -> ("orders", "{id}")."""
    segments: list[str] = []
    for raw in template.split("?")[0].split("/"):
        part = raw.strip()
        if not part:
            continue
        if _CATCH_ALL.match(part) or _OPTIONAL.match(part):
            segments.append(REST)
            break
        match = _PARAM.match(part)
        if match:
            name = next(v for v in match.groupdict().values() if v)
            segments.append("{" + name + "}")
        elif any(c in part for c in ":{}<>[]*"):
            segments.append("{param}")  # partly dynamic, e.g. `file-:id.json`
        else:
            segments.append(part)
    return tuple(segments)


def call_segments(path: str) -> tuple[str, ...]:
    """Segments of a called path in which runtime values are written as `{}`."""
    path = path.split("#")[0].split("?")[0]
    return tuple(DYNAMIC if DYNAMIC in part else part for part in path.split("/") if part)


def _segment_matches(route: str, call: str) -> bool:
    return route.startswith("{") or call == DYNAMIC or route == call


def segments_match(route: tuple[str, ...], call: tuple[str, ...]) -> bool:
    for i, part in enumerate(route):
        if part == REST:
            return True
        if i >= len(call) or not _segment_matches(part, call[i]):
            return False
    return len(call) == len(route)


def endpoint_matches(endpoint: Endpoint, call: tuple[str, ...]) -> bool:
    if endpoint.prefix_known:
        return segments_match(endpoint.segments, call)
    return any(segments_match(endpoint.segments, call[k:]) for k in range(len(call) + 1))


def method_matches(endpoint: Endpoint, method: str | None) -> bool:
    if method is None or endpoint.method == ANY_METHOD or endpoint.method == method:
        return True
    return method == "HEAD" and endpoint.method == "GET"  # frameworks answer HEAD for GET routes


def join_paths(*parts: str) -> str:
    return "/" + "/".join(p.strip("/") for p in parts if p and p.strip("/"))


def internal_path(url: str, internal_hosts: tuple[str, ...]) -> tuple[str, ...] | None:
    """Segments of `url` when it points at this project's own API; None for anything else.

    Relative paths ("/api/orders") are ours; absolute URLs only when their host is internal.
    """
    if url.startswith(("http://", "https://")):
        authority, _, path = url.split("://", 1)[1].partition("/")
        host = authority.rsplit("@", 1)[-1]
        hostname = host.split("]")[0] + "]" if host.startswith("[") else host.split(":")[0]
        if hostname.lower() not in internal_hosts:
            return None
        return call_segments("/" + path)
    if url.startswith("/") and not url.startswith("//"):
        return call_segments(url)
    return None
