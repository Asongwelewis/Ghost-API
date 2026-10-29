"""Tie registry, code scan, lockfiles and providers together into one report."""

from __future__ import annotations

import sys
from collections import defaultdict
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from ghostapi import registry
from ghostapi.engine import check_usage
from ghostapi.extractors.python import FileScan, scan_file
from ghostapi.models import ApiHistory, ApiKind, Confidence, Finding, Severity, Usage
from ghostapi.project import local_modules, python_files, rel
from ghostapi.providers import Provider, default_providers
from ghostapi.reconcile import reconcile
from ghostapi.registry import ApiEntry
from ghostapi.resolver import Pins, normalize, python_dist_for, resolve_versions


@dataclass
class Report:
    findings: list[Finding]
    unverifiable: int  # usages with no source of truth to check against (yet)
    files_scanned: int


def run_check(
    root: Path,
    files: Sequence[Path] | None = None,
    providers: Mapping[ApiKind, Provider] | None = None,
    offline: bool | None = None,
) -> Report:
    """Check the whole project, or only `files` (hook / pre-commit mode)."""
    root = root.resolve()
    providers = default_providers(offline) if providers is None else providers
    entries = registry.load(root)
    pins = resolve_versions(root)
    skip = set(sys.stdlib_module_names) | local_modules(root)

    full = files is None
    candidates = python_files(root) if files is None else [f for f in files if f.suffix == ".py"]
    scans: list[FileScan] = []
    for path in candidates:
        relative = rel(root, path)
        if relative is not None and path.is_file():
            scans.append(scan_file(path, relative, skip))

    findings = reconcile(root, entries, scans, pins=pins, full=full)
    findings += [
        Finding(
            rule="unparseable-file",
            severity=Severity.INFO,
            confidence=Confidence.HIGH,
            message=f"Skipped, could not parse ({scan.error}).",
            file=scan.file,
        )
        for scan in scans
        if scan.error
    ]

    subjects = list(_subjects(entries, scans, pins, full))
    # One provider call per package@version, so each one is fetched and indexed once.
    wanted: dict[tuple[ApiKind, str, str | None], set[str]] = defaultdict(set)
    for kind, usage, package, version in subjects:
        if package is not None:
            wanted[(kind, package, version)].add(usage.target)
    histories: dict[tuple[ApiKind, str, str | None], ApiHistory | None] = {}
    for key, targets in wanted.items():
        provider = providers.get(key[0])
        histories[key] = provider.history(key[1], key[2], sorted(targets)) if provider else None

    unverifiable = 0
    for kind, usage, package, version in subjects:
        history = histories.get((kind, package, version)) if package is not None else None
        if history is None or package is None:
            unverifiable += 1
            continue
        findings += check_usage(usage, package=package, version=version, history=history)

    # Don't ask the agent to register a call it must fix first.
    broken = {(f.file, f.line) for f in findings if f.severity is Severity.ERROR}
    findings = [
        f
        for f in findings
        if not (f.rule == "unregistered-api-call" and (f.file, f.line) in broken)
    ]
    return Report(_dedupe(findings), unverifiable, len(scans))


def _subjects(
    entries: Sequence[ApiEntry], scans: Sequence[FileScan], pins: Pins, full: bool
) -> Iterator[tuple[ApiKind, Usage, str | None, str | None]]:
    """Everything to verify: scanned usages, plus registry declarations the scan can't see."""
    python_pins = pins["python"]
    scanned_targets = {(u.file, u.target) for s in scans for u in s.usages}
    in_scope = {s.file for s in scans}

    for scan in scans:
        for usage in scan.usages:
            package = python_dist_for(usage.target, python_pins)
            yield "python", usage, package, python_pins.get(package) if package else None

    for entry in entries:
        for call in entry.calls:
            for loc in call.used_in:
                if (not full and loc.file not in in_scope) or (
                    loc.file,
                    call.target,
                ) in scanned_targets:
                    continue
                usage = Usage(
                    target=call.target,
                    module=entry.import_root.split(".")[0],
                    file=loc.file,
                    line=loc.line_hint,
                    symbol=loc.symbol or "<module>",
                )
                if entry.kind == "python":
                    package = python_dist_for(call.target, python_pins) or normalize(entry.api)
                    version = python_pins.get(package, entry.version)
                elif entry.kind == "npm":
                    package, version = entry.api, pins["npm"].get(entry.api, entry.version)
                else:
                    package, version = entry.api, entry.version
                yield entry.kind, usage, package, version


def _dedupe(findings: list[Finding]) -> list[Finding]:
    seen: set[tuple[object, ...]] = set()
    unique = []
    for f in findings:
        key = (f.rule, f.file, f.line, f.target, f.message)
        if key not in seen:
            seen.add(key)
            unique.append(f)
    return sorted(unique, key=lambda f: (f.file, f.line or 0, f.col or 0, f.rule))
