# Ghost API

Catches hallucinated, removed, too-new and moved API calls in AI-written code, checked against the **exact versions in the project's lockfile**. Python and JS/TS first; REST/GraphQL next. Shipped as a **CLI package** (`ghostapi`), with MCP and a GitHub Action as thin add-ons.

## Why it exists (cite these, don't invent new stats)
- ~19.7% of AI-suggested packages don't exist (slopsquatting study)
- ICSE 2025 "LLMs Meet Library Evolution": 70–90% deprecated-API usage when given deprecated context
- Feeding import/attribute errors back to the model: pass rate 76% → 90%
- Canonical breakages for tests/demos: `openai.ChatCompletion` removed · `google.generativeai` → `google-genai` · LangChain `LLMChain` moved · Tailwind v4 `@tailwind` → `@import` · React 16 vs 18

## Non-negotiable principles
1. **Never execute or import analysed packages.** Parse `.py`/`.pyi` from wheels and `.d.ts` from npm tarballs / `@types` statically.
2. **Version-exact.** Every verdict is relative to a resolved `package@version`, never "latest". The lockfile beats the registry's `version` field.
3. **No false certainty.** Dynamic code (`getattr`, module `__getattr__`, re-exports, TS overloads) → lower confidence or silence, not an error. No source of truth → `unverifiable`, never an error.
4. **Deterministic first, LLM second.** Findings and rename/move fixes come from symbol-index history. An LLM may only write explanations grounded in a changelog/migration guide, with a citation link.
5. **Machine-readable output is a first-class product** (agents consume it in fix loops): stable rule IDs, exact location, fix hint.
6. **Precision over recall.** A false error costs more trust than a missed one.

## How it works: the registry
The coding agent records every external API call in `.ghost/apis/<api>.yaml` (via `ghostapi register`, never by hand-editing). Ghost then checks it.
- **Registry = what the code claims to call** (removes guesswork, especially for REST). **Ghost's index = what really exists.** The registry is never trusted as truth, because the agent that hallucinates also writes it.
- **Reconciliation** keeps the registry honest: a static scan finds calls missing from the registry (`unregistered-api-call`) and entries whose file, symbol or import no longer exists (`stale-entry`). Locations are `file` + `symbol` (+ `line_hint`), so ordinary edits don't break them.
- **Deterministic enforcement**: `ghostapi init` writes agent instructions (AGENTS.md, CLAUDE.md, Cursor rules) and installs hooks: a Claude Code `PostToolUse` hook (`ghostapi check --hook`, exit 2 feeds findings back to the agent) and a git pre-commit hook (`ghostapi check --staged`).

## Rules
| Rule | Meaning |
|---|---|
| unknown-symbol (+ "did you mean") | never existed in any version (UC1) |
| removed-in | gone in pinned version, with replacement (UC2) |
| too-new | added after pinned version (UC3) |
| import-path | moved; gives new path (UC4) |
| bad-parameter / arity | unknown kwarg, too many positionals, missing required (UC5) |
| deprecated | still works; removal timeline (UC6) |
| unregistered-api-call / stale-entry / registry-version-mismatch | registry ↔ code ↔ lockfile drift |
| unknown-package (v1), mixed-version (later) | slopsquat gate (UC8), v1+v2 mixing (UC7) |

Finding fields: `rule`, `severity` (error/warning/info), `confidence` (high/medium/low/unverifiable), `file`, `line`, `col`, `api`, `version`, `target`, `message`, `fix`, `source_url`.

