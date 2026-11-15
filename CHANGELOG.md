# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

First public version.

### Added

- `ghostapi` CLI: `init`, `check`, `show`, `list`, `register`, `unregister`, `provide`,
  `unprovide`, `baseline` and `hook`.
- Library checks against the exact version pinned in the lockfile, for Python (PyPI) and
  JavaScript/TypeScript (npm, with DefinitelyTyped fallback): `unknown-symbol`, `removed-in`,
  `too-new`, `import-path`, `deprecated`, `bad-parameter` and `arity`, with fix hints.
- Version history from published packages: added-in and removed-in versions found by bisecting
  releases, cached on disk; offline mode.
- Checks for the project's own HTTP API, for teams of agents: routes from FastAPI, Flask,
  Django and DRF, Express, Fastify, Hono, Koa, Next.js, NestJS and OpenAPI files; calls from
  `fetch`, axios, requests and httpx. Rules `unknown-endpoint`, `wrong-method`,
  `endpoint-removed`, `duplicate-endpoint` and `similar-endpoint`.
- `.ghost/` registry of the APIs a project uses and provides, with owners and drift checks
  (`unregistered-api-call`, `stale-entry`, `registry-version-mismatch`). Writes are locked and
  atomic, one file per target.
- Agent hooks for Claude Code, OpenAI Codex and Cursor; agent instructions in `AGENTS.md`,
  `CLAUDE.md` and Cursor rules; git pre-commit hook; `check --changed` for everything else.
- GitHub Action: checks only changed files, baselines, failure thresholds, pull-request
  annotations and SARIF upload.
- Lockfiles: `uv.lock`, `poetry.lock`, `requirements.txt`, `pyproject.toml`,
  `package-lock.json`, `pnpm-lock.yaml` and `yarn.lock`.
- Output formats: text, JSON, SARIF 2.1.0 and GitHub annotations.

[Unreleased]: https://github.com/Asongwelewis/Ghost-API/commits/dev
