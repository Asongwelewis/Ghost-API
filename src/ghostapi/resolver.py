"""Resolve pinned versions from lockfiles. Read-only; never touches the network."""

from __future__ import annotations

import json
import re
import tomllib
from collections import defaultdict
from collections.abc import Callable, Iterable
from pathlib import Path

import yaml

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


def _pnpm_lock(path: Path) -> Iterable[tuple[str, str]]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    project = (data.get("importers") or {}).get(".") or data  # v6+ has importers; v5 doesn't
    for section in ("dependencies", "devDependencies", "optionalDependencies"):
        for name, info in (project.get(section) or {}).items():
            version = info.get("version") if isinstance(info, dict) else info
            if isinstance(version, str) and not version.startswith(
                ("link:", "file:", "workspace:")
            ):
                yield name, version.split("(")[0]  # "18.3.1(react@18.3.1)" -> peer suffix


def _yarn_lock(path: Path) -> Iterable[tuple[str, str]]:
    text = path.read_text(encoding="utf-8")
    found: dict[str, set[str]] = defaultdict(set)

    def names(header: str) -> list[str]:
        # '"@babel/core@^7.0.0", "@babel/core@^7.1.0"' or 'react@npm:^18.2.0' -> package names
        return [e.strip().strip('"').rpartition("@")[0] for e in header.split(",")]

    if "__metadata:" in text:  # yarn 2+ (berry) is YAML
        for key, info in (yaml.safe_load(text) or {}).items():
            if key != "__metadata" and isinstance(info, dict) and "version" in info:
                for name in names(key):
                    found[name].add(str(info["version"]))
    else:  # yarn 1
        current: list[str] = []
        for line in text.splitlines():
            if line and not line.startswith((" ", "#")):
                current = names(line.rstrip(":"))
            elif line.strip().startswith("version "):
                version = line.strip().split(" ", 1)[1].strip('"')
                for name in current:
                    found[name].add(version)
    for name, versions in found.items():
        if name and len(versions) == 1:  # several versions of one package: can't tell which
            yield name, versions.pop()


# Earlier files win: a real lockfile beats loose pins.
_SOURCES: list[tuple[str, str, Callable[[Path], Iterable[tuple[str, str]]]]] = [
    ("python", "uv.lock", _toml_packages),
    ("python", "poetry.lock", _toml_packages),
    ("python", "requirements.txt", _requirements_txt),
    ("python", "pyproject.toml", _pyproject),
    ("npm", "package-lock.json", _package_lock),
    ("npm", "pnpm-lock.yaml", _pnpm_lock),
    ("npm", "yarn.lock", _yarn_lock),
]


def resolve_versions(root: Path) -> Pins:
    pins: Pins = {"python": {}, "npm": {}}
    for ecosystem, filename, parse in _SOURCES:
        path = root / filename
        if not path.is_file():
            continue
        try:
            found = list(parse(path))
        except (ValueError, yaml.YAMLError, AttributeError, TypeError):
            continue  # unreadable lockfile: its packages become unverifiable, not errors
        for name, version in found:
            key = normalize(name) if ecosystem == "python" else name
            pins[ecosystem].setdefault(key, version)
    return pins


def npm_declared(root: Path) -> set[str]:
    """Packages package.json depends on, in any section."""
    try:
        data = json.loads((root / "package.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    sections = ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies")
    return {name for section in sections for name in (data.get(section) or {})}


def python_dist_for(target: str, python_pins: dict[str, str]) -> str | None:
    """Map a dotted import target to the pinned distribution that provides it, if any."""
    parts = target.split(".")
    for end in range(len(parts), 0, -1):  # longest prefix first: google.genai before google
        candidate = ".".join(parts[:end])
        dist = normalize(_IMPORT_TO_DIST.get(candidate, candidate))
        if dist in python_pins:
            return dist
    return None