## Code layout (`src/ghostapi/`)
- `models.py`: `Usage`, `ApiSymbol`, `Param`, `ApiHistory`, `Finding`
- `registry.py`: `.ghost/apis/*.yaml` schema (pydantic), load/save/register/unregister
- `resolver.py`: lockfile pins (uv.lock, poetry.lock, requirements.txt, pyproject, package-lock.json), import-name → distribution
- `extractors/python.py`: stdlib-`ast` usage scan (imports, attribute chains, calls + kwargs, enclosing symbol)
- `fetch.py`: the only network layer (httpx + on-disk cache, atomic writes, offline mode). Cache: `GHOSTAPI_CACHE_DIR`, else `%LOCALAPPDATA%\ghostapi\Cache` / `~/.cache/ghostapi`.
- `providers/__init__.py`: `Provider` protocol (`history(package, version, targets) -> ApiHistory | None`), `StaticProvider` for tests, `default_providers()`.
- `providers/surface.py`: griffe-built static surface of one version (tree of nodes keyed by canonical path; aliases resolved; `dynamic` = can't see into it: module `__getattr__`, `globals()`, star-import from compiled/external code, external base classes).
- `providers/pypi.py`: indexes only the pinned version up front. A symbol missing from it triggers history: latest release (→ `too-new`, bisect for `added_in`), up to 3 older series anchors (→ `removed-in`, try the anchor's next release, then bisect; single public same-name match → `moved_to`), else never existed. Surfaces and answers are cached per version.
- `engine.py`: rules. Knows nothing about providers or extractors.
- `reconcile.py`: registry ↔ code drift rules
- `checker.py`: orchestrates everything into a `Report`
- `output.py`: text / JSON / SARIF 2.1.0
- `bootstrap.py`: `init` (agent instructions + hooks, idempotent, never clobbers user files)
- `cli.py`: typer adapter only, no logic

Adding an API type = a provider (+ an extractor if it can be scanned). Never new rules.

## Commands
```
uv sync                       # install
uv run pytest                 # tests (offline; conftest forces GHOSTAPI_OFFLINE + temp cache)
GHOSTAPI_NETWORK_TESTS=1 uv run pytest -m network   # live PyPI test (~20s cold)
uv run ruff check . && uv run ruff format .
uv run pyright
uv run ghostapi --help
```

## Branching, CI/CD
- `main` = released code only. `dev` = integration branch. Work on `feat/*` / `fix/*` branches, PR into `dev`; release by PR `dev` → `main`, then tag `vX.Y.Z` on main (must equal `pyproject` version).
- `.github/workflows/ci.yml` (push/PR to main+dev, weekly): lint + format + pyright; pytest on Linux/Windows/macOS × py3.12–3.14; non-blocking live PyPI test; build + wheel smoke test.
- `.github/workflows/release.yml` (tag `v*`): verify tag/version/main, test, build, publish to PyPI via trusted publishing (environment `pypi`), GitHub release.
- Dependabot targets `dev`. Use `uv sync --locked` in CI, so commit `uv.lock` with dependency changes.

## Stack
Python 3.12+ · uv · typer · pydantic v2 · PyYAML · packaging · stdlib `ast` · griffe (static API loading) · httpx. Planned: `tree-sitter` (JS/TS + `.d.ts`), `mcp` SDK (`ghostapi mcp`).

## Roadmap
- **Done**: registry + drift rules, Python scanner, lockfiles, rule engine, PyPI provider with version history, CI/CD.
- **MVP (wk 1–4)**: npm provider with version history · F2 add pnpm/yarn locks + monorepo workspaces · F5 fix suggestions from index diffs · `ghostapi watch` · `ghostapi mcp` (`check_code`, `get_signature`, `what_changed`) · F8 eval harness / benchmark.
- **v1**: GitHub Action (diff-only, baseline, thresholds) · unknown-package / slopsquat pre-install gate · what-if upgrade mode · Markdown/notebook snippets · web playground · public leaderboard.
- **v1.5**: OpenAPI/REST, GraphQL, boto3/botocore, stdlib-by-runtime-version providers.
- **Later**: Go/Java, gRPC/proto, config files (Tailwind/Next), safe codemods, editor extension, maintainer radar.

## Working agreements
- Every rule needs fixture tests, ideally with a real before/after package version.
- Network access (registries, tarballs) goes through one cached fetch layer; tests run offline against fixtures.
- CLI output stays ASCII (Windows consoles).
