"""PyPI provider: API history of a Python distribution, built from its published files.

Only the pinned version is indexed up front. Other versions are fetched only to explain a symbol
that is missing from the pinned version: was it removed (bisect older releases), is it too new
(bisect newer releases), or did it never exist?
"""

from __future__ import annotations

import io
import tarfile
import tempfile
import zipfile
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

from packaging.version import InvalidVersion, Version

from ghostapi.fetch import Http, read_json, write_json
from ghostapi.models import ApiHistory, ApiSymbol
from ghostapi.providers.surface import FORMAT, Surface, build_surface, to_symbol
from ghostapi.resolver import normalize

MAX_ARCHIVE_BYTES = 100 * 1024 * 1024
MAX_ANCHORS = 3  # older release series probed when looking for a removed symbol
RELEASES_MAX_AGE = 24 * 3600
_SDIST_SKIP = {"tests", "test", "testing", "docs", "doc", "examples", "benchmarks", "scripts"}


@dataclass(frozen=True)
class ReleaseFile:
    filename: str
    url: str
    packagetype: str  # bdist_wheel | sdist
    size: int = 0
    yanked: bool = False


class PackageIndex(Protocol):
    def releases(self, package: str) -> dict[str, list[ReleaseFile]] | None: ...
    def download(self, file: ReleaseFile) -> bytes | None: ...


class PyPIIndex:
    def __init__(self, http: Http, base_url: str = "https://pypi.org") -> None:
        self.http = http
        self.base_url = base_url.rstrip("/")

    def releases(self, package: str) -> dict[str, list[ReleaseFile]] | None:
        name = normalize(package)
        data = self.http.get_json(
            f"{self.base_url}/pypi/{name}/json",
            f"pypi/{name}/releases.json",
            RELEASES_MAX_AGE,
        )
        if not isinstance(data, dict) or "releases" not in data:
            return None
        return {
            version: [
                ReleaseFile(
                    f["filename"],
                    f["url"],
                    f["packagetype"],
                    f.get("size", 0),
                    f.get("yanked", False),
                )
                for f in files
            ]
            for version, files in data["releases"].items()
        }

    def download(self, file: ReleaseFile) -> bytes | None:
        return self.http.get_bytes(file.url, MAX_ARCHIVE_BYTES)


