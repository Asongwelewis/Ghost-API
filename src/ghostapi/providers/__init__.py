"""Index providers: load the real API surface of a package, with its version history.

Providers never import or execute the package they describe.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Protocol

from ghostapi.models import ApiHistory, ApiKind
from ghostapi.resolver import normalize


class Provider(Protocol):
    def history(
        self, package: str, version: str | None, targets: Iterable[str]
    ) -> ApiHistory | None:
        """History of `package` as seen from `version`, covering at least `targets`.

        None when there is no source of truth (private package, offline, unknown version).
        """
        ...


class StaticProvider:
    """Serves histories held in memory. Used by tests and fixtures."""

    def __init__(self, histories: Iterable[ApiHistory]) -> None:
        self._by_name = {normalize(h.package): h for h in histories}

    def history(
        self, package: str, version: str | None, targets: Iterable[str]
    ) -> ApiHistory | None:
        return self._by_name.get(normalize(package))


def default_providers(offline: bool | None = None) -> dict[ApiKind, Provider]:
    from ghostapi.fetch import Http, cache_dir, offline_by_default
    from ghostapi.providers.npm import NpmProvider, NpmRegistry
    from ghostapi.providers.pypi import PyPIIndex, PyPIProvider

    offline = offline_by_default() if offline is None else offline
    cache = cache_dir()
    http = Http(cache, offline=offline)
    return {
        "python": PyPIProvider(PyPIIndex(http), cache),
        "npm": NpmProvider(NpmRegistry(http), cache),
    }


def history_for(
    providers: Mapping[ApiKind, Provider],
    kind: ApiKind,
    package: str,
    version: str | None,
    targets: Iterable[str],
    npm_pins: Mapping[str, str],
) -> ApiHistory | None:
    """History from the right provider. An npm package without bundled types is answered from
    its pinned `@types/*` package, as the TypeScript compiler would."""
    provider = providers.get(kind)
    if provider is None:
        return None
    targets = sorted(targets)
    history = provider.history(package, version, targets)
    if history is None and kind == "npm":
        from ghostapi.providers.npm import types_package

        typed = types_package(package)
        if typed in npm_pins:
            history = provider.history(typed, npm_pins[typed], targets)
    return history
