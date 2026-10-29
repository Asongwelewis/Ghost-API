"""`ghostapi init`: set up the registry, tell agents how to use it, and install check hooks.

Every step is idempotent and never overwrites content it didn't write.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from ghostapi.registry import GHOST_DIR, apis_dir

Agent = Literal["agents-md", "claude", "cursor", "git"]

START, END = "<!-- ghostapi:start -->", "<!-- ghostapi:end -->"
HOOK_COMMAND = "ghostapi check --hook"

INSTRUCTIONS = """\
## External APIs (Ghost API)
This project records every external API call in `.ghost/apis/` and checks it against the versions
actually pinned in the lockfile.
- When you add or change code that calls an external library, SDK, REST endpoint or GraphQL
  operation, record it:
  `ghostapi register --api <package> --kind <python|npm|openapi|graphql> --target <dotted.path or "METHOD /path"> --file <path> --symbol <function-or-class> --line <n>`
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


def install_claude_hook(root: Path) -> bool:
    path = root / ".claude" / "settings.json"
    settings = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    post = settings.setdefault("hooks", {}).setdefault("PostToolUse", [])
    for group in post:
        if any(HOOK_COMMAND in h.get("command", "") for h in group.get("hooks", [])):
            return False
    # The first check of a new package downloads and indexes it, so allow more than the default.
    hook = {"type": "command", "command": HOOK_COMMAND, "timeout": 180}
    post.append({"matcher": "Edit|MultiEdit|Write", "hooks": [hook]})
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    return True


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
    readme = root / GHOST_DIR / "README.md"
    if not readme.exists():
        readme.write_text(GHOST_README, encoding="utf-8")
        result.changed.append(f"{GHOST_DIR}/README.md")

    targets: dict[Agent, tuple[str, str]] = {
        "agents-md": ("AGENTS.md", ""),
        "claude": ("CLAUDE.md", ""),
        "cursor": (".cursor/rules/ghostapi.mdc", CURSOR_HEADER),
    }
    for agent, (name, header) in targets.items():
        if agent in agents:
            changed = write_block(root / name, INSTRUCTIONS, header)
            (result.changed if changed else result.skipped).append(name)

    if "claude" in agents:
        name = ".claude/settings.json (PostToolUse hook)"
        (result.changed if install_claude_hook(root) else result.skipped).append(name)

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
