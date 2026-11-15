# Ghost API

[![CI](https://github.com/Asongwelewis/Ghost-API/actions/workflows/ci.yml/badge.svg?branch=dev)](https://github.com/Asongwelewis/Ghost-API/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
![Python 3.12 | 3.13 | 3.14](https://img.shields.io/badge/python-3.12%20%7C%203.13%20%7C%203.14-blue)

**Catch the API calls your AI coding agents get wrong, before anyone runs the code.**

Coding agents write calls to functions that don't exist, to APIs that were removed two major
versions ago, and to endpoints another agent renamed ten minutes earlier. Ghost API checks that
code against reality and hands the problems straight back to the agent:

- **Libraries.** Every call is checked against the *exact version in your lockfile*, read
  from the package's published files. Nothing is installed or executed.
- **Your own endpoints.** When several agents build one product at once, Ghost reads the routes
  your backend serves and the HTTP calls your frontend makes, and tells the agent that breaks a
  contract which callers it just broke.

```text
app.py:6:12 error removed-in [high] `openai.ChatCompletion` was removed in openai 1.0.0; you have 1.40.0.
app.py:9:12 error unknown-symbol [high] `openai.OpenAl` does not exist in any known version of openai.
    fix: Did you mean `openai.OpenAI`?
src/App.tsx:1:1 error too-new [high] `react:useActionState` was added in @types/react 19.0.0; you have 18.3.12.
web/src/client.ts:6:9 error endpoint-removed [medium] This call broke: the edit to backend/routers/orders.py
    removed or changed `POST /api/orders`, and `POST /api/orders` no longer matches any route.
```

## Install

```sh
uv tool install ghostapi0          # or: pipx install ghostapi0   (the command is `ghostapi`)
```

The first release isn't on PyPI yet. Until it is, install from source:

```sh
uv tool install git+https://github.com/Asongwelewis/Ghost-API@dev
```

## Quickstart

```sh
cd your-project
ghostapi init              # .ghost/ registry, agent instructions, hooks for the agents it finds
ghostapi check             # check the whole project (text, --format json|sarif|github)
ghostapi show openai.OpenAI          # the real signature in your pinned version
ghostapi show "POST /api/orders"     # where an endpoint lives, who owns it, who calls it
ghostapi list              # every endpoint you serve and every external API you use
```

The first check of a package downloads it and builds an index; later checks use the cache and
also work offline (`--offline`).

## What it catches

| Problem | Rule | Example |
|---|---|---|
| Invented function, class or attribute | `unknown-symbol` | `openai.OpenAl`, "did you mean `openai.OpenAI`?" |
| API removed in your version | `removed-in` | `openai.ChatCompletion` (removed in 1.0.0) |
| API newer than your version | `too-new` | React 19's `useActionState` on React 18 |
| API moved to another module | `import-path` | gives the new import path |
| Wrong arguments | `bad-parameter`, `arity` | `OpenAI(timout=30)`, "did you mean `timeout`?" |
| Deprecated API | `deprecated` | still works; tells you so |
| Call to an endpoint you don't serve | `unknown-endpoint` | `fetch("/api/order/1")` when the route is `/api/orders/{id}` |
| Wrong HTTP method | `wrong-method` | `PUT` on a route that only accepts `GET` and `DELETE` |
| Route removed while code still calls it | `endpoint-removed` | reported to the agent that removed it, at the broken callers |
| Same endpoint built twice | `duplicate-endpoint`, `similar-endpoint` | two agents both add `GET /users/{id}` |

All rules, with examples: [docs/rules.md](docs/rules.md). Ghost stays silent when it can't be
sure (dynamic attributes, overloads, routes mounted in ways it can't trace), because a false
error costs more trust than a missed one.

## Works with your agent

`ghostapi init` wires the check into the agents it finds, so problems reach the agent right
after it edits a file:

| Agent | How Ghost runs | How the agent hears about problems |
|---|---|---|
| Claude Code | `PostToolUse` hook after every edit | hook output (exit 2) |
| OpenAI Codex | `PostToolUse` hook on `apply_patch` (trust the project's `.codex/`) | hook output (exit 2) |
| Cursor | edits recorded, checked when the agent stops | a follow-up message |
| Any other agent, editor or human | `ghostapi check --changed`, git pre-commit hook, CI | command output |

Details: [docs/agents.md](docs/agents.md).

## Several agents, one codebase

Git worktrees stop parallel agents from overwriting each other's files, but not from disagreeing
about an API. Ghost covers that gap:

- **Look before you call:** `ghostapi show "POST /api/orders"` gives an agent the current
  definition, owner and callers, so it doesn't work from a stale memory of the contract.
- **Claim what you build:** `ghostapi provide --method POST --path api/orders --owner billing-agent`.
- **Hear about what you break:** removing or changing a route that other code calls produces
  `endpoint-removed` in the editing agent's hook, located at every broken caller.
- **Safe for parallel writes:** registry writes are locked and atomic, and stored one file per
  target, so agents in separate worktrees rarely conflict.

Guide: [docs/multi-agent.md](docs/multi-agent.md).

## In CI

```yaml
# .github/workflows/ghostapi.yml
name: Ghost API
on: pull_request
permissions:
  contents: read
jobs:
  check:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v5
      - uses: Asongwelewis/Ghost-API@dev   # pin a release tag or commit once one exists
        with:
          fail-on: error
```

It checks only the files the pull request changes, annotates the problem lines, and can upload
SARIF to code scanning. For a project that already has findings, `ghostapi baseline` makes it
fail only on new ones. See [docs/ci.md](docs/ci.md).

## Supported

- **Languages:** Python, JavaScript, TypeScript (including JSX/TSX).
- **Package registries:** PyPI, npm (plus DefinitelyTyped `@types/*`).
- **Lockfiles:** `uv.lock`, `poetry.lock`, `requirements.txt`, `pyproject.toml`,
  `package-lock.json`, `pnpm-lock.yaml`, `yarn.lock`.
- **Your endpoints:** FastAPI, Flask, Django and DRF, Express, Fastify, Hono, Koa, Next.js,
  NestJS and OpenAPI files; calls through `fetch`, axios, requests and httpx.

The full matrix and known limits are in [docs/supported.md](docs/supported.md).

## Documentation

- [Getting started](docs/getting-started.md)
- [How it works](docs/how-it-works.md)
- [Rules](docs/rules.md)
- [CLI reference](docs/cli.md)
- [Agents and hooks](docs/agents.md)
- [Multi-agent development](docs/multi-agent.md)
- [Configuration](docs/configuration.md)
- [CI and the GitHub Action](docs/ci.md)
- [Supported languages and frameworks](docs/supported.md)
- [Example: full-stack project with deliberate mistakes](examples/fullstack/)

## Contributing

Bug reports, false-positive reports and pull requests are welcome. Start with
[CONTRIBUTING.md](CONTRIBUTING.md). Please follow the [code of conduct](CODE_OF_CONDUCT.md),
and report security issues privately as described in [SECURITY.md](SECURITY.md).

## License

[MIT](LICENSE) © Asongwe Lewis Fon
