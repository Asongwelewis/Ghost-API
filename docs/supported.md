# Supported languages, packages and frameworks

## Languages

| Language | Files | Library calls checked against |
|---|---|---|
| Python | `.py` | PyPI packages |
| JavaScript | `.js`, `.jsx`, `.mjs`, `.cjs` | npm packages (or their `@types/*`) |
| TypeScript | `.ts`, `.tsx`, `.mts`, `.cts` | npm packages (or their `@types/*`) |

`.d.ts` and `.min.js` files are skipped. Build output folders (`node_modules`, `dist`, `build`,
`.next`, `.venv`, and so on) are skipped.

## What a library check covers

- **Python:** imports, attribute chains from imported names, and calls with keyword names and
  positional counts.
- **JS/TS:** imports, `require`, re-exports, member chains, calls and `new` with argument
  counts, JSX tags, and type positions.
- **Not followed:** values returned by calls. `client = OpenAI(); client.chat...` checks
  `OpenAI`, not `.chat`. In `z.string().min(1)`, `z.string` is checked, not `.min`.

## Your own endpoints

| Framework | Routes Ghost reads |
|---|---|
| FastAPI | `@app.get(...)`, `APIRouter(prefix=...)`, `include_router(..., prefix=...)` across files, `api_route`, `add_api_route` |
| Flask | `@app.route(..., methods=[...])`, `@app.get(...)`, `Blueprint(url_prefix=...)`, `register_blueprint(..., url_prefix=...)` |
| Django | `urlpatterns` with `path`, `re_path` and `include(...)` across apps, `ROOT_URLCONF`; DRF `DefaultRouter`/`SimpleRouter.register` |
| Express | `app.get(...)`, `Router()`, `app.use("/prefix", router)` across files, `router.route(...).get(...).post(...)` |
| Fastify, Hono, Koa | routes on `fastify()`, `new Hono()` (incl. `.basePath`), `new Router({ prefix })` (`@koa/router`) |
| Next.js | `app/**/route.ts` exports (`GET`, `POST`, ...; route groups and private folders handled), `pages/api/**`; needs a `next.config.*` |
| NestJS | `@Controller(...)` and `@Get/@Post/...` decorators, `app.setGlobalPrefix(...)` |
| OpenAPI 3 | `paths` in `openapi.yaml`/`swagger.json`, with the server's base path |

| HTTP client | Calls Ghost reads |
|---|---|
| `fetch` (also `$fetch`, `ofetch`, `useFetch`) | URL plus `method` from an inline options object |
| axios | `axios.get/post/...`, `axios({ url, method })`, instances from `axios.create({ baseURL })` |
| requests | `requests.get/post/...`, `requests.request(...)`, `Session()` |
| httpx | `httpx.get/...`, `Client(base_url=...)` and `AsyncClient(base_url=...)`, including in `with` blocks |

URLs can be literals, template strings, f-strings, concatenations, or module-level constants.
Parts computed at runtime become a wildcard segment.

## Agents

Claude Code, OpenAI Codex and Cursor have hooks. Any other agent works through `AGENTS.md`,
`ghostapi check --changed`, pre-commit and CI. See [agents.md](agents.md).

## Not supported yet

- Go, Java/Kotlin, C#, Rust, Ruby, PHP, Swift and other languages
- Jupyter notebooks, and code inside Markdown
- Vue/Svelte single-file components (`.vue`, `.svelte`)
- Deno and JSR imports (`jsr:`, `npm:` specifiers)
- Private package registries: private packages are reported as unverifiable
- Monorepos with different versions per workspace
- External REST and GraphQL APIs as libraries: registry entries of kind `openapi`/`graphql` are
  accepted but reported as unverifiable
- Request and response body checks for your own endpoints
- Fastify plugins registered with `register()`, and routes built at runtime: declare those
  with `ghostapi provide`
