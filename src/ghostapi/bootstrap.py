"""`ghostapi init`: set up the registry, tell agents how to use it, and install check hooks.

Every step is idempotent and never overwrites content it didn't write.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from ghostapi.registry import GHOST_DIR, apis_dir

Agent = Literal["agents-md", "claude", "cursor", "codex", "git"]
AGENTS: tuple[Agent, ...] = ("agents-md", "claude", "cursor", "codex", "git")

START, END = "<!-- ghostapi:start -->", "<!-- ghostapi:end -->"
HOOK_TIMEOUT = 180  # the first check of a new package downloads and indexes it
CLAUDE_HOOK = "ghostapi hook claude"
CURSOR_EDIT_HOOK = "ghostapi hook cursor-edit"
CURSOR_STOP_HOOK = "ghostapi hook cursor-stop"
CODEX_HOOK = "ghostapi hook codex"

INSTRUCTIONS = """\
## External APIs (Ghost API)
This project records every external API call in `.ghost/apis/` and checks it against the versions
actually pinned in the lockfile.
- When you add or change code that calls an external library, SDK, REST endpoint or GraphQL
  operation, record it:
  `ghostapi register --api <package> --kind <python|npm|openapi|graphql> --target <target> --file <path> --symbol <function-or-class> --line <n>`
  Targets: Python `package.module.Name.attr`; npm `<module>:<export>.<member>` (e.g. `react:useState`, `axios:default.get`); REST `"METHOD /path"`.
  `ghostapi check` prints the exact `register` command for any call it finds unregistered.
- When you remove or move such a call, run `ghostapi unregister --api <package> --target <target> --file <path>` and register the new location.
- Before saying the work is done, run `ghostapi check` and fix every error. Don't dismiss a finding unless you can show it is wrong.
"""

CURSOR_HEADER = "---\ndescription: Ghost API registry rules\nalwaysApply: true\n---\n"
PRE_COMMIT = "#!/bin/sh\n# installed by ghostapi init\nexec ghostapi check --staged\n"
GHOST_README = """\
# .ghost

