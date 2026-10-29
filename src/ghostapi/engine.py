"""Rule engine: compare one usage with the real API history.

Knows nothing about where usages or histories come from, so every API type shares these rules.
"""

from __future__ import annotations

from difflib import get_close_matches

from packaging.version import InvalidVersion, Version

from ghostapi.models import ApiHistory, ApiSymbol, Finding, Severity, Usage

_CONTAINERS = {"module", "class"}


def _version(value: str | None) -> Version | None:
    if value is None:
        return None
    try:
        return Version(value)
    except InvalidVersion:
        return None


def _at_least(version: str | None, bound: str | None) -> bool:
    have, need = _version(version), _version(bound)
    return have is not None and need is not None and have >= need


def _below(version: str | None, bound: str | None) -> bool:
    have, need = _version(version), _version(bound)
    return have is not None and need is not None and have < need


class _Ctx:
    def __init__(self, usage: Usage, package: str, version: str | None, history: ApiHistory):
        self.usage = usage
        self.package = package
        self.version = version
        self.history = history

    @property
    def pinned(self) -> str:
        return f"{self.package} {self.version}" if self.version else self.package

    def finding(
        self,
        rule: str,
        severity: Severity,
        message: str,
        *,
        target: str | None = None,
        fix: str | None = None,
        source_url: str | None = None,
    ) -> Finding:
        return Finding(
            rule=rule,
            severity=severity,
            confidence=self.usage.confidence,
            message=message,
            file=self.usage.file,
            line=self.usage.line,
            col=self.usage.col,
            api=self.package,
            version=self.version,
            target=target or self.usage.target,
            fix=fix,
            source_url=source_url or self.history.docs_url,
        )


def check_usage(
    usage: Usage, *, package: str, version: str | None, history: ApiHistory
) -> list[Finding]:
    ctx = _Ctx(usage, package, version, history)
    symbol = history.symbols.get(usage.target)
    if symbol is None:
        # `openai.ChatCompletion.create` is explained by `openai.ChatCompletion` being removed.
        parts = usage.target.split(".")
        for end in range(len(parts) - 1, 0, -1):
            ancestor = history.symbols.get(".".join(parts[:end]))
            if ancestor is not None:
                return _lifecycle(ctx, ancestor) or _unknown(ctx)
        return []
    findings = _lifecycle(ctx, symbol)
    if any(f.severity is Severity.ERROR for f in findings):
        return findings
    return findings + _params(ctx, symbol)


def _unknown(ctx: _Ctx) -> list[Finding]:
    parts = ctx.usage.target.split(".")
    symbols = ctx.history.symbols
    # Longest prefix the index knows about; only judge the segment right after it.
    for end in range(len(parts) - 1, 0, -1):
        parent_path = ".".join(parts[:end])
        parent = symbols.get(parent_path)
        if parent is None:
            continue
        if parent.dynamic or parent.kind not in _CONTAINERS:
            return []  # attribute could be created at runtime, or lives on an instance
        missing = f"{parent_path}.{parts[end]}"
        siblings = sorted(
            p.rsplit(".", 1)[1]
            for p, s in symbols.items()
            if p.rpartition(".")[0] == parent_path and _exists_at(s, ctx.version)
        )
        match = get_close_matches(parts[end], siblings, n=1, cutoff=0.6)
        return [
            ctx.finding(
                "unknown-symbol",
                Severity.ERROR,
                f"`{missing}` does not exist in any known version of {ctx.package}.",
                target=missing,
                fix=f"Did you mean `{parent_path}.{match[0]}`?" if match else None,
            )
        ]
    return []


def _exists_at(symbol: ApiSymbol, version: str | None) -> bool:
    return not _at_least(version, symbol.removed_in) and not _below(version, symbol.added_in)


def _lifecycle(ctx: _Ctx, symbol: ApiSymbol) -> list[Finding]:
    target = symbol.path
    url = symbol.source_url
    if symbol.removed_in and _at_least(ctx.version, symbol.removed_in):
        if symbol.moved_to:
            return [
                ctx.finding(
                    "import-path",
                    Severity.ERROR,
                    f"`{target}` moved to `{symbol.moved_to}` in {ctx.package} "
                    f"{symbol.removed_in}; you have {ctx.version}.",
                    fix=f"Use `{symbol.moved_to}`.",
                    target=target,
                    source_url=url,
                )
            ]
        return [
            ctx.finding(
                "removed-in",
                Severity.ERROR,
                f"`{target}` was removed in {ctx.package} {symbol.removed_in}; "
                f"you have {ctx.version}.",
                fix=f"Use `{symbol.replacement}`." if symbol.replacement else None,
                target=target,
                source_url=url,
            )
        ]
    if symbol.added_in and _below(ctx.version, symbol.added_in):
        fix = f"Upgrade {ctx.package} to >= {symbol.added_in}"
        if symbol.replacement:
            fix += f", or use `{symbol.replacement}`"
        return [
            ctx.finding(
                "too-new",
                Severity.ERROR,
                f"`{target}` was added in {ctx.package} {symbol.added_in}; you have {ctx.version}.",
                fix=fix + ".",
                target=target,
                source_url=url,
            )
        ]
    if symbol.deprecated_in and _at_least(ctx.version, symbol.deprecated_in):
        timeline = f" and is removed in {symbol.removed_in}" if symbol.removed_in else ""
        return [
            ctx.finding(
                "deprecated",
                Severity.WARNING,
                f"`{target}` is deprecated in {ctx.pinned}{timeline}.",
                fix=f"Use `{symbol.replacement}`." if symbol.replacement else None,
                target=target,
                source_url=url,
            )
        ]
    return []


def _params(ctx: _Ctx, symbol: ApiSymbol) -> list[Finding]:
    usage = ctx.usage
    if symbol.params is None or usage.n_positional is None or usage.has_star_args:
        return []
    params = symbol.params
    findings: list[Finding] = []

    if not any(p.kind == "var_keyword" for p in params):
        keyword_names = sorted(
            p.name for p in params if p.kind in ("positional_or_keyword", "keyword_only")
        )
        for kw in usage.kwargs:
            if kw not in keyword_names:
                match = get_close_matches(kw, keyword_names, n=1, cutoff=0.6)
                findings.append(
                    ctx.finding(
                        "bad-parameter",
                        Severity.ERROR,
                        f"`{usage.target}` has no parameter `{kw}` in {ctx.pinned}.",
                        fix=f"Did you mean `{match[0]}`?" if match else None,
                    )
                )

    positional = [p for p in params if p.kind in ("positional_only", "positional_or_keyword")]
    if usage.n_positional > len(positional) and not any(p.kind == "var_positional" for p in params):
        findings.append(
            ctx.finding(
                "arity",
                Severity.ERROR,
                f"`{usage.target}` takes at most {len(positional)} positional argument(s) in "
                f"{ctx.pinned}; got {usage.n_positional}.",
            )
        )

    missing = [
        p.name
        for i, p in enumerate(positional)
        if p.required and i >= usage.n_positional and p.name not in usage.kwargs
    ] + [
        p.name
        for p in params
        if p.kind == "keyword_only" and p.required and p.name not in usage.kwargs
    ]
    if missing:
        findings.append(
            ctx.finding(
                "arity",
                Severity.ERROR,
                f"`{usage.target}` is missing required argument(s) in {ctx.pinned}: "
                + ", ".join(f"`{m}`" for m in missing)
                + ".",
            )
        )
    return findings
