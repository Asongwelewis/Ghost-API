"""Command-line adapter. No logic lives here; it only parses arguments and renders results."""

from __future__ import annotations

import json
import subprocess
import sys
from enum import StrEnum
from pathlib import Path
from typing import Annotated, cast

import typer

from ghostapi import __version__, bootstrap, registry
from ghostapi.checker import Report, run_check
from ghostapi.models import ApiKind, Severity
from ghostapi.output import RENDERERS, to_text
from ghostapi.project import python_files, rel, staged_files
from ghostapi.registry import Location, RegistryError
from ghostapi.resolver import normalize, resolve_versions

app = typer.Typer(
    help="Catch hallucinated, removed and too-new API calls in AI-written code.",
    no_args_is_help=True,
    pretty_exceptions_enable=False,
)

RootOption = Annotated[Path, typer.Option("--root", help="Project root.")]


class Kind(StrEnum):
    python = "python"
    npm = "npm"
    openapi = "openapi"
    graphql = "graphql"


class Format(StrEnum):
    text = "text"
    json = "json"
    sarif = "sarif"


class FailOn(StrEnum):
    error = "error"
    warning = "warning"
    never = "never"


def _fail(message: str) -> typer.Exit:
    typer.echo(f"ghostapi: {message}", err=True)
    return typer.Exit(2)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(__version__)
        raise typer.Exit()


@app.callback()
def _main(
    version: Annotated[
        bool, typer.Option("--version", callback=_version_callback, is_eager=True)
    ] = False,
) -> None:
    pass


@app.command()
def init(
    root: RootOption = Path("."),
    agent: Annotated[
        list[str] | None,
        typer.Option(help="agents-md | claude | cursor | git. Repeatable. Default: auto-detect."),
    ] = None,
) -> None:
    """Create .ghost/, add agent instructions and install check hooks."""
    allowed = {"agents-md", "claude", "cursor", "git"}
    if agent and not set(agent) <= allowed:
        raise _fail(f"unknown --agent {sorted(set(agent) - allowed)}; use {sorted(allowed)}")
    try:
        result = bootstrap.init(root, cast(list[bootstrap.Agent], agent) if agent else None)
    except json.JSONDecodeError as exc:
        raise _fail(f".claude/settings.json is not valid JSON ({exc}); fix it and rerun") from exc
    for name in result.changed:
        typer.echo(f"  updated  {name}")
    for name in result.skipped:
        typer.echo(f"  kept     {name}")
    typer.echo("Ghost API is set up. Agents will now register API calls; run `ghostapi check`.")


@app.command()
def register(
    api: Annotated[str, typer.Option(help="Package or API name, e.g. openai, stripe-rest.")],
    target: Annotated[str, typer.Option(help='Dotted path or "METHOD /path".')],
    file: Annotated[Path, typer.Option(help="File that makes the call.")],
    kind: Annotated[Kind, typer.Option()] = Kind.python,
    symbol: Annotated[str | None, typer.Option(help="Enclosing function/class.")] = None,
    line: Annotated[int | None, typer.Option(help="Line hint.")] = None,
    version: Annotated[str | None, typer.Option(help="Default: from the lockfile.")] = None,
    module: Annotated[
        str | None, typer.Option(help="Import root if it differs from --api.")
    ] = None,
    source: Annotated[str | None, typer.Option(help="Spec URL for REST/GraphQL.")] = None,
    root: RootOption = Path("."),
) -> None:
    """Record that FILE calls TARGET of API."""
    path = file if file.is_absolute() else root / file
    relative = rel(root, path)
    if relative is None or not path.is_file():
        raise _fail(f"{file} is not a file inside {root.resolve()}")
    if version is None and kind in (Kind.python, Kind.npm):
        pins = resolve_versions(root)[kind.value]
        version = pins.get(normalize(api) if kind is Kind.python else api)
    try:
        entry = registry.register(
            root,
            api=api,
            kind=cast(ApiKind, kind.value),
            target=target,
            location=Location(file=relative, symbol=symbol, line_hint=line),
            version=version,
            module=module,
            source=source,
        )
    except RegistryError as exc:
        raise _fail(str(exc)) from exc
    pinned = f" {entry.version}" if entry.version else ""
    typer.echo(f"registered {target} ({api}{pinned}) in {relative}")


@app.command()
def unregister(
    api: Annotated[str, typer.Option()],
    target: Annotated[str | None, typer.Option(help="Default: every target of API.")] = None,
    file: Annotated[Path | None, typer.Option(help="Default: every file.")] = None,
    root: RootOption = Path("."),
) -> None:
    """Remove registry locations that no longer exist."""
    relative = None
    if file is not None:
        relative = rel(root, file if file.is_absolute() else root / file)
        if relative is None:
            raise _fail(f"{file} is outside {root.resolve()}")
    try:
        removed = registry.unregister(root, api=api, target=target, file=relative)
    except RegistryError as exc:
        raise _fail(str(exc)) from exc
    typer.echo(f"removed {removed} location(s)")


def _fails(report: Report, fail_on: FailOn) -> bool:
    if fail_on is FailOn.never:
        return False
    levels = {Severity.ERROR} if fail_on is FailOn.error else {Severity.ERROR, Severity.WARNING}
    return any(f.severity in levels for f in report.findings)


def _run_hook() -> None:
    """Claude Code PostToolUse hook: check the edited file; exit 2 sends findings to the agent."""
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError:
        raise typer.Exit(0) from None
    edited = payload.get("tool_input", {}).get("file_path")
    if not edited or not edited.endswith(".py"):
        raise typer.Exit(0)
    root = Path(payload.get("cwd") or ".")
    try:
        edited_path = Path(edited) if Path(edited).is_absolute() else root / edited
        report = run_check(root, files=[edited_path])
    except RegistryError as exc:
        typer.echo(f"ghostapi: the registry is invalid: {exc}", err=True)
        raise typer.Exit(2) from exc
    if _fails(report, FailOn.warning):
        typer.echo(to_text(report), err=True)
        raise typer.Exit(2)


@app.command()
def check(
    paths: Annotated[
        list[Path] | None, typer.Argument(help="Files/dirs. Default: project.")
    ] = None,
    root: RootOption = Path("."),
    format: Annotated[Format, typer.Option("--format", "-f")] = Format.text,
    fail_on: Annotated[FailOn, typer.Option(help="Exit 1 at this severity.")] = FailOn.error,
    staged: Annotated[bool, typer.Option(help="Only git-staged files.")] = False,
    hook: Annotated[bool, typer.Option(help="Run as a Claude Code PostToolUse hook.")] = False,
    offline: Annotated[
        bool | None,
        typer.Option("--offline/--online", help="Use only cached indexes (env GHOSTAPI_OFFLINE)."),
    ] = None,
) -> None:
    """Check API usage against the pinned versions and the registry."""
    if hook:
        _run_hook()
        return

    files: list[Path] | None = None
    if staged:
        try:
            files = staged_files(root)
        except (OSError, subprocess.CalledProcessError) as exc:
            raise _fail(f"could not list staged files: {exc}") from exc
    elif paths:
        files = []
        for path in paths:
            resolved = path if path.is_absolute() else root / path
            files.extend(python_files(resolved) if resolved.is_dir() else [resolved])

    try:
        report = run_check(root, files=files, offline=offline)
    except RegistryError as exc:
        raise _fail(str(exc)) from exc
    typer.echo(RENDERERS[format.value](report))
    if _fails(report, fail_on):
        raise typer.Exit(1)


def main() -> None:
    app()
