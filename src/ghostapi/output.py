"""Render a report as text (humans), JSON (agents), SARIF 2.1.0 (code scanning) or GitHub
Actions annotations (pull requests)."""

from __future__ import annotations

import json
from collections import Counter

from ghostapi import __version__
from ghostapi.checker import Report
from ghostapi.models import Finding, Severity

_SARIF_LEVEL = {Severity.ERROR: "error", Severity.WARNING: "warning", Severity.INFO: "note"}
_GITHUB_LEVEL = {Severity.ERROR: "error", Severity.WARNING: "warning", Severity.INFO: "notice"}


def _location(f: Finding) -> str:
    if f.line is None:
        return f.file
    return f"{f.file}:{f.line}:{(f.col or 0) + 1}"


def summary(report: Report) -> str:
    counts = Counter(f.severity for f in report.findings)
    line = (
        f"{len(report.findings)} finding(s): {counts[Severity.ERROR]} error(s), "
        f"{counts[Severity.WARNING]} warning(s), {counts[Severity.INFO]} info "
        f"- {report.files_scanned} file(s) scanned"
    )
    if report.baselined:
        line += f", {report.baselined} accepted by the baseline"
    if report.unverifiable:
        line += f", {report.unverifiable} usage(s) unverifiable (no index for them yet)"
    return line


def to_text(report: Report) -> str:
    lines = []
    for f in report.findings:
        lines.append(f"{_location(f)} {f.severity} {f.rule} [{f.confidence}] {f.message}")
        if f.fix:
            lines.append(f"    fix: {f.fix}")
        if f.source_url:
            lines.append(f"    docs: {f.source_url}")
    lines.append(summary(report))
    return "\n".join(lines)


def _escape_data(value: str) -> str:
    return value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _escape_property(value: str) -> str:
    return _escape_data(value).replace(":", "%3A").replace(",", "%2C")


def to_github(report: Report) -> str:
    """GitHub Actions workflow commands: each finding becomes an inline annotation."""
    lines = []
    for f in report.findings:
        props = [f"file={_escape_property(f.file)}"]
        if f.line is not None:
            props += [f"line={f.line}", f"col={(f.col or 0) + 1}"]
        props.append(f"title={_escape_property(f'Ghost API {f.rule}')}")
        message = f.message + (f"\nFix: {f.fix}" if f.fix else "")
        message += f"\nDocs: {f.source_url}" if f.source_url else ""
        lines.append(f"::{_GITHUB_LEVEL[f.severity]} {','.join(props)}::{_escape_data(message)}")
    lines.append(summary(report))
    return "\n".join(lines)


def to_json(report: Report) -> str:
    return json.dumps(
        {
            "findings": [f.to_dict() for f in report.findings],
            "unverifiable": report.unverifiable,
            "files_scanned": report.files_scanned,
            "baselined": report.baselined,
        },
        indent=2,
    )


def to_sarif(report: Report) -> str:
    results = []
    for f in report.findings:
        text = f.message + (f" Fix: {f.fix}" if f.fix else "")
        location: dict[str, object] = {"artifactLocation": {"uri": f.file}}
        if f.line is not None:
            location["region"] = {"startLine": f.line, "startColumn": (f.col or 0) + 1}
        results.append(
            {
                "ruleId": f.rule,
                "level": _SARIF_LEVEL[f.severity],
                "message": {"text": text},
                "locations": [{"physicalLocation": location}],
                "properties": {"confidence": str(f.confidence)},
            }
        )
    rules = [{"id": rule} for rule in sorted({f.rule for f in report.findings})]
    sarif = {
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "version": "2.1.0",
        "runs": [
            {
                "tool": {"driver": {"name": "ghostapi", "version": __version__, "rules": rules}},
                "results": results,
            }
        ],
    }
    return json.dumps(sarif, indent=2)


RENDERERS = {"text": to_text, "json": to_json, "sarif": to_sarif, "github": to_github}
