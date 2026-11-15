"""Symbol paths, shared by every API kind.

Python paths are dotted: `openai.OpenAI.chat`. npm paths start with the module specifier and a
colon, because specifiers can contain dots and slashes: `react:useState`,
`chart.js:Chart.register`, and `react-dom/client:` for the module itself.
"""

from __future__ import annotations

from collections.abc import Sequence


def segments(path: str) -> list[str]:
    module, sep, rest = path.partition(":")
    if not sep:
        return path.split(".")
    return [f"{module}:", *rest.split(".")] if rest else [f"{module}:"]


def join(parts: Sequence[str]) -> str:
    out = parts[0]
    for part in parts[1:]:
        out = child(out, part)
    return out


def child(parent: str, name: str) -> str:
    return parent + name if parent.endswith(":") else f"{parent}.{name}"


def prefix(path: str, n: int) -> str:
    return join(segments(path)[:n])


def parent(path: str) -> str | None:
    parts = segments(path)
    return join(parts[:-1]) if len(parts) > 1 else None


def leaf(path: str) -> str:
    return segments(path)[-1]


def contains(outer: str, inner: str) -> bool:
    """True when `inner` is `outer` or lives under it."""
    a, b = segments(outer), segments(inner)
    return b[: len(a)] == a


def related(a: str, b: str) -> bool:
    return contains(a, b) or contains(b, a)


def display(path: str) -> str:
    return path.removesuffix(":")
