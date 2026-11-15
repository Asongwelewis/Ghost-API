"""Baselines: accept the findings a project already has, so only new ones fail (UC15).

A finding is identified by rule, file and target, not by line, so the baseline survives
ordinary edits. Counts matter: a second copy of a baselined problem in the same file is new.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from pathlib import Path

from ghostapi.fetch import read_json, write_json
from ghostapi.models import Finding

FORMAT = 1
DEFAULT_PATH = Path(".ghost") / "baseline.json"


class BaselineError(Exception):
    pass


def fingerprint(finding: Finding) -> str:
    return f"{finding.rule}|{finding.file}|{finding.target or finding.message}"


def write(path: Path, findings: Iterable[Finding]) -> int:
    counts = Counter(fingerprint(f) for f in findings)
    write_json(path, {"format": FORMAT, "findings": dict(sorted(counts.items()))})
    return sum(counts.values())


def load(path: Path) -> Counter[str]:
    data = read_json(path)
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        raise BaselineError(f"{path} is not a Ghost API baseline (regenerate: ghostapi baseline)")
    return Counter({str(k): int(v) for k, v in data.get("findings", {}).items()})


def apply(findings: Iterable[Finding], baseline: Counter[str]) -> tuple[list[Finding], int]:
    """(findings not covered by the baseline, how many were suppressed)."""
    remaining = baseline.copy()
    kept: list[Finding] = []
    suppressed = 0
    for finding in findings:
        key = fingerprint(finding)
        if remaining[key] > 0:
            remaining[key] -= 1
            suppressed += 1
        else:
            kept.append(finding)
    return kept, suppressed
