# Ghost API
  Catches hallucinated, removed and too-new library calls in AI-written code, checked against the exact versions in your lockfile. A CLI (with agent hooks and a GitHub Action) for Python and JavaScript/TypeScript.

## Quickstart

```sh
uv tool install ghostapi0        # or: pipx install ghostapi0 (the command is `ghostapi`)
cd your-project
ghostapi init                    # .ghost/ registry, agent instructions, check hooks
ghostapi check                   # text | --format json | --format sarif
```

Your coding agent records each external API call with `ghostapi register`; Ghost checks every
recorded and scanned call against the versions pinned in your lockfile, and flags calls that are
missing from the registry or entries that no longer match the code.

Example on a project pinned to `openai==1.40.0`:

```
app.py:5:14 error bad-parameter [high] `openai.OpenAI` has no parameter `timout` in openai 1.40.0.
    fix: Did you mean `timeout`?
app.py:6:12 error removed-in [high] `openai.ChatCompletion` was removed in openai 1.0.0; you have 1.40.0.
app.py:9:12 error unknown-symbol [high] `openai.OpenAl` does not exist in any known version of openai.
    fix: Did you mean `openai.OpenAI`?
```

Packages are read statically from their published files (nothing is installed or executed) and
cached locally. The first check of a package downloads it; later checks are instant and work
offline (`--offline`).

The same works for JavaScript and TypeScript, against npm packages (or their `@types/*`):

```
src/App.tsx:1:1 error too-new [high] `react:useActionState` was added in @types/react 19.0.0; you have 18.3.12.
src/App.tsx:6:31 error unknown-symbol [high] `zod:z.strng` does not exist in any known version of zod.
    fix: Did you mean `zod:z.string`?
src/App.tsx:17:9 error arity [high] `axios:default.get` takes at most 2 positional argument(s) in axios 1.7.9; got 3.
```

Lockfiles: `uv.lock`, `poetry.lock`, `requirements.txt`, `pyproject.toml`, `package-lock.json`,
`pnpm-lock.yaml`, `yarn.lock`.

> Status: early. Python (PyPI) and JavaScript/TypeScript (npm) are checked; REST and GraphQL
> providers are next. Anything Ghost can't verify is reported as "unverifiable", never as an error.

## Contributing

Branch from `dev`, open PRs into `dev`. `main` only receives release merges from `dev`, and tags
`vX.Y.Z` on `main` publish to PyPI.

```sh
uv sync
uv run pytest && uv run ruff check . && uv run pyright
```
