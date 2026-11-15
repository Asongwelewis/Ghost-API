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


def test_pnpm_lock_v9_strips_peer_suffix(tmp_path):
    (tmp_path / "pnpm-lock.yaml").write_text(
        "lockfileVersion: '9.0'\n"
        "importers:\n"
        "  .:\n"
        "    dependencies:\n"
        "      react-dom:\n"
        "        specifier: ^18.3.1\n"
        "        version: 18.3.1(react@18.3.1)\n"
        "      local-lib:\n"
        "        specifier: workspace:*\n"
        "        version: link:../lib\n"
        "    devDependencies:\n"
        "      '@types/react':\n"
        "        specifier: ^18.3.0\n"
        "        version: 18.3.12\n",
        encoding="utf-8",
    )
    assert resolve_versions(tmp_path)["npm"] == {"react-dom": "18.3.1", "@types/react": "18.3.12"}


def test_yarn_v1_skips_ambiguous_versions(tmp_path):
    (tmp_path / "yarn.lock").write_text(
        "# yarn lockfile v1\n\n"
        '"@babel/core@^7.0.0", "@babel/core@^7.1.0":\n  version "7.22.5"\n\n'
        'lodash@^4.17.0:\n  version "4.17.21"\n\n'
        'lodash@^3.0.0:\n  version "3.10.1"\n',
        encoding="utf-8",
    )
    assert resolve_versions(tmp_path)["npm"] == {"@babel/core": "7.22.5"}


def test_yarn_berry(tmp_path):
    (tmp_path / "yarn.lock").write_text(
        "__metadata:\n  version: 8\n\n"
        '"axios@npm:^1.7.0":\n  version: 1.7.9\n  resolution: "axios@npm:1.7.9"\n',
        encoding="utf-8",
    )
    assert resolve_versions(tmp_path)["npm"] == {"axios": "1.7.9"}


def test_broken_lockfile_is_ignored(tmp_path):
    (tmp_path / "pnpm-lock.yaml").write_text("importers: [unclosed", encoding="utf-8")
    (tmp_path / "requirements.txt").write_text("requests==2.32.3\n", encoding="utf-8")
    assert resolve_versions(tmp_path) == {"python": {"requests": "2.32.3"}, "npm": {}}


def test_import_name_to_distribution():
    pins = {"google-genai": "1.0.0", "pyyaml": "6.0", "google-cloud-storage": "2.0"}
    assert python_dist_for("google.genai.Client", pins) == "google-genai"
    assert python_dist_for("yaml.safe_load", pins) == "pyyaml"
    assert python_dist_for("google.cloud.storage.Client", pins) == "google-cloud-storage"
    assert python_dist_for("nothing.here", pins) is None
