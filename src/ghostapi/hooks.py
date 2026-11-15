"""Agent hook adapters: turn each agent's hook payload into a check, and findings into feedback
that agent actually sees. The payloads and feedback channels differ per agent; the check is the
same everywhere.

- Claude Code `PostToolUse` and Codex `PostToolUse`: check the edited files; exit 2 with the
  findings on stderr goes back to the model.
- Cursor: `afterFileEdit` can't reply, so it only records the file; the `stop` hook checks the
  recorded files and answers with `followup_message`, which Cursor sends as the next prompt.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ghostapi.checker import Report, run_check
from ghostapi.models import Severity
from ghostapi.output import to_text
from ghostapi.project import is_checkable
from ghostapi.registry import GHOST_DIR, RegistryError

PENDING = ".pending"  # inside .ghost/: files edited since the last Cursor stop
_PATCH_FILE = re.compile(r"^\*\*\* (?:Add File|Update File|Move to): (.+?)\s*$", re.MULTILINE)
_FEEDBACK_HEAD = "Ghost API found problems in files you just edited:\n\n"
_FEEDBACK_TAIL = (
    "\n\nFix the errors. Register calls flagged as unregistered with the `fix:` command shown."
)


@dataclass
class HookResult:
    code: int = 0
    stdout: str = ""
    stderr: str = ""


def _paths(root: Path, files: Iterable[str]) -> list[Path]:
    paths = []
    for name in files:
        path = Path(name)
        path = path if path.is_absolute() else root / path
        if is_checkable(path) and path not in paths:
            paths.append(path)
    return paths


def _feedback(root: Path, files: list[Path]) -> str | None:
    """Findings worth interrupting the agent for, as text; None when the files are clean."""
    if not files:
        return None
    try:
        report: Report = run_check(root, files=files)
    except RegistryError as exc:
        return f"Ghost API: the registry in {GHOST_DIR}/ is invalid: {exc}"
    if not any(f.severity in (Severity.ERROR, Severity.WARNING) for f in report.findings):
        return None
    return _FEEDBACK_HEAD + to_text(report) + _FEEDBACK_TAIL


def _blocking(root: Path, files: list[Path]) -> HookResult:
    message = _feedback(root, files)
    return HookResult(code=2, stderr=message) if message else HookResult()


def claude(payload: dict[str, Any]) -> HookResult:
    """Claude Code PostToolUse (Edit|MultiEdit|Write): `tool_input.file_path`."""
    root = Path(payload.get("cwd") or ".")
    edited = (payload.get("tool_input") or {}).get("file_path")
    return _blocking(root, _paths(root, [edited] if isinstance(edited, str) else []))


def patch_files(tool_input: dict[str, Any]) -> list[str]:
    """Files an `apply_patch` call touches; also accepts a plain `file_path`."""
    if isinstance(tool_input.get("file_path"), str):
        return [tool_input["file_path"]]
    command = tool_input.get("command") or tool_input.get("patch") or ""
    if isinstance(command, list):
        command = "\n".join(str(part) for part in command)
    return _PATCH_FILE.findall(str(command))


def codex(payload: dict[str, Any]) -> HookResult:
    """Codex PostToolUse (apply_patch): paths come from the patch text in `tool_input`."""
    root = Path(payload.get("cwd") or ".")
    return _blocking(root, _paths(root, patch_files(payload.get("tool_input") or {})))


def _cursor_root(payload: dict[str, Any]) -> Path:
    roots = payload.get("workspace_roots")
    if isinstance(roots, list) and roots and isinstance(roots[0], str):
        return Path(roots[0])
    return Path(os.environ.get("CURSOR_PROJECT_DIR") or os.getcwd())


def cursor_edit(payload: dict[str, Any]) -> HookResult:
    """Cursor afterFileEdit: remember the file for the stop hook (this event can't reply)."""
    root = _cursor_root(payload)
    edited = payload.get("file_path")
    if not isinstance(edited, str) or not _paths(root, [edited]):
        return HookResult()
    pending = root / GHOST_DIR / PENDING
    pending.parent.mkdir(parents=True, exist_ok=True)
    known = set(pending.read_text(encoding="utf-8").splitlines()) if pending.exists() else set()
    if edited not in known:
        with pending.open("a", encoding="utf-8") as handle:
            handle.write(edited + "\n")
    return HookResult()


def cursor_stop(payload: dict[str, Any]) -> HookResult:
    """Cursor stop: check every file edited this turn; reply with a follow-up if needed."""
    root = _cursor_root(payload)
    pending = root / GHOST_DIR / PENDING
    if not pending.exists():
        return HookResult(stdout="{}")
    files = _paths(root, pending.read_text(encoding="utf-8").splitlines())
    pending.unlink()
    if payload.get("status") not in (None, "completed"):
        return HookResult(stdout="{}")  # aborted or failed turn: don't start another one
    message = _feedback(root, files)
    return HookResult(stdout=json.dumps({"followup_message": message} if message else {}))


HOOKS: dict[str, Callable[[dict[str, Any]], HookResult]] = {
    "claude": claude,
    "codex": codex,
    "cursor-edit": cursor_edit,
    "cursor-stop": cursor_stop,
}


def run(agent: str, raw: str) -> HookResult:
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        return HookResult()  # never break the agent over a payload we don't understand
    if not isinstance(payload, dict):
        return HookResult()
    return HOOKS[agent](payload)
