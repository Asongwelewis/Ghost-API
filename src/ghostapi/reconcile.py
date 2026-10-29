"""Keep the registry honest: compare what it claims with what the code and lockfile say."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from pathlib import Path

from ghostapi import registry
from ghostapi.extractors.python import FileScan
from ghostapi.models import Confidence, Finding, Severity
from ghostapi.registry import ApiEntry, Location
from ghostapi.resolver import Pins, normalize, python_dist_for


def _related(a: str, b: str) -> bool:
    """True when one dotted path is the other or contains it (`openai` covers `openai.OpenAI`)."""
    return a == b or a.startswith(b + ".") or b.startswith(a + ".")


def reconcile(
    root: Path, entries: Sequence[ApiEntry], scans: Sequence[FileScan], *, pins: Pins, full: bool
) -> list[Finding]:
    """`full` means the whole project was scanned; otherwise only judge the scanned files."""
    return (
        _version_mismatches(root, entries, pins)
        + _stale_entries(root, entries, scans, full)
        + _unregistered(entries, scans, pins)
    )


def _registry_file(root: Path, entry: ApiEntry) -> str:
    return registry.entry_path(root, entry.api).relative_to(root).as_posix()


def _version_mismatches(root: Path, entries: Sequence[ApiEntry], pins: Pins) -> list[Finding]:
    findings = []
    for entry in entries:
        if entry.kind not in ("python", "npm") or entry.version is None:
            continue
        key = normalize(entry.api) if entry.kind == "python" else entry.api
        pinned = pins[entry.kind].get(key)
        if pinned is not None and pinned != entry.version:
            findings.append(
                Finding(
                    rule="registry-version-mismatch",
                    severity=Severity.WARNING,
                    confidence=Confidence.HIGH,
                    message=f"Registry says {entry.api} {entry.version}, lockfile pins {pinned}.",
                    file=_registry_file(root, entry),
                    api=entry.api,
                    version=pinned,
                    fix=f"Set `version: {pinned}` (the lockfile is the source of truth).",
                )
            )
    return findings


def _stale_reason(root: Path, entry: ApiEntry, loc: Location, scan: FileScan | None) -> str | None:
    if not (root / loc.file).is_file():
        return "that file no longer exists"
    if scan is None or scan.error is not None or entry.kind != "python":
        return None
    if loc.symbol and loc.symbol != "<module>" and loc.symbol not in scan.defined:
        return f"`{loc.symbol}` is not defined in that file"
    import_root = entry.import_root.split(".")[0]
    if import_root not in scan.imported_roots:
        return f"that file does not import `{import_root}`"
    return None


def _stale_entries(
    root: Path, entries: Sequence[ApiEntry], scans: Sequence[FileScan], full: bool
) -> list[Finding]:
    by_file = {s.file: s for s in scans}
    findings = []
    for entry in entries:
        for call in entry.calls:
            for loc in call.used_in:
                if not full and loc.file not in by_file:
                    continue
                reason = _stale_reason(root, entry, loc, by_file.get(loc.file))
                if reason is None:
                    continue
                where = f"{loc.file} ({loc.symbol})" if loc.symbol else loc.file
                findings.append(
                    Finding(
                        rule="stale-entry",
                        severity=Severity.WARNING,
                        confidence=Confidence.HIGH,
                        message=f"Registry says `{call.target}` is used in {where}, but {reason}.",
                        file=_registry_file(root, entry),
                        api=entry.api,
                        target=call.target,
                        fix=(
                            f"ghostapi unregister --api {entry.api} --target {call.target} "
                            f"--file {loc.file}  (or re-register the new location)"
                        ),
                    )
                )
    return findings


def _unregistered(
    entries: Sequence[ApiEntry], scans: Sequence[FileScan], pins: Pins
) -> list[Finding]:
    registered: dict[str, list[str]] = defaultdict(list)  # file -> targets
    for entry in entries:
        if entry.kind != "python":
            continue
        for call in entry.calls:
            for loc in call.used_in:
                registered[loc.file].append(call.target)

    findings = []
    for scan in scans:
        reported: set[str] = set()
        targets = {u.target for u in scan.usages}
        for usage in scan.usages:
            # `import openai` is implied by `openai.OpenAI(...)`; report only the deepest paths.
            if usage.target in reported or any(t.startswith(usage.target + ".") for t in targets):
                continue
            if any(_related(usage.target, t) for t in registered[scan.file]):
                continue
            reported.add(usage.target)
            api = python_dist_for(usage.target, pins["python"]) or usage.module
            command = f"ghostapi register --api {api} --target {usage.target} --file {scan.file}"
            if usage.symbol != "<module>":
                command += f" --symbol {usage.symbol}"
            if usage.line is not None:
                command += f" --line {usage.line}"
            findings.append(
                Finding(
                    rule="unregistered-api-call",
                    severity=Severity.WARNING,
                    confidence=Confidence.HIGH,
                    message=f"`{usage.target}` is used here but is not in the Ghost registry.",
                    file=scan.file,
                    line=usage.line,
                    col=usage.col,
                    api=api,
                    target=usage.target,
                    fix=command,
                )
            )
    return findings
