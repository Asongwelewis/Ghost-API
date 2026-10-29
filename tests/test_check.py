from ghostapi import registry
from ghostapi.checker import run_check
from ghostapi.models import ApiHistory, ApiSymbol
from ghostapi.providers import StaticProvider
from ghostapi.registry import Location


def make_project(tmp_path, source: str):
    (tmp_path / "uv.lock").write_text(
        '[[package]]\nname = "openai"\nversion = "1.40.0"\n', encoding="utf-8"
    )
    (tmp_path / "app.py").write_text(source, encoding="utf-8")
    return tmp_path


def rules(report):
    return sorted(f.rule for f in report.findings)


def test_unregistered_call_reports_deepest_target_with_fix_command(tmp_path):
    root = make_project(
        tmp_path, "import openai\n\ndef go():\n    openai.ChatCompletion.create()\n"
    )
    report = run_check(root)
    [f] = report.findings
    assert f.rule == "unregistered-api-call"
    assert f.target == "openai.ChatCompletion.create"
    assert f.fix is not None and "--symbol go --line 4" in f.fix
    assert report.unverifiable == 2  # no provider configured yet


def test_registered_call_is_quiet(tmp_path):
    root = make_project(
        tmp_path, "import openai\n\ndef go():\n    openai.ChatCompletion.create()\n"
    )
    registry.register(
        root,
        api="openai",
        kind="python",
        target="openai.ChatCompletion.create",
        location=Location(file="app.py", symbol="go", line_hint=4),
    )
    assert run_check(root).findings == []


def test_stale_entries(tmp_path):
    root = make_project(tmp_path, "def go():\n    pass\n")
    for loc in (
        Location(file="app.py", symbol="go"),  # file no longer imports openai
        Location(file="app.py", symbol="gone"),  # symbol removed
        Location(file="deleted.py"),  # file removed
    ):
        registry.register(root, api="openai", kind="python", target="openai.OpenAI", location=loc)
    assert rules(run_check(root)) == ["stale-entry"] * 3


def test_registry_version_mismatch(tmp_path):
    root = make_project(tmp_path, "import openai\n")
    registry.register(
        root,
        api="openai",
        kind="python",
        target="openai",
        location=Location(file="app.py"),
        version="0.28.0",
    )
    assert rules(run_check(root)) == ["registry-version-mismatch"]


def test_engine_runs_when_a_provider_has_history(tmp_path):
    root = make_project(tmp_path, "import openai\nopenai.ChatCompletion.create()\n")
    registry.register(
        root,
        api="openai",
        kind="python",
        target="openai.ChatCompletion.create",
        location=Location(file="app.py"),
    )
    history = ApiHistory(
        "openai",
        {
            "openai": ApiSymbol("openai", "module"),
            "openai.ChatCompletion": ApiSymbol("openai.ChatCompletion", "class"),
            "openai.ChatCompletion.create": ApiSymbol(
                "openai.ChatCompletion.create", "function", removed_in="1.0.0"
            ),
        },
    )
    report = run_check(root, providers={"python": StaticProvider([history])})
    assert rules(report) == ["removed-in"]
    assert report.unverifiable == 0

    registry.unregister(root, api="openai")
    report = run_check(root, providers={"python": StaticProvider([history])})
    assert rules(report) == ["removed-in"]  # no "register this" for a call that must be fixed


def test_rest_declaration_checked_against_provider(tmp_path):
    root = make_project(tmp_path, "")
    registry.register(
        root,
        api="stripe",
        kind="openapi",
        target="charges.refund_all",
        location=Location(file="app.py"),
        version="2024-06-20",
    )
    history = ApiHistory("stripe", {"charges": ApiSymbol("charges", "module")})
    report = run_check(root, providers={"openapi": StaticProvider([history])})
    assert rules(report) == ["unknown-symbol"]


def test_only_given_files_are_judged(tmp_path):
    root = make_project(tmp_path, "import openai\n")
    (root / "other.py").write_text("import requests\n", encoding="utf-8")
    report = run_check(root, files=[root / "other.py"])
    assert [f.file for f in report.findings] == ["other.py"]
