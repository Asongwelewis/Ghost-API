"""Optional project settings in `.ghost/config.yaml`.

http:
  internal_hosts: [localhost, api.example.com]   # absolute URLs on these hosts are ours
  base_url_names: [API_URL, process.env.NEXT_PUBLIC_API_URL]  # `${API_URL}/orders` is ours
  openapi: [docs/openapi.yaml]                   # specs that describe this project's API
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from ghostapi.registry import GHOST_DIR

DEFAULT_INTERNAL_HOSTS = ("localhost", "127.0.0.1", "0.0.0.0", "[::1]", "::1")


@dataclass(frozen=True)
class HttpConfig:
    internal_hosts: tuple[str, ...] = DEFAULT_INTERNAL_HOSTS
    base_url_names: tuple[str, ...] = ()
    openapi: tuple[str, ...] = ()


@dataclass(frozen=True)
class Config:
    http: HttpConfig = field(default_factory=HttpConfig)


def load_config(root: Path) -> Config:
    path = root / GHOST_DIR / "config.yaml"
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return Config()
    http = data.get("http") if isinstance(data, dict) else None
    if not isinstance(http, dict):
        return Config()

    def strings(key: str) -> tuple[str, ...]:
        value = http.get(key) or []
        return tuple(str(v) for v in value) if isinstance(value, list) else ()

    return Config(
        HttpConfig(
            internal_hosts=DEFAULT_INTERNAL_HOSTS + strings("internal_hosts"),
            base_url_names=strings("base_url_names"),
            openapi=strings("openapi"),
        )
    )