class PyPIProvider:
    def __init__(self, index: PackageIndex, cache: Path | None = None) -> None:
        self.index = index
        self.cache = cache
        self._surfaces: dict[tuple[str, str], Surface | None] = {}
        self._releases: dict[str, dict[str, list[ReleaseFile]] | None] = {}
        self._lifecycles: dict[tuple[str, str], dict[str, dict[str, Any] | None]] = {}

    def history(
        self, package: str, version: str | None, targets: Iterable[str]
    ) -> ApiHistory | None:
        if version is None:
            return None  # never judge against "latest"
        versions = self._versions(package, version)
        if versions is None:
            return None  # private or unknown to PyPI: unverifiable
        pinned = self.surface(package, version)
        if pinned is None:
            return None
        history = ApiHistory(
            package=normalize(package), docs_url=f"https://pypi.org/project/{package}/{version}/"
        )
        for target in targets:
            self._explain(target, package, version, versions, pinned, history)
        return history

    def _explain(
        self,
        target: str,
        package: str,
        version: str,
        versions: Sequence[str],
        pinned: Surface,
        history: ApiHistory,
    ) -> None:
        parts = target.split(".")
        chain = pinned.walk(target)
        if not chain:
            return  # the import root is not part of this distribution
        for i, node in enumerate(chain):
            path = ".".join(parts[: i + 1])
            history.symbols.setdefault(path, to_symbol(node, path, version))
        if len(chain) == len(parts):
            return

        parent, parent_path = chain[-1], ".".join(parts[: len(chain)])
        for name, canonical in parent.members.items():  # siblings, for "did you mean"
            path = f"{parent_path}.{name}"
            history.symbols.setdefault(path, to_symbol(pinned.node(canonical), path, version))

        missing = f"{parent_path}.{parts[len(chain)]}"
        if parent.kind in ("module", "class") and missing not in history.symbols:
            found = self._cached_lifecycle(package, version, versions, missing, pinned)
            if found is not None:
                history.symbols[missing] = found

    def _cached_lifecycle(
        self, package: str, version: str, versions: Sequence[str], path: str, pinned: Surface
    ) -> ApiSymbol | None:
        key = (normalize(package), version)
        file = self.cache / "pypi" / key[0] / f"{version}.lifecycle.json" if self.cache else None
        if key not in self._lifecycles:
            data = read_json(file) if file else None
            # A newer release can add the symbol, so answers are only valid for the same latest.
            fresh = (
                isinstance(data, dict)
                and data.get("format") == FORMAT
                and data.get("latest") == versions[-1]
            )
            self._lifecycles[key] = data["symbols"] if fresh and data else {}
        answers = self._lifecycles[key]
        if path in answers:
            known = answers[path]
            return ApiSymbol(**known) if known is not None else None
        try:
            found = self._lifecycle(package, version, versions, path, pinned)
        except _Unavailable:
            return None  # not cached, so the next run tries again
        answers[path] = asdict(found) if found else None
        if file:
            write_json(file, {"format": FORMAT, "latest": versions[-1], "symbols": answers})
        return found

    def _lifecycle(
        self, package: str, version: str, versions: Sequence[str], path: str, pinned: Surface
    ) -> ApiSymbol | None:
        """Explain a symbol missing from the pinned version, or None if it never existed."""
        pinned_v = Version(version)

        def present(v: str) -> bool:
            surface = self.surface(package, v)
            if surface is None:
                raise _Unavailable(v)
            return surface.has(path)

        later = [v for v in versions if Version(v) > pinned_v]
        if later and present(later[-1]):
            i = _first_true(later, present)
            return ApiSymbol(path, self._kind(package, later[i], path), added_in=later[i])

        earlier = [v for v in versions if Version(v) < pinned_v]
        for anchor in _anchors(earlier):
            if not present(anchor):
                continue
            span = [v for v in versions if Version(anchor) < Version(v) <= pinned_v]
            # The anchor ends a release series, and removals usually land in the next release.
            i = 0 if not present(span[0]) else 1 + _first_true(span[1:], lambda v: not present(v))
            kind = self._kind(package, anchor, path)
            moved = pinned.public_paths(path.rsplit(".", 1)[1], kind)
            return ApiSymbol(
                path, kind, removed_in=span[i], moved_to=moved[0] if len(moved) == 1 else None
            )
        return None

    def _kind(self, package: str, version: str, path: str) -> str:
        surface = self.surface(package, version)
        chain = surface.walk(path) if surface else []
        return chain[-1].kind if chain else "attribute"

    def _versions(self, package: str, pinned: str) -> list[str] | None:
        """Released versions in order, without pre-releases (unless pinned to one)."""
        if package not in self._releases:
            self._releases[package] = self.index.releases(package)
        releases = self._releases[package]
        if releases is None or pinned not in releases:
            return None
        pinned_parsed = _parse(pinned)
        if pinned_parsed is None:
            return None
        usable = []
        for v, files in releases.items():
            parsed = _parse(v)
            if parsed is None:
                continue
            if v == pinned or (
                any(not f.yanked for f in files)
                and (pinned_parsed.is_prerelease or not parsed.is_prerelease)
            ):
                usable.append((parsed, v))
        return [v for _, v in sorted(usable)]

    def surface(self, package: str, version: str) -> Surface | None:
        key = (normalize(package), version)
        if key in self._surfaces:
            return self._surfaces[key]
        path = self.cache / "pypi" / key[0] / f"{version}.surface.json" if self.cache else None
        surface = Surface.from_json(read_json(path) or {}) if path else None
        if surface is None:
            surface = self._build(package, version)
            if surface is not None and path is not None:
                write_json(path, surface.to_json())
        self._surfaces[key] = surface
        return surface

    def _build(self, package: str, version: str) -> Surface | None:
        if package not in self._releases:
            self._releases[package] = self.index.releases(package)
        files = (self._releases[package] or {}).get(version, [])
        file = _pick_file(files)
        if file is None:
            return None
        data = self.index.download(file)
        if data is None:
            return None
        with tempfile.TemporaryDirectory(prefix="ghostapi-") as tmp:
            try:
                tops, compiled = extract_sources(data, file.filename, Path(tmp))
            except (zipfile.BadZipFile, tarfile.TarError, OSError):
                return None
            if not tops:
                return None
            surface = build_surface(Path(tmp), tops, compiled)
        return surface if surface.roots else None


