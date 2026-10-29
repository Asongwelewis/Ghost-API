"""The `.ghost/apis/` registry: the agent's declared list of external APIs and where each is used.

The registry says what the code *claims* to call. It is never treated as proof that the API
exists; the engine checks every entry against a real source of truth.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, ValidationError

from ghostapi.models import ApiKind

GHOST_DIR = ".ghost"


class RegistryError(Exception):
    pass


class Location(BaseModel):
    file: str  # project-relative, forward slashes
    symbol: str | None = None  # enclosing function/class qualname; survives line shifts
    line_hint: int | None = None


class Call(BaseModel):
    target: str  # "openai.OpenAI" or "POST /v1/charges"
    used_in: list[Location] = Field(default_factory=list)


class ApiEntry(BaseModel):
    api: str  # distribution / package / API name, e.g. "google-genai", "stripe-rest"
    kind: ApiKind = "python"
    version: str | None = None
    module: str | None = None  # Python import root when it differs from `api`
    source: str | None = None  # where the truth lives, e.g. "openapi:https://..."
    calls: list[Call] = Field(default_factory=list)

    @property
    def import_root(self) -> str:
        return self.module or self.api.replace("-", "_")


def apis_dir(root: Path) -> Path:
    return root / GHOST_DIR / "apis"


def entry_path(root: Path, api: str) -> Path:
    return apis_dir(root) / (re.sub(r"[^A-Za-z0-9._-]+", "_", api) + ".yaml")


def _load_file(path: Path) -> ApiEntry:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return ApiEntry.model_validate(data)
    except (yaml.YAMLError, ValidationError) as exc:
        raise RegistryError(f"{path}: {exc}") from exc


def load(root: Path) -> list[ApiEntry]:
    directory = apis_dir(root)
    if not directory.is_dir():
        return []
    return [_load_file(path) for path in sorted(directory.glob("*.yaml"))]


def save(root: Path, entry: ApiEntry) -> Path:
    path = entry_path(root, entry.api)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = entry.model_dump(exclude_none=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path


def register(
    root: Path,
    *,
    api: str,
    kind: ApiKind,
    target: str,
    location: Location,
    version: str | None = None,
    module: str | None = None,
    source: str | None = None,
) -> ApiEntry:
    path = entry_path(root, api)
    entry = _load_file(path) if path.exists() else ApiEntry(api=api, kind=kind)
    if entry.kind != kind:
        raise RegistryError(f"{api} is registered as kind '{entry.kind}', not '{kind}'")
    if version is not None:
        entry.version = version
    if module is not None:
        entry.module = module
    if source is not None:
        entry.source = source

    call = next((c for c in entry.calls if c.target == target), None)
    if call is None:
        call = Call(target=target)
        entry.calls.append(call)
    # One location per file+symbol, so re-registering refreshes the line hint.
    call.used_in = [
        loc for loc in call.used_in if (loc.file, loc.symbol) != (location.file, location.symbol)
    ]
    call.used_in.append(location)
    save(root, entry)
    return entry


def unregister(root: Path, *, api: str, target: str | None = None, file: str | None = None) -> int:
    """Remove locations (or whole calls) from an entry. Returns how many locations were removed."""
    path = entry_path(root, api)
    if not path.exists():
        raise RegistryError(f"{api} is not registered")
    entry = _load_file(path)

    removed = 0
    for call in entry.calls:
        if target is not None and call.target != target:
            continue
        keep = [loc for loc in call.used_in if file is not None and loc.file != file]
        removed += len(call.used_in) - len(keep)
        call.used_in = keep
    entry.calls = [c for c in entry.calls if c.used_in]

    if entry.calls:
        save(root, entry)
    else:
        path.unlink()
    return removed
