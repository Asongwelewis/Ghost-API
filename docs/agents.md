# Agents and hooks

Ghost reaches an agent in two ways:

- **Instructions:** `ghostapi init` adds a short block to `AGENTS.md` (and `CLAUDE.md` or Cursor
  rules when present). It tells the agent to look contracts up with `ghostapi show`, record what
  it uses and provides, and run `ghostapi check` before finishing. The block sits between
  `<!-- ghostapi:start -->` and `<!-- ghostapi:end -->` markers and is refreshed in place; the
  rest of the file is left alone.
- **Hooks:** these run without the agent having to remember anything, and send findings back
  through the agent's own feedback channel.

| Agent | Config file | Event | Feedback |
|---|---|---|---|
| Claude Code | `.claude/settings.json` | `PostToolUse` on `Edit\|MultiEdit\|Write` | exit 2 with findings on stderr, which Claude Code passes to the model |
| OpenAI Codex | `.codex/hooks.json` | `PostToolUse` on `apply_patch\|Edit\|Write` | exit 2 with findings on stderr |
| Cursor | `.cursor/hooks.json` | `afterFileEdit` records the file; `stop` checks every file edited this turn | `followup_message`, which Cursor sends as the agent's next prompt |
| git | `.git/hooks/pre-commit` | `ghostapi check --staged` | blocks the commit |

Notes:

- **Codex** runs a project's hooks only after you trust the project's `.codex/` folder in Codex.
  Edited paths are read from the `apply_patch` text.
- **Cursor**'s `afterFileEdit` can't reply to the agent, which is why the check happens on
  `stop`. Cursor limits follow-up loops (5 by default). An aborted turn produces no follow-up.
- **Timeouts:** installed hooks allow 180 seconds, because the first check of a new package
  downloads it. Later checks take a second or two.
- **Bad payloads:** a hook that can't understand its input exits 0, so Ghost never blocks an
  agent because of its own problem.
- **Existing configs:** `init` merges into existing JSON configs, refuses to touch a config that
  isn't valid JSON, and leaves an existing third-party pre-commit hook alone. In that case it
  tells you to add `ghostapi check --staged` yourself.

## Agents without hooks

GitHub Copilot, Windsurf and anything else can still use Ghost:

- read the `AGENTS.md` instructions (many agents do);
- run `ghostapi check --changed` after editing;
- be caught by the git pre-commit hook or the GitHub Action, which no agent can skip.

Gemini CLI isn't wired up because it was replaced by Antigravity CLI in June 2026.
Copilot's and Windsurf's documented hook payloads don't include the edited file's path, so
there is nothing reliable to check yet.