def _parse(version: str) -> Version | None:
    try:
        return Version(version)
    except InvalidVersion:
        return None


class _Unavailable(Exception):
    """A version needed to answer a question could not be fetched or indexed."""


def _first_true(seq: Sequence[str], pred: Callable[[str], bool]) -> int:
    """Index of the first True in a False..True sequence whose last item is True."""
    lo, hi = 0, len(seq) - 1
    while lo < hi:
        mid = (lo + hi) // 2
        if pred(seq[mid]):
            hi = mid
        else:
            lo = mid + 1
    return lo


def _anchors(earlier: Sequence[str]) -> list[str]:
    """Last release of each older series (major, or minor for 0.x), newest first."""
    last_of_series: dict[tuple[int, ...], str] = {}
    for v in earlier:
        parsed = Version(v)
        series = (0, parsed.minor) if parsed.major == 0 else (parsed.major,)
        last_of_series[series] = v  # `earlier` is sorted, so the last write wins
    return sorted(last_of_series.values(), key=Version, reverse=True)[:MAX_ANCHORS]


def _pick_file(files: Sequence[ReleaseFile]) -> ReleaseFile | None:
    def rank(f: ReleaseFile) -> int:
        if f.packagetype == "bdist_wheel":
            return 0 if f.filename.endswith("-none-any.whl") else 1
        if f.packagetype == "sdist" and f.filename.endswith((".tar.gz", ".zip")):
            return 2
        return 9

    usable = [f for f in files if not f.yanked and rank(f) < 9 and f.size <= MAX_ARCHIVE_BYTES]
    return min(usable, key=rank, default=None)


def _archive_members(data: bytes, filename: str) -> Iterator[tuple[str, Callable[[], bytes]]]:
    if filename.endswith((".whl", ".zip")):
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            for info in archive.infolist():
                if not info.is_dir():
                    yield info.filename, lambda info=info: archive.read(info)
    else:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as archive:
            for member in archive.getmembers():
                if member.isfile():  # never links or devices

                    def read(member: tarfile.TarInfo = member) -> bytes:
                        handle = archive.extractfile(member)
                        return handle.read() if handle else b""

                    yield member.name, read


def extract_sources(data: bytes, filename: str, dest: Path) -> tuple[list[str], list[str]]:
    """Write the archive's .py/.pyi files under `dest`.

    Returns (top-level module names, dotted names of compiled extension modules). Paths that
    could escape `dest` are skipped.
    """
    is_sdist = not filename.endswith(".whl")
    tops: set[str] = set()
    compiled: list[str] = []
    for name, read in _archive_members(data, filename):
        parts = list(PurePosixPath(name.replace("\\", "/")).parts)
        if is_sdist:
            parts = parts[1:]  # "<name>-<version>/" prefix
            if parts and parts[0] == "src":
                parts = parts[1:]
            if parts and parts[0] in _SDIST_SKIP:
                continue
        if (
            not parts
            or any(p in ("", ".", "..") or ":" in p for p in parts)
            or parts[0].startswith("/")
            or any(p.endswith((".dist-info", ".egg-info", ".data")) for p in parts[:-1])
        ):
            continue
        leaf = parts[-1]
        if leaf.endswith((".so", ".pyd")):
            compiled.append(".".join([*parts[:-1], leaf.split(".")[0]]))
            continue
        if not leaf.endswith((".py", ".pyi")) or (
            len(parts) == 1 and leaf in ("setup.py", "conftest.py")
        ):
            continue
        target = dest.joinpath(*parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(read())
        top = parts[0] if len(parts) > 1 else leaf.rsplit(".", 1)[0]
        if top.isidentifier():
            tops.add(top)
    return sorted(tops), [c for c in compiled if c.split(".")[0] in tops or "." not in c]
