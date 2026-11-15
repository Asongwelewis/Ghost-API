"""Rules for the APIs a project builds for itself, which is where parallel agents collide.

- unknown-endpoint: a call that lands on no route the project serves
- wrong-method: the path exists but doesn't accept that method
- endpoint-removed: an edit removed or changed a route that other code still calls (compared
  with the last commit, so the agent that broke the callers is the one told)
- duplicate-endpoint: the same method + path defined twice (two agents built the same thing)
- similar-endpoint: near-identical paths, e.g. /user/{id} next to /users/{id}
"""

from __future__ import annotations

import subprocess
from collections import defaultdict
from collections.abc import Iterable
from difflib import get_close_matches
from pathlib import Path

from ghostapi.config import Config
from ghostapi.endpoints import (
    ANY_METHOD,
    Endpoint,
    HttpCall,
    endpoint_matches,
    method_matches,
)
from ghostapi.endpoints.index import ApiIndex, build_index, declared_endpoints, scan_file
from ghostapi.models import Confidence, Finding, Severity
from ghostapi.registry import load_provided


def _read_disk(root: Path):  # type: ignore[no-untyped-def]
    def read(rel: str) -> str | None:
        try:
            return (root / rel).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None

    return read


def read_head(root: Path, rel: str) -> str | None:
    """The file as of the last commit, or None (not in git, or new)."""
    result = subprocess.run(
        ["git", "show", f"HEAD:./{rel}"], cwd=root, capture_output=True, text=True, encoding="utf-8"
    )
    return result.stdout if result.returncode == 0 else None


def shape(endpoint: Endpoint) -> tuple[str, ...]:
    return tuple("{}" if s.startswith("{") else s for s in endpoint.segments)


def with_owners(endpoints: list[Endpoint]) -> list[Endpoint]:
    """Copy owners declared with `ghostapi provide` onto the code endpoints they describe."""
    owners = {
        (e.method, shape(e)): e.owner for e in endpoints if e.source == "declared" and e.owner
    }
    out = []
    for e in endpoints:
        owner = e.owner or owners.get((e.method, shape(e))) or owners.get((ANY_METHOD, shape(e)))
        out.append(e if owner == e.owner else _replace_owner(e, owner))
    return out


def _replace_owner(endpoint: Endpoint, owner: str | None) -> Endpoint:
    from dataclasses import replace

    return replace(endpoint, owner=owner)


def project_index(root: Path, files: Iterable[str], config: Config, read=None) -> ApiIndex:  # type: ignore[no-untyped-def]
    declared = declared_endpoints(load_provided(root))
    index = build_index(root, files, config, read or _read_disk(root), declared)
    index.endpoints = with_owners(index.endpoints)
    return index


def _finding(
    rule: str, severity: Severity, call: HttpCall, message: str, fix: str | None
) -> Finding:
    return Finding(
        rule=rule,
        severity=severity,
        confidence=Confidence.MEDIUM,  # static view of a runtime router
        message=message,
        file=call.file,
        line=call.line,
        col=call.col,
        api="http",
        target=call.display,
        fix=fix,
    )


def _judge(call: HttpCall, endpoints: list[Endpoint], first_segments: set[str]) -> list[Finding]:
    hits = [e for e in endpoints if endpoint_matches(e, call.segments)]
    if not hits:
        if not call.segments or call.segments[0] not in first_segments:
            return []  # not obviously aimed at this project's API (static file, page, ...)
        suggestion = _closest(call, [e for e in endpoints if e.prefix_known])
        fix = f"Did you mean `{suggestion.path}` ({suggestion.where()})?" if suggestion else None
        return [
            _finding(
                "unknown-endpoint",
                Severity.ERROR,
                call,
                f"`{call.display}` matches no route this project serves.",
                fix,
            )
        ]
    if any(method_matches(e, call.method) for e in hits):
        return []
    allowed = ", ".join(sorted({e.method for e in hits}))
    first = hits[0]
    return [
        _finding(
            "wrong-method",
            Severity.ERROR,
            call,
            f"`{call.path}` exists ({first.where()}) but accepts {allowed}, not {call.method}.",
            f"Use {allowed}, or add a {call.method} handler for this path.",
        )
    ]


def _closest(call: HttpCall, endpoints: list[Endpoint]) -> Endpoint | None:
    """The route the call most likely meant: compared by shape, same length first."""
    wanted = "/" + "/".join(call.segments)
    by_shape: dict[str, Endpoint] = {}
    for e in endpoints:  # per shape, prefer the route whose method the call uses
        key = "/" + "/".join(shape(e))
        if key not in by_shape or (
            method_matches(e, call.method) and not method_matches(by_shape[key], call.method)
        ):
            by_shape[key] = e
    same_length = [s for s, e in by_shape.items() if len(e.segments) == len(call.segments)]
    for candidates in (same_length, list(by_shape)):
        match = get_close_matches(wanted, candidates, n=1, cutoff=0.6)
        if match:
            return by_shape[match[0]]
    return None


def _first_segments(endpoints: list[Endpoint]) -> set[str]:
    return {e.segments[0] for e in endpoints if e.prefix_known and e.segments}


