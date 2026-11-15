"""The single network layer: HTTP with an on-disk cache. Every failure degrades to None."""

from __future__ import annotations

import json
import os
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from ghostapi import __version__
from ghostapi.fsutil import atomic_write_text

USER_AGENT = f"ghostapi/{__version__} (+https://github.com/Asongwelewis/Ghost-API)"


def cache_dir() -> Path:
    if override := os.environ.get("GHOSTAPI_CACHE_DIR"):
        return Path(override)
    if sys.platform == "win32" and (local := os.environ.get("LOCALAPPDATA")):
        return Path(local) / "ghostapi" / "Cache"
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "ghostapi"


def offline_by_default() -> bool:
    return os.environ.get("GHOSTAPI_OFFLINE", "") not in ("", "0", "false")


class Http:
    def __init__(self, cache: Path, *, offline: bool = False, timeout: float = 30.0) -> None:
        self.cache = cache
        self.offline = offline
        self._timeout = timeout
        self._client: httpx.Client | None = None

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                timeout=self._timeout, follow_redirects=True, headers={"User-Agent": USER_AGENT}
            )
        return self._client

    def get_json(
        self,
        url: str,
        cache_key: str,
        max_age: float,
        headers: dict[str, str] | None = None,
        transform: Callable[[Any], Any] | None = None,
    ) -> Any | None:
        """JSON from `url`, cached under `cache_key`. Stale cache is used offline or on failure.

        `transform` reduces the response before it is cached and returned.
        """
        path = self.cache / cache_key
        cached = read_json(path)
        if cached is not None and (self.offline or time.time() - cached["fetched_at"] < max_age):
            return cached["data"]
        if self.offline:
            return None
        try:
            response = self.client.get(url, headers=headers)
            if response.status_code == 404:
                return None
            response.raise_for_status()
            data = response.json()
            if transform is not None:
                data = transform(data)
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            return cached["data"] if cached is not None else None
        write_json(path, {"fetched_at": time.time(), "data": data})
        return data

    def get_bytes(self, url: str, max_bytes: int) -> bytes | None:
        if self.offline:
            return None
        try:
            with self.client.stream("GET", url) as response:
                response.raise_for_status()
                chunks, size = [], 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > max_bytes:
                        return None
                    chunks.append(chunk)
                return b"".join(chunks)
        except httpx.HTTPError:
            return None


def read_json(path: Path) -> Any | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def write_json(path: Path, data: Any) -> None:
    """Atomic write, so a crash or a parallel run never leaves a half-written cache file."""
    atomic_write_text(path, json.dumps(data, separators=(",", ":")))
