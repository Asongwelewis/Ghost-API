# Ghost API

Catches hallucinated, removed, too-new and moved API calls in AI-written code, checked against the **exact versions in the project's lockfile** (Python/PyPI, JS/TS/npm), and keeps the **project's own HTTP endpoints** consistent when several agents build them in parallel. Shipped as a **CLI package** (`ghostapi`) with agent hooks and a GitHub Action; MCP is on hold (no hosting).

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
Agents record what they use and provide in `.ghost/` via the CLI, never by hand: `apis/<api>/api.yaml` + `apis/<api>/calls/<target>.yaml` (one file per target, so parallel agents and git worktrees rarely touch the same file), `provides/<endpoint>.yaml` (endpoint owners / routes the scanner can't see), optional `config.yaml` (`http.internal_hosts`, `http.base_url_names`, `http.openapi`). All writes hold `.ghost/.lock` (`fsutil.exclusive_lock`) and replace files atomically; 0.1's single-file `apis/<api>.yaml` is still read and migrated on the next write.
- **Registry = what the code claims to call** (removes guesswork, especially for REST). **Ghost's index = what really exists.** The registry is never trusted as truth, because the agent that hallucinates also writes it.
- **Reconciliation** keeps the registry honest: a static scan finds calls missing from the registry (`unregistered-api-call`) and entries whose file, symbol or import no longer exists (`stale-entry`). Locations are `file` + `symbol` (+ `line_hint`), so ordinary edits don't break them.
- **Deterministic enforcement**: `ghostapi init` writes agent instructions (AGENTS.md, CLAUDE.md, Cursor rules) and installs hooks (`hooks.py`, entry point `ghostapi hook <agent>`):
  - Claude Code `.claude/settings.json` PostToolUse `Edit|MultiEdit|Write` → exit 2 + stderr reaches the model.
  - Codex `.codex/hooks.json` PostToolUse `apply_patch|Edit|Write` → paths parsed from the patch text; exit 2 + stderr. Codex runs project hooks only once the user trusts `.codex/`.
  - Cursor `.cursor/hooks.json`: `afterFileEdit` can't reply, so it appends to `.ghost/.pending`; `stop` checks those files and returns `followup_message` (Cursor caps follow-up loops at 5).
  - git pre-commit (`ghostapi check --staged`). Any other agent or editor: `ghostapi check --changed` (git-modified + untracked files).
  - Not supported, on purpose: Gemini CLI (replaced by Antigravity, June 2026); Copilot and Windsurf hooks don't document the edited file's path.
  - Hooks never fail on a payload they don't understand: they exit 0.

## Rules
| Rule | Meaning |
|---|---|
| unknown-endpoint / wrong-method | a call to this project's own API that matches no route / not that method |
| endpoint-removed | an edit (vs `HEAD`) removed a route other code still calls; reported to the editing agent at the callers |
| duplicate-endpoint / similar-endpoint | two definitions of one route; near-identical paths (`/user/{id}` vs `/users/{id}`) |
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
- `registry.py`: `.ghost/` schema (pydantic), register/unregister, provide/unprovide, legacy migration; `fsutil.py`: lock + atomic write
- `config.py`: `.ghost/config.yaml`
- `endpoints/`: the project's own HTTP API. `__init__` (Endpoint/HttpCall, route-template normalisation across frameworks, matching, internal-URL test); `python.py` (FastAPI routers + include_router, Flask blueprints, Django path/include + DRF routers; requests/httpx incl. client base_url, f-strings, module constants); `javascript.py` (Express/Fastify/Hono/Koa routers + `use` mounts, `route()` chains, Next.js app/pages routes (needs next.config.*), NestJS controllers + setGlobalPrefix; fetch/$fetch/useFetch, axios incl. `create({baseURL})`); `index.py` (cross-file prefix composition; untraceable mounts → `prefix_known=False`, suffix match; Django roots; OpenAPI files; declared endpoints); `check.py` (rules; hook mode skips the project scan unless the edit touches calls/routes; `endpoint-removed` re-indexes with `git show HEAD:` for edited files). Calls only count as ours when relative, on an internal host or a configured base name, and their first segment is one our routes use.
- `inventory.py`: `ghostapi list` / `ghostapi show` (endpoint: definitions, owner, callers; symbol: pinned signature + problems)
- `paths.py`: symbol paths. Python `openai.OpenAI.chat`; npm `<module>:<export>.<member>` (`react:useState`, `react-dom/client:createRoot`) because specifiers contain dots/slashes. Never split paths by hand.
- `resolver.py`: lockfile pins (uv.lock, poetry.lock, requirements.txt, pyproject; package-lock.json, pnpm-lock.yaml, yarn.lock v1/berry, ambiguous multi-version entries skipped), package.json deps, import-name → distribution
- `extractors/`: `FileScan`; `python.py` (stdlib `ast`); `javascript.py` (tree-sitter: imports, require, re-exports, member chains, calls/new + arg counts, JSX and type positions). Bare specifiers count as external only if declared in package.json/lockfile (tsconfig aliases).
- `tsparse.py`: tree-sitter TS/TSX parsers + node helpers.
- `fetch.py`: the only network layer (httpx + on-disk cache, atomic writes, offline mode). Cache: `GHOSTAPI_CACHE_DIR`, else `%LOCALAPPDATA%\ghostapi\Cache` / `~/.cache/ghostapi`.
- `providers/__init__.py`: `Provider` protocol (`history(package, version, targets) -> ApiHistory | None`), `StaticProvider` for tests, `default_providers()`.
- `providers/surface.py`: language-neutral surface of one version (nodes keyed by canonical id, `roots` = importable modules; `dynamic` = can't see into it → engine stays silent).
- `providers/pysurface.py`: griffe builder (dynamic: module `__getattr__`, `globals()`, star-import from compiled/external code, external base classes).
- `providers/dts.py`: `.d.ts` builder (package.json `exports` conditions + wildcards, `types`, deep imports; `export =`, namespaces (members always exported), declaration merging, `declare const x: Iface` → Iface members, re-exports, ambient `declare module` of its own name; overloads → no params; dynamic: index signatures, bases/star-exports from other packages, global scripts).
- `providers/history.py`: `VersionedProvider` base. Indexes only the pinned version; a missing symbol triggers history: latest release (→ `too-new`, bisect `added_in`), up to 3 older series anchors (→ `removed-in`, try the anchor's next release, then bisect; single public same-name match → `moved_to`), else never existed. Surfaces and answers cached per version.
- `providers/pypi.py`, `providers/npm.py`: registry clients + builders (npm uses the abbreviated registry doc and tarball `.d.ts` only). Checker falls back to pinned `@types/x` when `x` ships no types; findings then name `@types/x` and its version.
- `engine.py`: rules. Knows nothing about providers or extractors.
- `reconcile.py`: registry ↔ code drift rules
- `checker.py`: orchestrates everything into a `Report`
- `output.py`: text / JSON / SARIF 2.1.0 / GitHub annotations (`--format github`)
- `baseline.py`: accepted findings keyed by rule+file+target (line-independent, count-aware); `ghostapi baseline`, `check --baseline`
- `bootstrap.py`: `init` (agent instructions + hooks, idempotent, merges into existing JSON configs, refuses to overwrite invalid ones)
- `hooks.py`: per-agent hook adapters (payload → files → check → that agent's feedback channel)
- `cli.py`: typer adapter only, no logic

Adding an API type = a provider (+ an extractor if it can be scanned). Never new rules.

## Commands
```
uv sync                       # install
uv run pytest                 # tests (offline; conftest forces GHOSTAPI_OFFLINE + temp cache)
GHOSTAPI_NETWORK_TESTS=1 uv run pytest -m network   # live PyPI + npm tests (~90s cold)
uv run ruff check . && uv run ruff format .
uv run pyright
uv run ghostapi --help
```
Git Bash on Windows rewrites arguments starting with `/`: pass endpoint paths as `api/orders` (Ghost adds the slash) or set `MSYS_NO_PATHCONV=1`.

## Branching, CI/CD
- `main` = released code only. `dev` = integration branch. Work on `feat/*` / `fix/*` branches, PR into `dev`; release by PR `dev` → `main`, then tag `vX.Y.Z` on main (must equal `pyproject` version).
- `.github/workflows/ci.yml` (push/PR to main+dev, weekly): lint + format + pyright; pytest on Linux/Windows/macOS × py3.12–3.14; non-blocking live PyPI test; build + wheel smoke test; `CI passed` gate job (the only check branch rules require).
- Published to PyPI as `ghostapi0`: `ghost-api` belongs to someone else, and PyPI treats `ghostapi` as the same name. The command stays `ghostapi`.
- `.github/workflows/release.yml` (tag `v*`): verify tag/version/main, test, build, publish to PyPI via trusted publishing (environment `pypi`), GitHub release.
- `action.yml` (repo root, composite): installs ghostapi from the action's own checkout (or `version:` from PyPI), caches indexes keyed on lockfiles, runs `check --format github --sarif-out` with `--diff-base FETCH_HEAD` on PRs and `.ghost/baseline.json` when present, optional SARIF upload; inputs go through env vars, never inline `${{ }}` in scripts. Self-tested by the `action` job against `.github/action-test/`.
- Dependabot targets `dev`. Use `uv sync --locked` in CI, so commit `uv.lock` with dependency changes.

## Stack
Python 3.12+ · uv · typer · pydantic v2 · PyYAML · packaging · stdlib `ast` · griffe (static Python API loading) · tree-sitter + tree-sitter-typescript (JS/TS + `.d.ts`) · httpx. MCP server is on hold (no hosting infrastructure).

## Roadmap
- **Done**: registry (concurrency-safe) + drift rules, Python + JS/TS scanners, lockfiles (pip/uv/poetry/npm/pnpm/yarn), rule engine, PyPI + npm providers with version history, project endpoint checks for multi-agent work (routes vs calls, removals, duplicates, owners, `list`/`show`), Claude/Codex/Cursor hooks, GitHub Action (diff-only, baseline, thresholds, SARIF), CI/CD.
- **MVP (wk 1–4)**: monorepo workspaces · F5 fix suggestions from index diffs · `ghostapi watch` · F8 eval harness / benchmark. `ghostapi mcp` on hold (no infrastructure).
- **v1**: unknown-package / slopsquat pre-install gate · what-if upgrade mode · Markdown/notebook snippets · web playground · public leaderboard.
- **v1.5**: external OpenAPI/REST and GraphQL providers, request/response body checks for own endpoints, boto3/botocore, stdlib-by-runtime-version providers.
- **Later**: Go/Java, gRPC/proto, config files (Tailwind/Next), safe codemods, editor extension, maintainer radar.

## Working agreements
- Every rule needs fixture tests, ideally with a real before/after package version.
- Network access (registries, tarballs) goes through one cached fetch layer; tests run offline against fixtures.
- CLI output stays ASCII (Windows consoles).
