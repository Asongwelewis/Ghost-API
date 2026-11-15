"""Usage extractors: find what the code actually calls. They read source, never run it."""

from __future__ import annotations

import warnings
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

from ghostapi.models import ApiKind, Usage


@dataclass
class FileScan:
    file: str
    kind: ApiKind = "python"
    usages: list[Usage] = field(default_factory=list)
    defined: set[str] = field(default_factory=set)  # qualnames of functions/classes in the file
    imported_roots: set[str] = field(default_factory=set)  # external top-level modules imported
    error: str | None = None


@contextmanager
def quiet_parsing() -> Iterator[None]:
    """Old packages are full of invalid escapes like "\\*"; their warnings aren't ours to print."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", SyntaxWarning)
        warnings.simplefilter("ignore", DeprecationWarning)
        yield
