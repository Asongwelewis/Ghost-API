import json

from ghostapi.resolver import python_dist_for, resolve_versions


def test_lockfile_beats_requirements(tmp_path):
    (tmp_path / "uv.lock").write_text(
        '[[package]]\nname = "OpenAI"\nversion = "1.40.0"\n', encoding="utf-8"
    )
    (tmp_path / "requirements.txt").write_text(
        "openai==0.28.0\nrequests[socks]==2.32.3 ; python_version >= '3.8'\n# pandas==1.0\n",
        encoding="utf-8",
    )
    pins = resolve_versions(tmp_path)["python"]
    assert pins == {"openai": "1.40.0", "requests": "2.32.3"}


def test_package_lock_top_level_only(tmp_path):
    lock = {
        "lockfileVersion": 3,
        "packages": {
            "": {"name": "app"},
            "node_modules/react": {"version": "18.3.1"},
            "node_modules/a/node_modules/react": {"version": "16.0.0"},
        },
    }
    (tmp_path / "package-lock.json").write_text(json.dumps(lock), encoding="utf-8")
    assert resolve_versions(tmp_path)["npm"] == {"react": "18.3.1"}


def test_import_name_to_distribution():
    pins = {"google-genai": "1.0.0", "pyyaml": "6.0", "google-cloud-storage": "2.0"}
    assert python_dist_for("google.genai.Client", pins) == "google-genai"
    assert python_dist_for("yaml.safe_load", pins) == "pyyaml"
    assert python_dist_for("google.cloud.storage.Client", pins) == "google-cloud-storage"
    assert python_dist_for("nothing.here", pins) is None
