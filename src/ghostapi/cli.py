"""Command-line adapter. No logic lives here; it only parses arguments and renders results."""

from __future__ import annotations

import json
import subprocess
import sys
from enum import StrEnum
from pathlib import Path
from typing import Annotated, cast

import typer

from ghostapi import __version__, bootstrap, hooks, inventory, registry
from ghostapi import baseline as baselines
from ghostapi.checker import Report, run_check
from ghostapi.models import ApiKind, Severity
from ghostapi.output import RENDERERS, to_sarif
from ghostapi.project import changed_files, diff_files, rel, source_files, staged_files
from ghostapi.providers import default_providers
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
    github = "github"


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
        typer.Option(
            help="agents-md | claude | cursor | codex | git. Repeatable. Default: detect."
        ),
    ] = None,
) -> None:
    """Create .ghost/, add agent instructions and install check hooks."""
    allowed = set(bootstrap.AGENTS)
    if agent and not set(agent) <= allowed:
        raise _fail(f"unknown --agent {sorted(set(agent) - allowed)}; use {sorted(allowed)}")
    try:
        result = bootstrap.init(root, cast(list[bootstrap.Agent], agent) if agent else None)
    except json.JSONDecodeError as exc:
        raise _fail(
            f"an existing settings/hooks JSON file is invalid ({exc}); fix it and rerun"
        ) from exc
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
    owner: Annotated[str | None, typer.Option(help="Who owns this API (agent, team).")] = None,
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
            owner=owner,
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


class Method(StrEnum):
    GET = "GET"
    POST = "POST"
    PUT = "PUT"
    PATCH = "PATCH"
    DELETE = "DELETE"
    HEAD = "HEAD"
    OPTIONS = "OPTIONS"
    ANY = "*"


@app.command()
def provide(
    method: Annotated[
        Method, typer.Option(case_sensitive=False, help="HTTP method, or * for any.")
    ],
    path: Annotated[str, typer.Option(help="Route path, e.g. /api/orders/{id}.")],
    owner: Annotated[str | None, typer.Option(help="Who owns it (agent, team).")] = None,
    description: Annotated[str | None, typer.Option(help="What it is for.")] = None,
    file: Annotated[Path | None, typer.Option(help="Where it is implemented.")] = None,
    symbol: Annotated[str | None, typer.Option(help="Handler function.")] = None,
    root: RootOption = Path("."),
) -> None:
    """Declare an endpoint this project serves: who owns it, or one the route scanner can't see."""
    relative = None
    if file is not None:
        relative = rel(root, file if file.is_absolute() else root / file)
        if relative is None:
            raise _fail(f"{file} is outside {root.resolve()}")
    try:
        path = inventory.endpoint_path(path)
    except inventory.QueryError as exc:
        raise _fail(str(exc)) from exc
    endpoint = registry.ProvidedEndpoint(
        method=method.value, path=path, owner=owner, description=description,
        file=relative, symbol=symbol,
    )  # fmt: skip
    try:
        written = registry.provide(root, endpoint)
    except RegistryError as exc:
        raise _fail(str(exc)) from exc
    typer.echo(f"declared {endpoint.method} {path} in {rel(root, written) or written}")


@app.command()
def unprovide(
    method: Annotated[Method, typer.Option(case_sensitive=False)],
    path: Annotated[str, typer.Option()],
    root: RootOption = Path("."),
) -> None:
    """Remove an endpoint declaration."""
    try:
        path = inventory.endpoint_path(path)
    except inventory.QueryError as exc:
        raise _fail(str(exc)) from exc
    removed = registry.unprovide(root, method.value, path)
    typer.echo("removed" if removed else "no such declaration")


@app.command(name="list")
def list_apis(
    root: RootOption = Path("."),
    format: Annotated[Format, typer.Option("--format", "-f")] = Format.text,
) -> None:
    """Every endpoint this project serves and every external API it uses."""
    data = inventory.inventory(root)
    typer.echo(
        json.dumps(data, indent=2) if format is Format.json else inventory.text_inventory(data)
    )


