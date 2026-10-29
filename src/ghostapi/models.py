"""Shared data types: what the code uses, what the API really has, and what we report."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Literal

ApiKind = Literal["python", "npm", "openapi", "graphql"]
ParamKind = Literal[
    "positional_only", "positional_or_keyword", "var_positional", "keyword_only", "var_keyword"
]


class Severity(StrEnum):
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


class Confidence(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    UNVERIFIABLE = "unverifiable"


@dataclass(frozen=True)
class Usage:
    """One place the code touches an external API."""

    target: str  # dotted path ("openai.ChatCompletion.create") or "METHOD /path" for REST
    module: str  # top-level import root, e.g. "openai"
    file: str  # project-relative, forward slashes
    line: int | None
    col: int = 0
    symbol: str = "<module>"  # enclosing function/class qualname
    kwargs: tuple[str, ...] = ()
    n_positional: int | None = None  # None when the usage is not a call
    has_star_args: bool = False
    confidence: Confidence = Confidence.HIGH


@dataclass(frozen=True)
class Param:
    name: str
    kind: ParamKind = "positional_or_keyword"
    required: bool = False


@dataclass(frozen=True)
class ApiSymbol:
    path: str
    kind: str  # module | class | function | attribute | endpoint
    params: tuple[Param, ...] | None = None  # None = unknown signature
    added_in: str | None = None
    removed_in: str | None = None
    deprecated_in: str | None = None
    replacement: str | None = None
    moved_to: str | None = None
    dynamic: bool = False  # resolves unknown attributes at runtime (module/class __getattr__)
    source_url: str | None = None


@dataclass
class ApiHistory:
    """The API surface of one package as seen from a pinned version.

    `params` describe the pinned version; added/removed/deprecated span every known version,
    so symbols that no longer exist (or don't exist yet) are still present here.
    """

    package: str
    symbols: dict[str, ApiSymbol] = field(default_factory=dict)
    docs_url: str | None = None


@dataclass
class Finding:
    rule: str
    severity: Severity
    confidence: Confidence
    message: str
    file: str
    line: int | None = None
    col: int | None = None
    api: str | None = None
    version: str | None = None
    target: str | None = None
    fix: str | None = None
    source_url: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {k: v for k, v in asdict(self).items() if v is not None}
