# How it works

## Principles

1. **Never run what you analyse.** Ghost reads `.py`/`.pyi` files from wheels and `.d.ts` files
   from npm tarballs as text. It never installs, imports or executes a package.
2. **Version-exact.** Every verdict is about a specific `package@version` from your lockfile.
3. **No false certainty.** Where code can change at runtime (module `__getattr__`, index
   signatures, overloads, routers mounted in ways Ghost can't trace), Ghost stays quiet or
   lowers its confidence. With no source of truth, it reports `unverifiable`, never an error.
4. **Deterministic.** Findings come from comparing indexes, not from a language model.
5. **Built for agents.** Every finding has a stable rule id, an exact location and a fix hint,
   in text, JSON, SARIF or GitHub annotation form.

## Two kinds of API

### Libraries you depend on

```text
lockfile ──► pinned versions
source   ──► usage scan (Python: ast; JS/TS: tree-sitter)
                 │
registry ────────┤  (.ghost/apis: what the agents said they call)
                 ▼
         provider (PyPI / npm): download the pinned version's files,
         build a static index, cache it
                 │  symbol missing? look at other versions:
                 │    newer release has it   -> too-new (bisect for "added in")
                 │    older series had it    -> removed-in (bisect for "removed in")
                 │    same name elsewhere    -> import-path
                 │    nowhere                -> unknown-symbol + "did you mean"
                 ▼
         rule engine -> findings
```

- **Index.** For Python, [griffe](https://mkdocstrings.github.io/griffe/) builds the index.
  For JS/TS, Ghost's own `.d.ts` reader understands `package.json` exports, `export =`,
  namespaces, declaration merging and re-exports. Packages without bundled types are read from
  their pinned `@types/*` package, as the TypeScript compiler would.
- **Version history.** Only the pinned version is downloaded up front. Other versions are
  fetched only to explain a missing symbol, and the answers are cached on disk.
- **Registry.** Agents record the external calls they make (`ghostapi register`). The registry
  is never treated as proof that an API exists, because the agent that hallucinates a call also
  writes the entry. Ghost checks every entry, and compares the registry with the code to flag
  calls nobody recorded and entries the code no longer matches.

### Endpoints your project serves

```text
route definitions (FastAPI, Flask, Django, Express, Next.js, NestJS, OpenAPI, ...)
      │   composed across files: include_router, register_blueprint, app.use, include(), ...
      ▼
endpoint index ◄──── HTTP calls (fetch, axios, requests, httpx)
      │
      ├─ call matches no route          -> unknown-endpoint
      ├─ path exists, method doesn't    -> wrong-method
      ├─ same route defined twice       -> duplicate-endpoint / similar-endpoint
      └─ an edit removed a route that other code calls (compared with git HEAD)
                                        -> endpoint-removed, reported at each caller
```

A call counts as "to this project" only when it is a relative path, an absolute URL on an
internal host (localhost by default; configurable), or built from a configured base-URL name,
and when its first path segment is one your routes use. Calls to other services are ignored.

## Performance

- **First check of a package:** usually a few seconds, to download and index it. If symbols
  are missing, a few more versions may be fetched to explain them.
- **After that:** answered from the cache, in about a second or two for a typical project.
- **Hooks:** an edit that doesn't touch HTTP calls or routes skips the project-wide endpoint
  scan entirely.

## Where things live in the code

See the code layout section of [CLAUDE.md](../CLAUDE.md), which doubles as the contributor map.
