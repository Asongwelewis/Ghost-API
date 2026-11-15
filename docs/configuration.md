# Configuration

Ghost needs no configuration. Everything below is optional.

## `.ghost/` (commit it)

```text
.ghost/
  README.md                       # what this folder is
  .gitignore                      # ignores the lock and scratch files below
  config.yaml                     # optional settings (below)
  baseline.json                   # optional: accepted findings (ghostapi baseline)
  apis/<api>/api.yaml             # an external API: kind, version, module, source, owner
  apis/<api>/calls/<target>.yaml  # one called target and the places that call it
  provides/<endpoint>.yaml        # an endpoint this project serves: owner, description
  .lock, .pending                 # local only, ignored
```

Change it through the CLI (`register`, `unregister`, `provide`, `unprovide`), not by hand:
the CLI holds `.ghost/.lock` and writes atomically, so parallel agents never lose updates.
Ghost 0.1 stored each API in a single `apis/<api>.yaml`; those files are still read and are
converted on the next write.

## `.ghost/config.yaml`

```yaml
http:
  # Absolute URLs on these hosts are this project's own API. localhost, 127.0.0.1,
  # 0.0.0.0 and ::1 are always included.
  internal_hosts: [api.example.com, staging.example.com]

  # Expressions that hold this project's base URL, so `${API_URL}/orders` counts as ours.
  # Write them as they appear in code.
  base_url_names: [API_URL, process.env.NEXT_PUBLIC_API_URL, settings.API_BASE]

  # OpenAPI files describing this project's API. Default: openapi.(yaml|yml|json) or
  # swagger.(yaml|json) at the root or in docs/, api/, spec/ or openapi/.
  openapi: [docs/openapi.yaml]
```

## Environment variables

| Variable | Meaning |
|---|---|
| `GHOSTAPI_CACHE_DIR` | Where indexes are cached. Default `%LOCALAPPDATA%\ghostapi\Cache` on Windows, `$XDG_CACHE_HOME/ghostapi` or `~/.cache/ghostapi` elsewhere. |
| `GHOSTAPI_OFFLINE` | `1` to use only the cache (same as `--offline`). |
| `GHOSTAPI_NETWORK_TESTS` | `1` to run the live PyPI/npm tests (contributors). |

## Lockfiles

Python, in order of preference: `uv.lock`, `poetry.lock`, `requirements.txt` (`==` pins),
`pyproject.toml` (`==` pins). npm: `package-lock.json`, `pnpm-lock.yaml`, `yarn.lock` (v1 and
Berry). A yarn package resolved to several versions is skipped, because Ghost can't tell which
one your code gets.

Bare import specifiers count as npm packages only when `package.json` or the lockfile lists
them, so `tsconfig` path aliases like `components/button` are never mistaken for packages.
