"""`ghostapi list` and `ghostapi show`: let an agent look up the current contract of an API
before it writes a call, instead of trusting what it remembers (another agent may have changed
it a minute ago)."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ghostapi import registry
from ghostapi.config import load_config
from ghostapi.endpoints import (
    ANY_METHOD,
    Endpoint,
    HttpCall,
    call_segments,
    endpoint_matches,
    method_matches,
)
from ghostapi.endpoints.check import project_index, shape
from ghostapi.engine import check_usage
from ghostapi.extractors.javascript import package_of
from ghostapi.models import ApiKind, Param, Usage
from ghostapi.paths import display
from ghostapi.project import rel, source_files
from ghostapi.providers import Provider, default_providers, history_for
from ghostapi.resolver import python_dist_for, resolve_versions


@dataclass
class EndpointInfo:
    endpoint: Endpoint
    callers: list[HttpCall] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        e = self.endpoint
        return {
            "method": e.method,
            "path": e.path,
            "prefix_known": e.prefix_known,
            "framework": e.framework,
            "source": e.source,
            "defined_at": e.where(),
            "handler": e.symbol,
            "owner": e.owner,
            "callers": [f"{c.file}:{c.line}" for c in self.callers],
        }


def _files(root: Path) -> list[str]:
    return [r for p in source_files(root) if (r := rel(root, p)) is not None]


def endpoints(root: Path) -> list[EndpointInfo]:
    index = project_index(root, _files(root), load_config(root))
    in_code = {(e.method, shape(e)) for e in index.endpoints if e.source == "code"}
    infos = []
    for e in sorted(index.endpoints, key=lambda e: (e.path, e.method, e.file)):
        if e.source == "declared" and (e.method, shape(e)) in in_code:
            continue  # only annotates a route the scanner found (its owner is copied there)
        callers = [
            c
            for c in index.calls
            if endpoint_matches(e, c.segments) and method_matches(e, c.method)
        ]
        infos.append(EndpointInfo(e, callers))
    return infos


def inventory(root: Path) -> dict[str, Any]:
    entries = registry.load(root)
    return {
        "endpoints": [i.to_dict() for i in endpoints(root)],
        "apis": [
            {
                "api": e.api,
                "kind": e.kind,
                "version": e.version,
                "owner": e.owner,
                "calls": len(e.calls),
                "files": len({loc.file for c in e.calls for loc in c.used_in}),
            }
            for e in sorted(entries, key=lambda e: e.api)
        ],
    }


_SHELL_MANGLED = re.compile(r"^[A-Za-z]:[\/]")


class QueryError(ValueError):
    pass


def endpoint_path(path: str) -> str:
    """A route path as typed by a user or agent; accepts `api/orders` without the slash."""
    if _SHELL_MANGLED.match(path):
        raise QueryError(
            f"{path!r} looks like a file path: Git Bash on Windows rewrites arguments that start "
            "with '/'. Leave out the leading slash (api/orders) or set MSYS_NO_PATHCONV=1."
        )
    return path if path.startswith("/") else "/" + path


def is_endpoint_query(target: str) -> bool:
    """`POST /api/orders` or `api/orders/{id}`, as opposed to `openai.OpenAI` / `react:useState`."""
    path = target.split()[-1] if target.split() else target
    return (
        path.startswith("/")
        or ("/" in path and ":" not in path)
        or bool(_SHELL_MANGLED.match(path))
    )


def find_endpoints(root: Path, query: str) -> list[EndpointInfo]:
    """`POST /api/orders`, `/api/orders/{id}` or `/api/orders/42`."""
    method, _, path = query.strip().rpartition(" ")
    method = method.strip().upper() or None
    wanted = call_segments(endpoint_path(path))
    return [
        info
        for info in endpoints(root)
        if endpoint_matches(info.endpoint, wanted)
        and (method is None or info.endpoint.method in (method, ANY_METHOD))
    ]


def _signature(target: str, params: tuple[Param, ...] | None) -> str | None:
    if params is None:
        return None
    parts = []
    for p in params:
        prefix = {"var_positional": "*", "var_keyword": "**"}.get(p.kind, "")
        parts.append(f"{prefix}{p.name}{'' if p.required or prefix else '?'}")
    return f"{display(target)}({', '.join(parts)})"


def describe_symbol(
    root: Path, target: str, providers: Mapping[ApiKind, Provider] | None = None
) -> dict[str, Any]:
    """What the pinned version of a library really offers for `target`."""
    pins = resolve_versions(root)
    if ":" in target:
        kind: ApiKind = "npm"
        package = package_of(target.split(":", 1)[0])
        version = pins["npm"].get(package) if package else None
    else:
        kind = "python"
        package = python_dist_for(target, pins["python"])
        version = pins["python"].get(package) if package else None
    result: dict[str, Any] = {
        "target": target,
        "kind": kind,
        "package": package,
        "version": version,
    }
    usages = [
        f"{loc.file}:{loc.line_hint}" if loc.line_hint else loc.file
        for e in registry.load(root)
        for c in e.calls
        if c.target == target
        for loc in c.used_in
    ]
    result["registered_uses"] = usages
    providers = default_providers() if providers is None else providers
    history = (
        history_for(providers, kind, package, version, [target], pins["npm"]) if package else None
    )
    if history is None:
        result["status"] = "unverifiable"
        return result
    symbol = history.symbols.get(target)
    usage = Usage(target=target, module=package or "", file="<query>", line=None)
    problems = check_usage(usage, package=package or "", version=version, history=history)
    result["status"] = problems[0].rule if problems else ("ok" if symbol else "unknown")
    result["problems"] = [p.to_dict() for p in problems]
    if symbol is not None:
        result["symbol_kind"] = symbol.kind
        result["signature"] = _signature(target, symbol.params)
        result["package"], result["version"] = history.package, history.version or version
    return result


def text_inventory(data: dict[str, Any]) -> str:
    lines = [f"Endpoints this project serves ({len(data['endpoints'])}):"]
    for e in data["endpoints"]:
        path = e["path"] if e["prefix_known"] else f"...{e['path']}"
        owner = f"  owner: {e['owner']}" if e["owner"] else ""
        lines.append(
            f"  {e['method']:<7} {path:<40} {e['defined_at']}  callers: {len(e['callers'])}{owner}"
        )
    lines.append(f"External APIs in the registry ({len(data['apis'])}):")
    for a in data["apis"]:
        owner = f"  owner: {a['owner']}" if a["owner"] else ""
        version = f" {a['version']}" if a["version"] else ""
        lines.append(
            f"  {a['api']} ({a['kind']}{version}): {a['calls']} target(s) in "
            f"{a['files']} file(s){owner}"
        )
    return "\n".join(lines)


def text_endpoints(infos: list[EndpointInfo]) -> str:
    if not infos:
        return "No route in this project matches that."
    lines = []
    for info in infos:
        d = info.to_dict()
        lines.append(f"{d['method']} {d['path']}")
        lines.append(f"  defined at: {d['defined_at']} ({d['framework'] or d['source']})")
        if d["handler"]:
            lines.append(f"  handler:    {d['handler']}")
        if not d["prefix_known"]:
            lines.append("  mounted under a prefix Ghost could not trace")
        if d["owner"]:
            lines.append(f"  owner:      {d['owner']}")
        lines.append(f"  callers:    {', '.join(d['callers']) or 'none found'}")
    return "\n".join(lines)


def text_symbol(data: dict[str, Any]) -> str:
    pinned = f"{data['package']} {data['version']}" if data.get("version") else data["package"]
    lines = [f"{display(data['target'])} ({data['kind']}, {pinned or 'package not pinned'})"]
    lines.append(f"  status:    {data['status']}")
    if data.get("signature"):
        lines.append(f"  signature: {data['signature']}   (? = optional)")
    elif data.get("symbol_kind"):
        lines.append(f"  kind:      {data['symbol_kind']}")
    for problem in data.get("problems", []):
        lines.append(f"  problem:   {problem['message']}")
        if problem.get("fix"):
            lines.append(f"  fix:       {problem['fix']}")
    if data["registered_uses"]:
        lines.append(f"  used in:   {', '.join(data['registered_uses'])}")
    return "\n".join(lines)
