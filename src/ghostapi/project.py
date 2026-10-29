"""Project file discovery."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

EXCLUDED_DIRS = {
    ".git", ".ghost", ".venv", "venv", "env", "node_modules", "__pycache__", "build", "dist",
    ".tox", ".nox", ".mypy_cache", ".pytest_cache", ".ruff_cache", "site-packages",
}  # fmt: skip


def python_files(directory: Path) -> list[Path]:
    found: list[Path] = []
    for current, dirs, files in os.walk(directory):
        dirs[:] = sorted(d for d in dirs if d not in EXCLUDED_DIRS and not d.endswith(".egg-info"))
        found.extend(Path(current) / f for f in sorted(files) if f.endswith(".py"))
    return found


def rel(root: Path, path: Path) -> str | None:
    """Project-relative POSIX path, or None when `path` is outside the project."""
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return None


def local_modules(root: Path) -> set[str]:
    """Top-level module names that belong to the project itself, not to a dependency."""
    names: set[str] = set()
    for base in (root, root / "src"):
        if not base.is_dir():
            continue
        for child in base.iterdir():
            if child.suffix == ".py":
                names.add(child.stem)
            elif child.is_dir() and child.name not in EXCLUDED_DIRS and any(child.glob("*.py")):
                names.add(child.name)
    return names


def staged_files(root: Path) -> list[Path]:
    result = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z"],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    )
    return [root / name for name in result.stdout.split("\0") if name]
