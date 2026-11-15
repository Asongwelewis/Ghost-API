"""npm provider: API history of an npm package, built from the declarations it publishes.

Packages without bundled types are answered from DefinitelyTyped (`@types/x`) when the project
pins it; the checker handles that fallback.
"""

from __future__ import annotations

import io
import tarfile
import tempfile
from pathlib import Path, PurePosixPath
from typing import Protocol
from urllib.parse import quote

from ghostapi.fetch import Http
from ghostapi.providers.dts import DTS_SUFFIXES, build_dts_surface
from ghostapi.providers.history import VersionedProvider
from ghostapi.providers.surface import Surface

MAX_TARBALL_BYTES = 100 * 1024 * 1024
MAX_UNPACKED_BYTES = 300 * 1024 * 1024
RELEASES_MAX_AGE = 24 * 3600
# The abbreviated "corgi" document: versions and tarball URLs, a fraction of the full size.
_ABBREVIATED = {"Accept": "application/vnd.npm.install-v1+json"}


def types_package(package: str) -> str:
    """DefinitelyTyped name: `lodash` -> `@types/lodash`, `@babel/core` -> `@types/babel__core`."""
    if package.startswith("@"):
        scope, _, name = package[1:].partition("/")
        return f"@types/{scope}__{name}"
    return f"@types/{package}"


class NpmIndex(Protocol):
    def tarballs(self, package: str) -> dict[str, str] | None:
        """Version -> tarball URL, or None when the package isn't published."""
        ...

    def download(self, url: str) -> bytes | None: ...


class NpmRegistry:
    def __init__(self, http: Http, base_url: str = "https://registry.npmjs.org") -> None:
        self.http = http
        self.base_url = base_url.rstrip("/")

    def tarballs(self, package: str) -> dict[str, str] | None:
        data = self.http.get_json(
            f"{self.base_url}/{quote(package, safe='@')}",
            f"npm/{package}/versions.json",
            RELEASES_MAX_AGE,
            headers=_ABBREVIATED,
            transform=_tarball_map,
        )
        return data if isinstance(data, dict) else None

    def download(self, url: str) -> bytes | None:
        return self.http.get_bytes(url, MAX_TARBALL_BYTES)


class NpmProvider(VersionedProvider):
    ecosystem = "npm"

    def __init__(self, index: NpmIndex, cache: Path | None = None) -> None:
        super().__init__(cache)
        self.index = index
        self._tarballs: dict[str, dict[str, str] | None] = {}

    def docs_url(self, package: str, version: str) -> str:
        return f"https://www.npmjs.com/package/{package}/v/{version}"

    def _versions_of(self, package: str) -> dict[str, str] | None:
        if package not in self._tarballs:
            self._tarballs[package] = self.index.tarballs(package)
        return self._tarballs[package]

    def release_versions(self, package: str) -> dict[str, bool] | None:
        tarballs = self._versions_of(package)
        return None if tarballs is None else dict.fromkeys(tarballs, True)

    def build(self, package: str, version: str) -> Surface | None:
        url = (self._versions_of(package) or {}).get(version)
        data = self.index.download(url) if url else None
        if data is None:
            return None
        with tempfile.TemporaryDirectory(prefix="ghostapi-") as tmp:
            try:
                extract_declarations(data, Path(tmp))
            except (tarfile.TarError, OSError, EOFError):
                return None
            return build_dts_surface(Path(tmp), package)


def _tarball_map(document: dict) -> dict[str, str]:
    return {
        version: info["dist"]["tarball"]
        for version, info in document["versions"].items()
        if "tarball" in info.get("dist", {})
    }


def extract_declarations(data: bytes, dest: Path) -> None:
    """Write the tarball's package.json and declaration files under `dest`.

    Links, devices and paths that could escape `dest` are skipped; so is any bundled
    node_modules, which belongs to other packages.
    """
    total = 0
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        for member in archive.getmembers():
            if not member.isfile():
                continue
            parts = list(PurePosixPath(member.name.replace("\\", "/")).parts)[1:]  # "package/"
            if (
                not parts
                or any(p in ("", ".", "..") or ":" in p for p in parts)
                or "node_modules" in parts
            ):
                continue
            name = "/".join(parts)
            if not (name == "package.json" or name.endswith(DTS_SUFFIXES)):
                continue
            total += member.size
            if total > MAX_UNPACKED_BYTES:
                raise OSError("declarations too large")
            handle = archive.extractfile(member)
            if handle is None:
                continue
            target = dest.joinpath(*parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(handle.read())
