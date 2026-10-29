import json

from typer.testing import CliRunner

from ghostapi.cli import app

runner = CliRunner()


def test_init_is_idempotent_and_keeps_user_content(tmp_path):
    (tmp_path / "CLAUDE.md").write_text("# My project\n", encoding="utf-8")
    (tmp_path / ".git" / "hooks").mkdir(parents=True)

    first = runner.invoke(app, ["init", "--root", str(tmp_path)])
    second = runner.invoke(app, ["init", "--root", str(tmp_path)])
    assert first.exit_code == 0 and second.exit_code == 0
    assert "updated" not in second.output

    claude_md = (tmp_path / "CLAUDE.md").read_text(encoding="utf-8")
    assert claude_md.startswith("# My project")
    assert claude_md.count("ghostapi:start") == 1
    assert (tmp_path / "AGENTS.md").exists()
    settings = json.loads((tmp_path / ".claude" / "settings.json").read_text(encoding="utf-8"))
    assert settings["hooks"]["PostToolUse"][0]["hooks"][0]["command"] == "ghostapi check --hook"
    assert "ghostapi check --staged" in (tmp_path / ".git" / "hooks" / "pre-commit").read_text()


def test_init_leaves_foreign_pre_commit_alone(tmp_path):
    hook = tmp_path / ".git" / "hooks" / "pre-commit"
    hook.parent.mkdir(parents=True)
    hook.write_text("#!/bin/sh\nmake lint\n", encoding="utf-8")
    result = runner.invoke(app, ["init", "--root", str(tmp_path), "--agent", "git"])
    assert "existing hook" in result.output
    assert hook.read_text(encoding="utf-8") == "#!/bin/sh\nmake lint\n"


def test_register_then_check_json(tmp_path):
    (tmp_path / "app.py").write_text("import openai\nopenai.OpenAI()\n", encoding="utf-8")
    result = runner.invoke(app, ["check", "--root", str(tmp_path), "--format", "json"])
    assert result.exit_code == 0  # warnings only; default --fail-on error
    assert json.loads(result.output)["findings"][0]["rule"] == "unregistered-api-call"

    result = runner.invoke(
        app,
        ["register", "--root", str(tmp_path), "--api", "openai", "--target", "openai.OpenAI",
         "--file", "app.py", "--line", "2"],
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    result = runner.invoke(app, ["check", "--root", str(tmp_path), "--fail-on", "warning"])
    assert result.exit_code == 0, result.output


def test_register_rejects_missing_file(tmp_path):
    result = runner.invoke(
        app, ["register", "--root", str(tmp_path), "--api", "x", "--target", "x", "--file", "no.py"]
    )
    assert result.exit_code == 2


def test_sarif_output(tmp_path):
    (tmp_path / "app.py").write_text("import openai\n", encoding="utf-8")
    result = runner.invoke(app, ["check", "--root", str(tmp_path), "-f", "sarif"])
    sarif = json.loads(result.output)
    [res] = sarif["runs"][0]["results"]
    assert res["ruleId"] == "unregistered-api-call"
    assert res["locations"][0]["physicalLocation"]["region"]["startLine"] == 1


def test_hook_feeds_findings_back_with_exit_2(tmp_path):
    app_py = tmp_path / "app.py"
    app_py.write_text("import openai\n", encoding="utf-8")
    payload = {"cwd": str(tmp_path), "tool_input": {"file_path": str(app_py)}}
    result = runner.invoke(app, ["check", "--hook"], input=json.dumps(payload))
    assert result.exit_code == 2
    assert "unregistered-api-call" in result.output

    payload["tool_input"]["file_path"] = str(tmp_path / "README.md")
    assert runner.invoke(app, ["check", "--hook"], input=json.dumps(payload)).exit_code == 0
