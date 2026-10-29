"""Render a report as text (humans), JSON (agents) or SARIF 2.1.0 (code scanning / CI)."""

from __future__ import annotations

import json
from collections import Counter

from ghostapi import __version__
from ghostapi.checker import Report
from ghostapi.models import Finding, Severity

_SARIF_LEVEL = {Severity.ERROR: "error", Severity.WARNING: "warning", Severity.INFO: "note"}


def _location(f: Finding) -> str:
    if f.line is None:
        return f.file
    return f"{f.file}:{f.line}:{(f.col or 0) + 1}"


def to_text(report: Report) -> str:
    lines = []
    for f in report.findings:
        lines.append(f"{_location(f)} {f.severity} {f.rule} [{f.confidence}] {f.message}")
        if f.fix:
            lines.append(f"    fix: {f.fix}")
        if f.source_url:
            lines.append(f"    docs: {f.source_url}")
    counts = Counter(f.severity for f in report.findings)
    summary = (
        f"{len(report.findings)} finding(s): {counts[Severity.ERROR]} error(s), "
        f"{counts[Severity.WARNING]} warning(s), {counts[Severity.INFO]} info "
        f"- {report.files_scanned} file(s) scanned"
    )
    if report.unverifiable:
        summary += f", {report.unverifiable} usage(s) unverifiable (no index for them yet)"
    lines.append(summary)
    return "\n".join(lines)


def to_json(report: Report) -> str:
    return json.dumps(
        {
            "findings": [f.to_dict() for f in report.findings],
            "unverifiable": report.unverifiable,
            "files_scanned": report.files_scanned,
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


RENDERERS = {"text": to_text, "json": to_json, "sarif": to_sarif}
