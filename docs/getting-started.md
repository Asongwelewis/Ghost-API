# Getting started

## Requirements

- Python 3.12 or newer, to run Ghost itself. The projects you check can use any Python or
  Node.js version.
- A lockfile in the project you check. Ghost judges every call against the version you pin,
  never against "latest", so a package without a pin is reported as unverifiable.
- Network access the first time a package is checked. After that, the index is cached and
  `--offline` works.

## Install

```sh
uv tool install ghostapi0        # or: pipx install ghostapi0
ghostapi --version
```

The package is called `ghostapi0` on PyPI, and the command it installs is `ghostapi`. Until the
first release is published, install from source:

```sh
uv tool install git+https://github.com/Asongwelewis/Ghost-API@dev
```

## Set up a project

```sh
cd your-project
ghostapi init
```

`init` is safe to run more than once, and it never overwrites files it didn't write. It:

- creates `.ghost/`, the registry where agents record the APIs they use and provide (commit it);
- adds a short instruction block to `AGENTS.md`, plus `CLAUDE.md` and Cursor rules when those
  agents are present, telling the agent how to use Ghost;
- installs hooks for the agents it detects (Claude Code, Codex, Cursor) and a git pre-commit hook.

Choose the agents yourself with `--agent` (repeatable): `ghostapi init --agent claude --agent git`.

## Check

```sh
ghostapi check                         # the whole project
ghostapi check src/api                 # some files or folders
ghostapi check --changed               # files git sees as modified or new
ghostapi check --format json           # for agents and scripts
ghostapi check --fail-on warning       # exit 1 on warnings too (default: errors only)
```

Each finding gives a location, a rule, a confidence level, a message and usually a `fix:` line:

```text
app.py:5:14 error bad-parameter [high] `openai.OpenAI` has no parameter `timout` in openai 1.40.0.
    fix: Did you mean `timeout`?
```

## Look things up

Agents should check a contract before writing a call:

```sh
ghostapi show openai.OpenAI              # signature in the pinned version, or why it's wrong
ghostapi show "react:useEffect"          # npm symbols are written <module>:<export>.<member>
ghostapi show "POST /api/orders"         # this project's endpoint: definitions, owner, callers
ghostapi list                            # everything the project serves and uses
```

On Windows Git Bash, write endpoint paths without the leading slash (`api/orders`), because
Git Bash rewrites arguments that start with `/`.

## Next steps

- Wire Ghost into CI: [ci.md](ci.md)
- Using several agents at once: [multi-agent.md](multi-agent.md)
- What each finding means: [rules.md](rules.md)
