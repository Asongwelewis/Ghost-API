"""CI features: baselines, GitHub annotations, SARIF files and diff-only checks."""

import json
import subprocess

from typer.testing import CliRunner

from ghostapi import baseline
from ghostapi.checker import Report
from ghostapi.cli import app
from ghostapi.models import Confidence, Finding, Severity
from ghostapi.output import to_github

runner = CliRunner()


def finding(target: str, line: int = 1, file: str = "a.py") -> Finding:
    return Finding(
        "unknown-symbol", Severity.ERROR, Confidence.HIGH, "msg", file, line=line, target=target
    )


def test_baseline_ignores_lines_but_counts_copies(tmp_path):
    path = tmp_path / "baseline.json"
    assert baseline.write(path, [finding("x.a", line=3)]) == 1
    accepted = baseline.load(path)
    kept, hidden = baseline.apply([finding("x.a", line=40), finding("x.a", line=41)], accepted)
    assert hidden == 1 and [f.line for f in kept] == [41]  # moved copy accepted, extra copy new


def test_github_annotations_escape_message_and_properties():
    f = Finding(
        "removed-in",
        Severity.WARNING,
        Confidence.HIGH,
        "100% gone\nnow",
        "src/a,b.py",
        line=2,
        col=4,
        fix="use y:z",
    )
    first = to_github(Report([f], 0, 1)).splitlines()[0]
    assert first == (
        "::warning file=src/a%2Cb.py,line=2,col=5,title=Ghost API removed-in"
        "::100%25 gone%0Anow%0AFix: use y:z"
    )


def git(root, *args):
    base = ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false"]
    subprocess.run([*base, *args], cwd=root, check=True, capture_output=True)


def test_diff_base_baseline_and_sarif_in_a_subdirectory_project(tmp_path):
    repo = tmp_path
    app_dir = repo / "service"
    app_dir.mkdir()
    (app_dir / "old.py").write_text("import requests\nrequests.get('x')\n", encoding="utf-8")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "base")
    git(repo, "checkout", "-q", "-b", "feature")
    (app_dir / "new.py").write_text("import httpx\nhttpx.get('x')\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "change")

    sarif = tmp_path / "out.sarif"
    result = runner.invoke(
        app,
        ["check", "--root", str(app_dir), "--diff-base", "main", "-f", "json",
         "--sarif-out", str(sarif)],
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    assert {f["file"] for f in json.loads(result.output)["findings"]} == {"new.py"}
    assert json.loads(sarif.read_text(encoding="utf-8"))["runs"][0]["results"]

    assert runner.invoke(app, ["baseline", "--root", str(app_dir)]).exit_code == 0
    result = runner.invoke(
        app, ["check", "--root", str(app_dir), "--baseline", ".ghost/baseline.json", "-f", "json"]
    )
    report = json.loads(result.output)
    assert report["findings"] == [] and report["baselined"] == 2


def test_only_one_file_selector(tmp_path):
    result = runner.invoke(app, ["check", "--root", str(tmp_path), "--changed", "--staged"])
    assert result.exit_code == 2


def test_invalid_baseline_is_a_clear_error(tmp_path):
    (tmp_path / "b.json").write_text("{}", encoding="utf-8")
    result = runner.invoke(app, ["check", "--root", str(tmp_path), "--baseline", "b.json"])
    assert result.exit_code == 2 and "not a Ghost API baseline" in result.output
