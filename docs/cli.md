# CLI reference

Every command takes `--root PATH` (default: the current directory) and `--help`.

## `ghostapi init`

Creates `.ghost/`, adds agent instructions, and installs hooks. It's idempotent and never
overwrites a file it didn't write.

| Option | Meaning |
|---|---|
| `--agent NAME` | `agents-md`, `claude`, `cursor`, `codex` or `git`. Repeatable. Default: detect from the project (`AGENTS.md` always; the others when `CLAUDE.md`/`.claude/`, `.cursor/`, `.codex/` or `.git/` exist). |

## `ghostapi check [PATHS...]`

Checks API usage against the pinned versions, the project's routes and the registry.

| Option | Meaning |
|---|---|
| `PATHS` | Files or folders to check. Default: the whole project. |
| `--format`, `-f` | `text` (default), `json`, `sarif`, `github` (workflow annotations). |
| `--fail-on` | `error` (default), `warning` or `never`. Exit 1 at this severity. |
| `--staged` | Only git-staged files (used by the pre-commit hook). |
| `--changed` | Only files git sees as modified or untracked: for agents and editors without hooks. |
| `--diff-base REF` | Only files changed since `REF` (pull requests). |
| `--baseline FILE` | Hide findings this baseline already accepts. |
| `--sarif-out FILE` | Also write SARIF to a file. |
| `--offline` / `--online` | Use only cached indexes. Default from `GHOSTAPI_OFFLINE`. |

Exit codes: `0` passed, `1` findings at `--fail-on` severity, `2` usage or setup error.

## `ghostapi show TARGET`

The current contract of one thing, checked before writing a call.

- A library symbol, such as `openai.OpenAI` or `react:useState`: its kind and signature in the
  pinned version, or the problem with it (removed, too new, never existed, plus "did you mean").
- An endpoint, such as `"POST /api/orders"`, `api/orders/{id}` or `/api/orders/42`: every
  definition (file, line, framework, handler), its owner, and its callers.

Takes `--format json` and `--offline`. Exits 1 when the target doesn't exist or has a problem.

## `ghostapi list`

Every endpoint the project serves (with owners and caller counts) and every external API in
the registry. Takes `--format json`.

## `ghostapi register`

Records that a file calls an external API.

| Option | Meaning |
|---|---|
| `--api` | Package or API name: `openai`, `react`, `stripe-rest`. |
| `--target` | Python `package.module.Name.attr`; npm `<module>:<export>.<member>`; REST `"METHOD /path"`. |
| `--file` | The file that makes the call. |
| `--kind` | `python` (default), `npm`, `openapi` or `graphql`. |
| `--symbol` | Enclosing function or class (survives line changes). |
| `--line` | Line hint. |
| `--version` | Default: from the lockfile. |
| `--module` | Python import root, when it differs from `--api` (`google.genai` for `google-genai`). |
| `--source` | Spec URL, for REST and GraphQL APIs. |
| `--owner` | Who owns this API (agent, team or person). |

`ghostapi check` prints the exact `register` command for every call it finds unregistered.

## `ghostapi unregister`

Removes registry locations: `--api` (required), `--target` (default: every target),
`--file` (default: every file).

## `ghostapi provide`

Declares an endpoint this project serves, to record an owner or a route the scanner can't
see (one built at runtime, for example).

| Option | Meaning |
|---|---|
| `--method` | `GET`, `POST`, `PUT`, `PATCH`, `DELETE`, `HEAD`, `OPTIONS` or `*`. |
| `--path` | `/api/orders/{id}`. The leading slash is optional. |
| `--owner`, `--description` | Who owns it and what it's for. |
| `--file`, `--symbol` | Where it is implemented. |

## `ghostapi unprovide`

Removes a declaration: `--method`, `--path`.

## `ghostapi baseline`

Accepts every current finding, so `check --baseline` reports only new ones.

| Option | Meaning |
|---|---|
| `--output FILE` | Where to write it. Default `.ghost/baseline.json`. |
| `--offline` / `--online` | Use only cached indexes. |

## `ghostapi hook AGENT`

The entry point the installed hooks call: `claude`, `codex`, `cursor-edit` or `cursor-stop`.
It reads the agent's hook payload on stdin. You don't normally run it yourself.