@app.command()
def show(
    target: Annotated[
        str,
        typer.Argument(
            help='"POST /api/orders", "/api/orders/{id}", "openai.OpenAI", "react:useState".'
        ),
    ],
    root: RootOption = Path("."),
    format: Annotated[Format, typer.Option("--format", "-f")] = Format.text,
    offline: Annotated[bool | None, typer.Option("--offline/--online")] = None,
) -> None:
    """The current contract of an endpoint or library symbol: check it before writing a call."""
    if inventory.is_endpoint_query(target):  # an endpoint, not a library symbol
        try:
            infos = inventory.find_endpoints(root, target)
        except inventory.QueryError as exc:
            raise _fail(str(exc)) from exc
        if format is Format.json:
            typer.echo(json.dumps([i.to_dict() for i in infos], indent=2))
        else:
            typer.echo(inventory.text_endpoints(infos))
        if not infos:
            raise typer.Exit(1)
        return
    data = inventory.describe_symbol(root, target, default_providers(offline))
    typer.echo(json.dumps(data, indent=2) if format is Format.json else inventory.text_symbol(data))
    if data["status"] not in ("ok", "unverifiable", "deprecated"):
        raise typer.Exit(1)


class HookAgent(StrEnum):
    claude = "claude"
    codex = "codex"
    cursor_edit = "cursor-edit"
    cursor_stop = "cursor-stop"


def _emit(result: hooks.HookResult) -> None:
    if result.stdout:
        typer.echo(result.stdout)
    if result.stderr:
        typer.echo(result.stderr, err=True)
    if result.code:
        raise typer.Exit(result.code)


@app.command()
def hook(agent: Annotated[HookAgent, typer.Argument(help="Which agent is calling.")]) -> None:
    """Entry point for agent hooks (installed by `ghostapi init`); reads the payload on stdin."""
    _emit(hooks.run(agent.value, sys.stdin.read()))


@app.command()
def check(
    paths: Annotated[
        list[Path] | None, typer.Argument(help="Files/dirs. Default: project.")
    ] = None,
    root: RootOption = Path("."),
    format: Annotated[Format, typer.Option("--format", "-f")] = Format.text,
    fail_on: Annotated[FailOn, typer.Option(help="Exit 1 at this severity.")] = FailOn.error,
    staged: Annotated[bool, typer.Option(help="Only git-staged files.")] = False,
    changed: Annotated[
        bool, typer.Option(help="Only files git sees as modified or new (any agent, any editor).")
    ] = False,
    diff_base: Annotated[
        str | None, typer.Option(help="Only files changed since this git ref (pull requests).")
    ] = None,
    baseline: Annotated[
        Path | None, typer.Option(help="Hide findings this baseline file already accepts.")
    ] = None,
    sarif_out: Annotated[
        Path | None, typer.Option(help="Also write SARIF to this file (code scanning).")
    ] = None,
    hook: Annotated[
        bool, typer.Option(help="Deprecated alias for `ghostapi hook claude`.", hidden=True)
    ] = False,
    offline: Annotated[
        bool | None,
        typer.Option("--offline/--online", help="Use only cached indexes (env GHOSTAPI_OFFLINE)."),
    ] = None,
) -> None:
    """Check API usage against the pinned versions and the registry."""
    if hook:
        _emit(hooks.run("claude", sys.stdin.read()))
        return
    if sum(map(bool, (staged, changed, diff_base, paths))) > 1:
        raise _fail("use only one of PATHS, --staged, --changed and --diff-base")

    files: list[Path] | None = None
    try:
        if staged:
            files = staged_files(root)
        elif changed:
            files = changed_files(root)
        elif diff_base:
            files = diff_files(root, diff_base)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise _fail(f"could not list files from git: {exc}") from exc
    if paths:
        files = []
        for path in paths:
            resolved = path if path.is_absolute() else root / path
            files.extend(source_files(resolved) if resolved.is_dir() else [resolved])

    try:
        report = run_check(root, files=files, offline=offline)
        if baseline is not None:
            accepted = baselines.load(baseline if baseline.is_absolute() else root / baseline)
            report.findings, report.baselined = baselines.apply(report.findings, accepted)
    except (RegistryError, baselines.BaselineError) as exc:
        raise _fail(str(exc)) from exc
    if sarif_out is not None:
        sarif_out.write_text(to_sarif(report), encoding="utf-8")
    typer.echo(RENDERERS[format.value](report))
    if _fails(report, fail_on):
        raise typer.Exit(1)


@app.command(name="baseline")
def write_baseline(
    root: RootOption = Path("."),
    output: Annotated[Path, typer.Option(help="Baseline file.")] = baselines.DEFAULT_PATH,
    offline: Annotated[bool | None, typer.Option("--offline/--online")] = None,
) -> None:
    """Accept every current finding, so `check --baseline` only reports new ones."""
    try:
        report = run_check(root, offline=offline)
    except RegistryError as exc:
        raise _fail(str(exc)) from exc
    path = output if output.is_absolute() else root / output
    count = baselines.write(path, report.findings)
    typer.echo(f"accepted {count} finding(s) in {rel(root, path) or path}")


def main() -> None:
    app()
