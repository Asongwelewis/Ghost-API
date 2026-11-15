"""The `.ghost/` registry: what the agents declare about the APIs a project uses and provides.

The registry says what the code *claims*. It is never treated as proof that an API exists; the
engine checks every entry against a real source of truth.

Layout (one small file per thing, so agents working in parallel, or in separate git worktrees,
rarely touch the same file):

    .ghost/apis/<api>/api.yaml               api, kind, version, module, source, owner
    .ghost/apis/<api>/calls/<target>.yaml    target and the places that call it
    .ghost/provides/<endpoint>.yaml          an endpoint this project serves: owner, notes

Every write happens under `.ghost/.lock` and replaces files atomically, so concurrent
`ghostapi register` runs neither lose updates nor leave half-written files.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, ValidationError

from ghostapi.fsutil import atomic_write_text, exclusive_lock
from ghostapi.models import ApiKind

GHOST_DIR = ".ghost"
LOCK_FILE = ".lock"


class RegistryError(Exception):
    pass


class Location(BaseModel):
    file: str  # project-relative, forward slashes
    symbol: str | None = None  # enclosing function/class qualname; survives line shifts
    line_hint: int | None = None


class Call(BaseModel):
    target: str  # "openai.OpenAI", "react:useState" or "POST /v1/charges"
    used_in: list[Location] = Field(default_factory=list)


class ApiEntry(BaseModel):
    api: str  # distribution / package / API name, e.g. "google-genai", "stripe-rest"
    kind: ApiKind = "python"
    version: str | None = None
    module: str | None = None  # Python import root when it differs from `api`
    source: str | None = None  # where the truth lives, e.g. "openapi:https://..."
    owner: str | None = None  # who to talk to about this API (agent, team or person)
    calls: list[Call] = Field(default_factory=list)

    @property
    def import_root(self) -> str:
        return self.module or self.api.replace("-", "_")


class ProvidedEndpoint(BaseModel):
    """An endpoint this project serves, as declared by the agent that owns it."""

    method: str  # GET, POST, ... or * for any
    path: str  # /api/orders/{id}
    owner: str | None = None
    description: str | None = None
    file: str | None = None  # where it is implemented, when the route scanner can't see it
    symbol: str | None = None


def _slug(value: str) -> str:
    """A file name for `value` that is readable, short and collision-free."""
    readable = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("_")[:60] or "x"
    return f"{readable}-{hashlib.sha1(value.encode()).hexdigest()[:8]}"


def apis_dir(root: Path) -> Path:
    return root / GHOST_DIR / "apis"


def provides_dir(root: Path) -> Path:
    return root / GHOST_DIR / "provides"


def _api_dir(root: Path, api: str) -> Path:
    return apis_dir(root) / re.sub(r"[^A-Za-z0-9._-]+", "_", api)


def entry_path(root: Path, api: str) -> Path:
    """The file that holds an API's metadata; findings about the entry point here."""
    return _api_dir(root, api) / "api.yaml"


def _legacy_path(root: Path, api: str) -> Path:
    return apis_dir(root) / (re.sub(r"[^A-Za-z0-9._-]+", "_", api) + ".yaml")


@contextmanager
def _locked(root: Path) -> Iterator[None]:
    try:
        with exclusive_lock(root / GHOST_DIR / LOCK_FILE):
            yield
    except TimeoutError as exc:
        raise RegistryError(f"another ghostapi process is holding the registry: {exc}") from exc


def _read_yaml(path: Path) -> dict | None:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None  # removed by another agent between listing and reading
    except yaml.YAMLError as exc:
        raise RegistryError(f"{path}: {exc}") from exc
    if data is None:
        return None
    if not isinstance(data, dict):
        raise RegistryError(f"{path}: expected a mapping")
    return data


def _write_yaml(path: Path, data: dict) -> None:
    atomic_write_text(path, yaml.safe_dump(data, sort_keys=False, allow_unicode=True))


def _validate[T: BaseModel](model: type[T], data: dict, path: Path) -> T:
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        raise RegistryError(f"{path}: {exc}") from exc


def _sorted_locations(locations: list[Location]) -> list[Location]:
    return sorted(locations, key=lambda loc: (loc.file, loc.symbol or "", loc.line_hint or 0))


def _load_dir(directory: Path) -> ApiEntry | None:
    meta_path = directory / "api.yaml"
    meta = _read_yaml(meta_path)
    if meta is None:
        return None
    entry = _validate(ApiEntry, {**meta, "calls": []}, meta_path)
    for call_path in sorted((directory / "calls").glob("*.yaml")):
        data = _read_yaml(call_path)
        if data is not None:
            entry.calls.append(_validate(Call, data, call_path))
    entry.calls.sort(key=lambda c: c.target)
    return entry


def _load_legacy(path: Path) -> ApiEntry | None:
    data = _read_yaml(path)
    return None if data is None else _validate(ApiEntry, data, path)


