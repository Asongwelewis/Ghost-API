"""PyPI provider: API history of a Python distribution, built from its published files."""

from __future__ import annotations

import io
import tarfile
import tempfile
import zipfile
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Protocol

from ghostapi.fetch import Http
from ghostapi.providers.history import VersionedProvider
from ghostapi.providers.pysurface import build_surface
from ghostapi.providers.surface import Surface
from ghostapi.resolver import normalize

MAX_ARCHIVE_BYTES = 100 * 1024 * 1024
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


class PyPIProvider(VersionedProvider):
    ecosystem = "pypi"

    def __init__(self, index: PackageIndex, cache: Path | None = None) -> None:
        super().__init__(cache)
        self.index = index
        self._files: dict[str, dict[str, list[ReleaseFile]] | None] = {}

    def key(self, package: str) -> str:
        return normalize(package)

    def docs_url(self, package: str, version: str) -> str:
        return f"https://pypi.org/project/{package}/{version}/"

    def _release_files(self, package: str) -> dict[str, list[ReleaseFile]] | None:
        if package not in self._files:
            self._files[package] = self.index.releases(package)
        return self._files[package]

    def release_versions(self, package: str) -> dict[str, bool] | None:
        releases = self._release_files(package)
        if releases is None:
            return None
        return {v: any(not f.yanked for f in files) for v, files in releases.items()}

    def build(self, package: str, version: str) -> Surface | None:
        file = _pick_file((self._release_files(package) or {}).get(version, []))
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
