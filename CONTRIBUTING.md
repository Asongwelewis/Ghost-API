# Contributing to Ghost API

Thanks for helping. Bug reports, false-positive reports, documentation fixes and code are all
welcome.

## Ground rules

Ghost's whole value is that agents (and people) can trust its findings. So:

- **Precision over recall.** A false error costs more than a missed one. When the code can't
  be sure, it stays silent, lowers its confidence, or reports `unverifiable`.
- **Never run what you analyse.** Packages are read as text from their published files, never
  installed, imported or executed.
- **Version-exact.** Every verdict is about the version the project pins.
- **Deterministic.** Findings come from comparing indexes, not from a language model.

## Reporting

- **Wrong finding** (Ghost flagged correct code): use the *False positive* issue form. These
  are the most valuable reports we get. Include the package and version, or the framework, plus
  a minimal snippet.
- **Missed problem** or a crash: use the *Bug report* form.
- **Security issue:** don't open an issue. See [SECURITY.md](SECURITY.md).

## Development setup

You need [uv](https://docs.astral.sh/uv/) and git.

```sh
git clone https://github.com/Asongwelewis/Ghost-API.git
cd Ghost-API
uv sync
uv run pytest                     # offline; the suite never touches the network
uv run ruff check . && uv run ruff format --check .
uv run pyright
GHOSTAPI_NETWORK_TESTS=1 uv run pytest -m network   # optional: live PyPI and npm
```

On Windows with Git Bash, arguments that start with `/` are rewritten into Windows paths. Pass
endpoint paths as `api/orders`, or set `MSYS_NO_PATHCONV=1`.

## Making a change

1. Fork the repository and branch from `dev`: `feat/<topic>`, `fix/<topic>` or `docs/<topic>`.
2. Keep each pull request to one topic, and add tests:
   - every new rule needs a fixture showing it fires;
   - every new detection needs a fixture of **correct** code showing it stays quiet.
3. Run the commands above. CI runs them on Linux, macOS and Windows with Python 3.12 to 3.14.
4. Open the pull request against `dev`, and fill in the template.

Commit messages: an imperative subject line ("Add Hono basePath support"), then a body
explaining *why* when the change isn't obvious.

## Where things go

[CLAUDE.md](CLAUDE.md) has the full code map. The usual entry points:

| You want to... | Look at |
|---|---|
| support a new web framework's routes or an HTTP client | `src/ghostapi/endpoints/python.py` or `javascript.py`; cross-file composition in `endpoints/index.py` |
| support a new package registry | a `providers/` module subclassing `VersionedProvider` (`providers/history.py`) |
| read more of a package's API (Python or `.d.ts`) | `providers/pysurface.py`, `providers/dts.py` |
| add a rule | `engine.py` (library calls), `endpoints/check.py` (endpoints), `reconcile.py` (registry) |
| support a new agent's hooks | `hooks.py` and `bootstrap.py`, based on the agent's official hook docs |

Adding an API type means adding a provider (and an extractor if it can be scanned). It should
not need new rules.

## Releases (maintainers)

`main` holds released code only. Releases go `dev` → pull request → `main`, then a `vX.Y.Z`
tag matching the version in `pyproject.toml`. The release workflow tests, builds and publishes
to PyPI through trusted publishing, after a maintainer approves the `pypi` environment.

## License

By contributing, you agree that your contributions are licensed under the project's
[MIT License](LICENSE). There is no CLA.
