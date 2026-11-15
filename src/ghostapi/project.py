"""Project file discovery."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from ghostapi.extractors.javascript import is_source as is_js_source

EXCLUDED_DIRS = {
    ".git", ".ghost", ".venv", "venv", "env", "node_modules", "__pycache__", "build", "dist",
    ".tox", ".nox", ".mypy_cache", ".pytest_cache", ".ruff_cache", "site-packages",
    ".next", ".nuxt", ".svelte-kit", ".turbo", ".output", "coverage", "out",
}  # fmt: skip


def is_checkable(path: Path | str) -> bool:
    return str(path).endswith(".py") or is_js_source(path)


def source_files(directory: Path) -> list[Path]:
    """Python and JavaScript/TypeScript files Ghost can check, skipping build output."""
    found: list[Path] = []
    for current, dirs, files in os.walk(directory):
        dirs[:] = sorted(d for d in dirs if d not in EXCLUDED_DIRS and not d.endswith(".egg-info"))
        found.extend(Path(current) / f for f in sorted(files) if is_checkable(f))
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


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True)
    if result.returncode != 0:
        raise subprocess.CalledProcessError(result.returncode, result.args, stderr=result.stderr)
    return result.stdout


def _from_toplevel(root: Path, names: list[str]) -> list[Path]:
    """git prints paths relative to the repository root, which may be above `root`."""
    top = Path(_git(root, "rev-parse", "--show-toplevel").strip())
    return [top / name for name in names if name]


def staged_files(root: Path) -> list[Path]:
    out = _git(root, "diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z")
    return _from_toplevel(root, out.split("\0"))


def changed_files(root: Path) -> list[Path]:
    """Files git sees as modified, added or untracked; for agents with no edit hook."""
    entries = _git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all").split("\0")
    names: list[str] = []
    i = 0
    while i < len(entries):
        entry = entries[i]
        i += 1
        if len(entry) < 4:
            continue
        status, name = entry[:2], entry[3:]
        if status[0] in "RC":
            i += 1  # renames are followed by the old path
        if "D" not in status:
            names.append(name)
    return _from_toplevel(root, names)


def diff_files(root: Path, base: str) -> list[Path]:
    """Files added or modified since `base` (via the merge base, when git can find one)."""
    try:
        start = _git(root, "merge-base", base, "HEAD").strip()
    except subprocess.CalledProcessError:
        start = base  # shallow CI checkouts lack the history; diffing the trees still works
    out = _git(root, "diff", "--name-only", "--diff-filter=ACMR", "-z", start, "HEAD")
    return _from_toplevel(root, out.split("\0"))