Registry of the external APIs this project calls, one YAML file per API, maintained with
`ghostapi register` / `ghostapi unregister`. Commit this folder. Run `ghostapi check` to verify.
"""
GHOST_GITIGNORE = "# scratch state for agent hooks\n.pending\n"


@dataclass
class InitResult:
    changed: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


def detect_agents(root: Path) -> list[Agent]:
    agents: list[Agent] = ["agents-md"]
    if (root / "CLAUDE.md").exists() or (root / ".claude").is_dir():
        agents.append("claude")
    if (root / ".cursor").is_dir():
        agents.append("cursor")
    if (root / ".codex").is_dir():
        agents.append("codex")
    if (root / ".git").is_dir():
        agents.append("git")
    return agents


def write_block(path: Path, body: str, header: str = "") -> bool:
    """Insert or refresh the marked Ghost block in `path`. Returns True when the file changed."""
    block = f"{START}\n{body.rstrip()}\n{END}"
    if path.exists():
        text = path.read_text(encoding="utf-8")
        pattern = re.compile(re.escape(START) + r".*?" + re.escape(END), re.DOTALL)
        if pattern.search(text):
            new = pattern.sub(lambda _: block, text)
        else:
            new = text.rstrip() + "\n\n" + block + "\n"
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        new = header + block + "\n"
        text = None
    if new == text:
        return False
    path.write_text(new, encoding="utf-8")
    return True


def _load_json(path: Path) -> dict[str, Any]:
    """Existing JSON config, or {}. Raises json.JSONDecodeError rather than overwrite a bad file."""
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise json.JSONDecodeError("expected a JSON object", "", 0)
    return data


def _save_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _has_ghost_command(groups: list[Any]) -> bool:
    for group in groups:
        entries = group.get("hooks", [group]) if isinstance(group, dict) else []
        if any("ghostapi" in str(h.get("command", "")) for h in entries if isinstance(h, dict)):
            return True
    return False


def _install_post_tool_hook(path: Path, matcher: str, command: str) -> bool:
    """Claude Code and Codex share this hooks layout."""
    settings = _load_json(path)
    post = settings.setdefault("hooks", {}).setdefault("PostToolUse", [])
    if _has_ghost_command(post):
        return False
    hook = {"type": "command", "command": command, "timeout": HOOK_TIMEOUT}
    post.append({"matcher": matcher, "hooks": [hook]})
    _save_json(path, settings)
    return True


def install_claude_hook(root: Path) -> bool:
    path = root / ".claude" / "settings.json"
    return _install_post_tool_hook(path, "Edit|MultiEdit|Write", CLAUDE_HOOK)


def install_codex_hook(root: Path) -> bool:
    # Codex loads project hooks only after the user trusts the project's .codex/ folder.
    return _install_post_tool_hook(
        root / ".codex" / "hooks.json", "apply_patch|Edit|Write", CODEX_HOOK
    )


def install_cursor_hooks(root: Path) -> bool:
    path = root / ".cursor" / "hooks.json"
    config = _load_json(path)
    config.setdefault("version", 1)
    hooks = config.setdefault("hooks", {})
    changed = False
    for event, command, extra in (
        ("afterFileEdit", CURSOR_EDIT_HOOK, {}),
        ("stop", CURSOR_STOP_HOOK, {"timeout": HOOK_TIMEOUT}),
    ):
        entries = hooks.setdefault(event, [])
        if not _has_ghost_command(entries):
            entries.append({"command": command, **extra})
            changed = True
    if changed:
        _save_json(path, config)
    return changed


def install_pre_commit(root: Path) -> bool | None:
    """True if installed, False if already present, None if a foreign hook is in the way."""
    path = root / ".git" / "hooks" / "pre-commit"
    if path.exists():
        return False if "ghostapi" in path.read_text(encoding="utf-8", errors="replace") else None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(PRE_COMMIT, encoding="utf-8", newline="\n")
    path.chmod(0o755)
    return True


def init(root: Path, agents: list[Agent] | None = None) -> InitResult:
    result = InitResult()
    agents = detect_agents(root) if agents is None else agents

    apis_dir(root).mkdir(parents=True, exist_ok=True)
    for name, content in (("README.md", GHOST_README), (".gitignore", GHOST_GITIGNORE)):
        path = root / GHOST_DIR / name
        if not path.exists():
            path.write_text(content, encoding="utf-8")
            result.changed.append(f"{GHOST_DIR}/{name}")

    targets: dict[Agent, tuple[str, str]] = {
        "agents-md": ("AGENTS.md", ""),
        "claude": ("CLAUDE.md", ""),
        "cursor": (".cursor/rules/ghostapi.mdc", CURSOR_HEADER),
    }
    for agent, (name, header) in targets.items():
        if agent in agents:
            changed = write_block(root / name, INSTRUCTIONS, header)
            (result.changed if changed else result.skipped).append(name)

    installers: tuple[tuple[Agent, str, Callable[[Path], bool]], ...] = (
        ("claude", ".claude/settings.json (PostToolUse hook)", install_claude_hook),
        ("cursor", ".cursor/hooks.json (afterFileEdit + stop hooks)", install_cursor_hooks),
        (
            "codex",
            ".codex/hooks.json (PostToolUse; trust the project in Codex)",
            install_codex_hook,
        ),
    )
    for agent, name, install in installers:
        if agent in agents:
            (result.changed if install(root) else result.skipped).append(name)

    if "git" in agents:
        installed = install_pre_commit(root)
        if installed:
            result.changed.append(".git/hooks/pre-commit")
        elif installed is None:
            result.skipped.append(
                ".git/hooks/pre-commit (existing hook; add `ghostapi check --staged` to it)"
            )
        else:
            result.skipped.append(".git/hooks/pre-commit")
    return result
