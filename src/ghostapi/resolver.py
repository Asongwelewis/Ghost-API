"""Resolve pinned versions from lockfiles. Read-only; never touches the network."""

from __future__ import annotations

import json
import re
import tomllib
from collections.abc import Callable, Iterable
from pathlib import Path

Pins = dict[str, dict[str, str]]  # ecosystem -> normalized package name -> version

_REQUIREMENT_PIN = re.compile(
    r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[^\]]*\])?\s*===?\s*([^\s;#,]+)"
)

# Import names that differ from their distribution name. Provisional: the pypi provider will
# replace this with top_level.txt / RECORD from the actual wheel.
_IMPORT_TO_DIST = {
    "PIL": "pillow",
    "bs4": "beautifulsoup4",
    "cv2": "opencv-python",
    "dateutil": "python-dateutil",
    "dotenv": "python-dotenv",
    "google.genai": "google-genai",
    "google.generativeai": "google-generativeai",
    "jwt": "pyjwt",
    "sklearn": "scikit-learn",
    "yaml": "pyyaml",
}


def normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _toml_packages(path: Path) -> Iterable[tuple[str, str]]:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    for package in data.get("package", []):
        if "name" in package and "version" in package:
            yield package["name"], package["version"]


def _requirement_lines(lines: Iterable[str]) -> Iterable[tuple[str, str]]:
    for line in lines:
        match = _REQUIREMENT_PIN.match(line)
        if match:
            yield match.group(1), match.group(2)


def _requirements_txt(path: Path) -> Iterable[tuple[str, str]]:
    return _requirement_lines(path.read_text(encoding="utf-8").splitlines())


def _pyproject(path: Path) -> Iterable[tuple[str, str]]:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    return _requirement_lines(data.get("project", {}).get("dependencies", []))


def _package_lock(path: Path) -> Iterable[tuple[str, str]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if "packages" in data:  # lockfileVersion 2/3
        for key, info in data["packages"].items():
            if key.count("node_modules/") == 1 and "version" in info:
                yield key.removeprefix("node_modules/"), info["version"]
    else:  # lockfileVersion 1
        for name, info in data.get("dependencies", {}).items():
            if "version" in info:
                yield name, info["version"]


# Earlier files win: a real lockfile beats loose pins.
_SOURCES: list[tuple[str, str, Callable[[Path], Iterable[tuple[str, str]]]]] = [
    ("python", "uv.lock", _toml_packages),
    ("python", "poetry.lock", _toml_packages),
    ("python", "requirements.txt", _requirements_txt),
    ("python", "pyproject.toml", _pyproject),
    ("npm", "package-lock.json", _package_lock),
]


def resolve_versions(root: Path) -> Pins:
    pins: Pins = {"python": {}, "npm": {}}
    for ecosystem, filename, parse in _SOURCES:
        path = root / filename
        if not path.is_file():
            continue
        for name, version in parse(path):
            key = normalize(name) if ecosystem == "python" else name
            pins[ecosystem].setdefault(key, version)
    return pins


def python_dist_for(target: str, python_pins: dict[str, str]) -> str | None:
    """Map a dotted import target to the pinned distribution that provides it, if any."""
    parts = target.split(".")
    for end in range(len(parts), 0, -1):  # longest prefix first: google.genai before google
        candidate = ".".join(parts[:end])
        dist = normalize(_IMPORT_TO_DIST.get(candidate, candidate))
        if dist in python_pins:
            return dist
    return None
