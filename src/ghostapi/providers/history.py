"""Version history for providers backed by a package registry (PyPI, npm).

Only the pinned version is indexed up front. Other versions are fetched only to explain a symbol
that is missing from the pinned version: was it removed (bisect older releases), is it too new
(bisect newer releases), or did it never exist?
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable, Sequence
from dataclasses import asdict
from pathlib import Path
from typing import Any

from packaging.version import InvalidVersion, Version

from ghostapi.fetch import read_json, write_json
from ghostapi.models import ApiHistory, ApiSymbol
from ghostapi.paths import child, leaf, prefix, segments
from ghostapi.providers.surface import FORMAT, Surface, to_symbol

MAX_ANCHORS = 3  # older release series probed when looking for a removed symbol


class Unavailable(Exception):
    """A version needed to answer a question could not be fetched or indexed."""


class VersionedProvider(ABC):
    ecosystem: str  # cache folder, e.g. "pypi"

    def __init__(self, cache: Path | None = None) -> None:
        self.cache = cache
        self._surfaces: dict[tuple[str, str], Surface | None] = {}
        self._releases: dict[str, dict[str, bool] | None] = {}
        self._lifecycles: dict[tuple[str, str], dict[str, dict[str, Any] | None]] = {}

    @abstractmethod
    def release_versions(self, package: str) -> dict[str, bool] | None:
        """Every published version -> whether it is still usable (not yanked). None: unknown."""

    @abstractmethod
    def build(self, package: str, version: str) -> Surface | None:
        """Fetch and index one version, or None when it can't be done."""

    @abstractmethod
    def docs_url(self, package: str, version: str) -> str: ...

    def key(self, package: str) -> str:
        """Canonical package name, used for caching."""
        return package

    def history(
        self, package: str, version: str | None, targets: Iterable[str]
    ) -> ApiHistory | None:
        if version is None:
            return None  # never judge against "latest"
        versions = self._versions(package, version)
        if versions is None:
            return None  # private or unknown to the registry: unverifiable
        pinned = self.surface(package, version)
        if pinned is None:
            return None
        history = ApiHistory(
            package=self.key(package), docs_url=self.docs_url(package, version), version=version
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
        parts = segments(target)
        chain = pinned.walk(target)
        if not chain:
            return  # the import root is not part of this package
        for i, node in enumerate(chain):
            path = prefix(target, i + 1)
            history.symbols.setdefault(path, to_symbol(node, path, version))
        if len(chain) == len(parts):
            return

        parent, parent_path = chain[-1], prefix(target, len(chain))
        for name, canonical in parent.members.items():  # siblings, for "did you mean"
            path = child(parent_path, name)
            history.symbols.setdefault(path, to_symbol(pinned.node(canonical), path, version))

        missing = child(parent_path, parts[len(chain)])
        if parent.kind in ("module", "class") and missing not in history.symbols:
            found = self._cached_lifecycle(package, version, versions, missing, pinned)
            if found is not None:
                history.symbols[missing] = found

    def _cached_lifecycle(
        self, package: str, version: str, versions: Sequence[str], path: str, pinned: Surface
    ) -> ApiSymbol | None:
        key = (self.key(package), version)
        file = self._cache_file(package, f"{version}.lifecycle.json")
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
        except Unavailable:
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
                raise Unavailable(v)
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
            moved = pinned.public_paths(leaf(path), kind)
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
            self._releases[package] = self.release_versions(package)
        releases = self._releases[package]
        if releases is None or pinned not in releases:
            return None
        pinned_parsed = parse_version(pinned)
        if pinned_parsed is None:
            return None
        usable = []
        for v, ok in releases.items():
            parsed = parse_version(v)
            if parsed is None:
                continue
            if v == pinned or (ok and (pinned_parsed.is_prerelease or not parsed.is_prerelease)):
                usable.append((parsed, v))
        return [v for _, v in sorted(usable)]

    def surface(self, package: str, version: str) -> Surface | None:
        key = (self.key(package), version)
        if key in self._surfaces:
            return self._surfaces[key]
        path = self._cache_file(package, f"{version}.surface.json")
        surface = Surface.from_json(read_json(path) or {}) if path else None
        if surface is None:
            surface = self.build(package, version)
            if surface is not None and path is not None:
                write_json(path, surface.to_json())
        self._surfaces[key] = surface
        return surface

    def _cache_file(self, package: str, name: str) -> Path | None:
        if self.cache is None:
            return None
        return self.cache / self.ecosystem / self.key(package) / name


def parse_version(version: str) -> Version | None:
    try:
        return Version(version)
    except InvalidVersion:
        return None


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