def _definition_findings(endpoints: list[Endpoint], scope: set[str] | None) -> list[Finding]:
    code = [e for e in endpoints if e.source == "code" and e.prefix_known]
    findings: list[Finding] = []
    by_shape: dict[tuple[str, tuple[str, ...]], list[Endpoint]] = defaultdict(list)
    for e in code:
        by_shape[(e.method, shape(e))].append(e)
    for group in by_shape.values():
        places = sorted({(e.file, e.line or 0) for e in group})
        if len(places) < 2:
            continue
        first = next(e for e in group if (e.file, e.line or 0) == places[0])
        for e in group:
            if (e.file, e.line or 0) == places[0] or (scope is not None and e.file not in scope):
                continue
            findings.append(
                Finding(
                    rule="duplicate-endpoint",
                    severity=Severity.WARNING,
                    confidence=Confidence.MEDIUM,
                    message=f"`{e.display}` is already defined at {first.where()}.",
                    file=e.file,
                    line=e.line,
                    api="http",
                    target=e.display,
                    fix="Reuse the existing endpoint, or remove one of the two definitions.",
                )
            )

    for a in code:
        if scope is not None and a.file not in scope:
            continue
        for b in code:
            if b is a or b.method != a.method or len(b.segments) != len(a.segments):
                continue
            diffs = [(x, y) for x, y in zip(a.segments, b.segments, strict=True) if x != y]
            if len(diffs) == 1 and _plural_pair(*diffs[0]) and (b.file, b.line) < (a.file, a.line):
                findings.append(
                    Finding(
                        rule="similar-endpoint",
                        severity=Severity.INFO,
                        confidence=Confidence.LOW,
                        message=f"`{a.display}` looks like `{b.display}` ({b.where()}).",
                        file=a.file,
                        line=a.line,
                        api="http",
                        target=a.display,
                        fix="If they are the same resource, keep one name.",
                    )
                )
    return findings


def _plural_pair(x: str, y: str) -> bool:
    if x.startswith("{") or y.startswith("{"):
        return False
    short, long = sorted((x, y), key=len)
    return long in (short + "s", short + "es") or (
        short.endswith("y") and long == short[:-1] + "ies"
    )


def check_endpoints(
    root: Path, all_files: list[str], subset: set[str] | None, config: Config
) -> list[Finding]:
    """`subset` is the set of files just edited (hook / pre-commit); None checks everything."""
    read = _read_disk(root)
    head_sources: dict[str, str | None] = {}
    if subset is not None:
        # Cheap pre-check: skip the whole-project scan when the edit can't affect HTTP APIs.
        relevant = False
        for rel in subset:
            current = read(rel)
            now = scan_file(rel, current, config, root) if current is not None else None
            head_sources[rel] = read_head(root, rel)
            before = (
                scan_file(rel, head_sources[rel] or "", config, root) if head_sources[rel] else None
            )
            if (now and (now.calls or now.defines_routes)) or (before and before.defines_routes):
                relevant = True
        if not relevant:
            return []

    index = project_index(root, all_files, config, read)
    if not index.endpoints:
        return []  # nothing to check calls against
    first_segments = _first_segments(index.endpoints)
    findings: list[Finding] = []
    for call in index.calls:
        if subset is None or call.file in subset:
            findings += _judge(call, index.endpoints, first_segments)
    findings += _definition_findings(index.endpoints, subset)

    if subset is not None:
        edited_providers = {
            rel
            for rel in subset
            if rel in index.provider_files
            or (
                head_sources.get(rel)
                and scan_file(rel, head_sources[rel] or "", config, root).defines_routes
            )
        }
        findings += _removed(root, all_files, config, index, edited_providers, head_sources, subset)
    return findings


def _removed(
    root: Path,
    all_files: list[str],
    config: Config,
    index: ApiIndex,
    edited: set[str],
    head_sources: dict[str, str | None],
    subset: set[str],
) -> list[Finding]:
    """Calls elsewhere that worked before the edit to `edited` and don't any more."""
    if not edited or not any(head_sources.get(rel) for rel in edited):
        return []
    disk = _read_disk(root)

    def read_before(rel: str) -> str | None:
        return head_sources[rel] if rel in edited else disk(rel)

    before = project_index(root, all_files, config, read_before)
    now_first = _first_segments(index.endpoints) | _first_segments(before.endpoints)
    findings = []
    for call in index.calls:
        if call.file in subset:
            continue  # already judged against the current routes
        if _judge(call, before.endpoints, now_first) or not _judge(
            call, index.endpoints, now_first
        ):
            continue
        lost = next(
            (
                e
                for e in before.endpoints
                if endpoint_matches(e, call.segments) and method_matches(e, call.method)
            ),
            None,
        )
        changed = lost.file if lost is not None else ", ".join(sorted(edited))
        owner = f" (owner: {lost.owner})" if lost is not None and lost.owner else ""
        route = f"`{lost.display}`" if lost is not None else "the route"
        findings.append(
            _finding(
                "endpoint-removed",
                Severity.ERROR,
                call,
                f"This call broke: the edit to {changed} removed or changed {route}{owner}, "
                f"and `{call.display}` no longer matches any route.",
                "Keep the old route (or an alias) until this caller is updated, "
                "or update this caller in the same change.",
            )
        )
    return findings