def load(root: Path) -> list[ApiEntry]:
    directory = apis_dir(root)
    if not directory.is_dir():
        return []
    entries: dict[str, ApiEntry] = {}
    for child in sorted(directory.iterdir()):
        entry = None
        if child.is_dir():
            entry = _load_dir(child)
        elif child.suffix == ".yaml":
            entry = _load_legacy(child)  # one-file-per-API layout from ghostapi 0.1
        if entry is not None:
            existing = entries.setdefault(entry.api, entry)
            if existing is not entry:  # both layouts present mid-migration
                known = {c.target for c in existing.calls}
                existing.calls.extend(c for c in entry.calls if c.target not in known)
    return list(entries.values())


def _load_one(root: Path, api: str) -> ApiEntry | None:
    entry = _load_dir(_api_dir(root, api))
    legacy = _legacy_path(root, api)
    if legacy.exists():
        old = _load_legacy(legacy)
        if old is not None and entry is None:
            entry = old
        elif old is not None and entry is not None:
            known = {c.target for c in entry.calls}
            entry.calls.extend(c for c in old.calls if c.target not in known)
    return entry


def _call_path(root: Path, api: str, target: str) -> Path:
    return _api_dir(root, api) / "calls" / f"{_slug(target)}.yaml"


def _save_meta(root: Path, entry: ApiEntry) -> None:
    data = entry.model_dump(exclude_none=True, exclude={"calls"})
    _write_yaml(entry_path(root, entry.api), data)


def _save_call(root: Path, api: str, call: Call) -> None:
    path = _call_path(root, api, call.target)
    if not call.used_in:
        path.unlink(missing_ok=True)
        return
    call.used_in = _sorted_locations(call.used_in)
    _write_yaml(path, call.model_dump(exclude_none=True))


def _migrate_legacy(root: Path, entry: ApiEntry) -> None:
    """Rewrite an 0.1 single-file entry into the per-target layout."""
    legacy = _legacy_path(root, entry.api)
    if not legacy.exists():
        return
    _save_meta(root, entry)
    for call in entry.calls:
        _save_call(root, entry.api, call)
    legacy.unlink()


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
    owner: str | None = None,
) -> ApiEntry:
    with _locked(root):
        entry = _load_one(root, api) or ApiEntry(api=api, kind=kind)
        if entry.kind != kind:
            raise RegistryError(f"{api} is registered as kind '{entry.kind}', not '{kind}'")
        _migrate_legacy(root, entry)
        for field_name, value in (
            ("version", version), ("module", module), ("source", source), ("owner", owner),
        ):  # fmt: skip
            if value is not None:
                setattr(entry, field_name, value)

        call = next((c for c in entry.calls if c.target == target), None)
        if call is None:
            call = Call(target=target)
            entry.calls.append(call)
        # One location per file+symbol, so re-registering refreshes the line hint.
        call.used_in = [
            loc
            for loc in call.used_in
            if (loc.file, loc.symbol) != (location.file, location.symbol)
        ]
        call.used_in.append(location)
        _save_meta(root, entry)
        _save_call(root, api, call)
        return entry


def unregister(root: Path, *, api: str, target: str | None = None, file: str | None = None) -> int:
    """Remove locations (or whole calls) from an entry. Returns how many locations were removed."""
    with _locked(root):
        entry = _load_one(root, api)
        if entry is None:
            raise RegistryError(f"{api} is not registered")
        _migrate_legacy(root, entry)
        removed = 0
        for call in entry.calls:
            if target is not None and call.target != target:
                continue
            keep = [loc for loc in call.used_in if file is not None and loc.file != file]
            removed += len(call.used_in) - len(keep)
            call.used_in = keep
            _save_call(root, api, call)
        if not any(c.used_in for c in entry.calls):
            for leftover in (_api_dir(root, api) / "calls").glob("*.yaml"):
                leftover.unlink(missing_ok=True)
            entry_path(root, api).unlink(missing_ok=True)
            for directory in (_api_dir(root, api) / "calls", _api_dir(root, api)):
                if directory.is_dir() and not any(directory.iterdir()):
                    directory.rmdir()
        return removed


# Endpoints this project provides


def endpoint_key(method: str, path: str) -> str:
    return f"{method.upper()} {path}"


def _provide_path(root: Path, method: str, path: str) -> Path:
    return provides_dir(root) / f"{_slug(endpoint_key(method, path))}.yaml"


def load_provided(root: Path) -> list[ProvidedEndpoint]:
    found = []
    for path in sorted(provides_dir(root).glob("*.yaml")):
        data = _read_yaml(path)
        if data is not None:
            found.append(_validate(ProvidedEndpoint, data, path))
    return found


def provide(root: Path, endpoint: ProvidedEndpoint) -> Path:
    endpoint.method = endpoint.method.upper()
    with _locked(root):
        path = _provide_path(root, endpoint.method, endpoint.path)
        existing = _read_yaml(path)
        merged = {**(existing or {}), **endpoint.model_dump(exclude_none=True)}
        _write_yaml(path, merged)
        return path


def unprovide(root: Path, method: str, path: str) -> bool:
    with _locked(root):
        target = _provide_path(root, method, path)
        if not target.exists():
            return False
        target.unlink()
        return True
