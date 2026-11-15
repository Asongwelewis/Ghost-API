"""Agent hook adapters and their installers."""

import json
import subprocess

from typer.testing import CliRunner

from ghostapi import bootstrap, hooks
from ghostapi.cli import app

runner = CliRunner()


def project(tmp_path):
    (tmp_path / "app.py").write_text("import openai\nopenai.OpenAI()\n", encoding="utf-8")
    (tmp_path / "clean.py").write_text("x = 1\n", encoding="utf-8")
    return tmp_path


def test_claude_hook_blocks_with_findings(tmp_path):
    root = project(tmp_path)
    payload = {"cwd": str(root), "tool_input": {"file_path": str(root / "app.py")}}
    result = hooks.run("claude", json.dumps(payload))
    assert result.code == 2 and "unregistered-api-call" in result.stderr
    payload["tool_input"]["file_path"] = str(root / "clean.py")
    assert hooks.run("claude", json.dumps(payload)).code == 0


def test_patch_files_reads_apply_patch_text():
    patch = (
        "*** Begin Patch\n*** Update File: src/a.py\n@@\n-x\n+y\n"
        "*** Add File: src/b.ts\n+export {}\n*** Delete File: old.py\n*** End Patch\n"
    )
    assert hooks.patch_files({"command": patch}) == ["src/a.py", "src/b.ts"]
    assert hooks.patch_files({"command": ["apply_patch", patch]}) == ["src/a.py", "src/b.ts"]
    assert hooks.patch_files({"file_path": "c.py"}) == ["c.py"]


def test_codex_hook_checks_patched_files(tmp_path):
    root = project(tmp_path)
    payload = {"cwd": str(root), "tool_input": {"command": "*** Update File: app.py\n"}}
    result = hooks.run("codex", json.dumps(payload))
    assert result.code == 2 and "app.py" in result.stderr


def test_cursor_records_edits_then_follows_up_on_stop(tmp_path):
    root = project(tmp_path)
    base = {"workspace_roots": [str(root)]}
    for name in ("app.py", "app.py", "clean.py", "notes.md"):
        assert (
            hooks.run("cursor-edit", json.dumps({**base, "file_path": str(root / name)})).code == 0
        )
    pending = (root / ".ghost" / ".pending").read_text(encoding="utf-8").splitlines()
    assert len(pending) == 2  # deduplicated, and only files Ghost can check

    result = hooks.run("cursor-stop", json.dumps({**base, "status": "completed"}))
    message = json.loads(result.stdout)["followup_message"]
    assert "unregistered-api-call" in message and "app.py" in message
    assert not (root / ".ghost" / ".pending").exists()
    # Nothing new edited: the next stop is quiet, so the loop ends.
    assert json.loads(hooks.run("cursor-stop", json.dumps(base)).stdout) == {}


def test_cursor_stop_after_aborted_turn_is_quiet(tmp_path):
    root = project(tmp_path)
    base = {"workspace_roots": [str(root)]}
    hooks.run("cursor-edit", json.dumps({**base, "file_path": str(root / "app.py")}))
    assert (
        json.loads(hooks.run("cursor-stop", json.dumps({**base, "status": "aborted"})).stdout) == {}
    )


def test_bad_payloads_never_break_the_agent():
    for raw in ("", "not json", "[1, 2]", "{}"):
        for agent in hooks.HOOKS:
            assert hooks.run(agent, raw).code == 0


def test_hook_command_and_legacy_alias(tmp_path):
    root = project(tmp_path)
    payload = json.dumps({"cwd": str(root), "tool_input": {"file_path": str(root / "app.py")}})
    assert runner.invoke(app, ["hook", "claude"], input=payload).exit_code == 2
    assert runner.invoke(app, ["check", "--hook"], input=payload).exit_code == 2


def test_init_installs_cursor_and_codex_hooks_idempotently(tmp_path):
    (tmp_path / ".cursor").mkdir()
    (tmp_path / ".cursor" / "hooks.json").write_text(
        json.dumps({"version": 1, "hooks": {"stop": [{"command": "./mine.sh"}]}}), encoding="utf-8"
    )
    (tmp_path / ".codex").mkdir()
    first = bootstrap.init(tmp_path)
    second = bootstrap.init(tmp_path)
    assert ".cursor/hooks.json (afterFileEdit + stop hooks)" in first.changed
    assert not any("hooks.json" in name for name in second.changed)

    cursor = json.loads((tmp_path / ".cursor" / "hooks.json").read_text(encoding="utf-8"))
    assert [h["command"] for h in cursor["hooks"]["stop"]] == [
        "./mine.sh",
        "ghostapi hook cursor-stop",
    ]
    assert cursor["hooks"]["afterFileEdit"] == [{"command": "ghostapi hook cursor-edit"}]
    codex = json.loads((tmp_path / ".codex" / "hooks.json").read_text(encoding="utf-8"))
    [group] = codex["hooks"]["PostToolUse"]
    assert group["matcher"] == "apply_patch|Edit|Write"
    assert group["hooks"][0]["command"] == "ghostapi hook codex"
    assert (tmp_path / ".ghost" / ".gitignore").read_text(encoding="utf-8").count(".pending") == 1


def test_check_changed_uses_git_status(tmp_path):
    root = project(tmp_path)
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false"]
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run([*git, "add", "clean.py"], cwd=root, check=True)
    subprocess.run([*git, "commit", "-qm", "init"], cwd=root, check=True)
    result = runner.invoke(app, ["check", "--root", str(root), "--changed", "-f", "json"])
    files = {f["file"] for f in json.loads(result.output)["findings"]}
    assert files == {"app.py"}  # untracked; clean.py is committed and unchanged
