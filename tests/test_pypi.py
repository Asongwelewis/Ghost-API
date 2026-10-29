"""PyPI provider against an in-memory fake index of a made-up package, `fakeai`."""

import io
import tarfile
import zipfile

import pytest

from ghostapi.engine import check_usage
from ghostapi.models import Usage
from ghostapi.providers.pypi import PyPIProvider, ReleaseFile, extract_sources

V0_9 = {
    "fakeai/__init__.py": (
        "from .api import ChatCompletion\nfrom .legacy_utils import count_tokens\n"
    ),
    "fakeai/api.py": (
        "class ChatCompletion:\n"
        "    @classmethod\n"
        "    def create(cls, model, messages):\n"
        "        ...\n"
    ),
    "fakeai/legacy_utils.py": "def count_tokens(text):\n    ...\n",
}
V1_0 = {
    "fakeai/__init__.py": "from ._client import Client\nfrom . import lazy\n",
    "fakeai/_client.py": (
        "class Client:\n"
        "    def __init__(self, *, api_key=None):\n"
        "        ...\n"
        "    def complete(self, prompt, *, temperature=1.0):\n"
        "        ...\n"
    ),
    "fakeai/utils.py": "def count_tokens(text):\n    ...\n",
    "fakeai/lazy.py": "def __getattr__(name):\n    ...\n",
}
V1_1 = V1_0
V1_2 = {
    **V1_0,
    "fakeai/__init__.py": "from ._client import Client, AsyncClient\nfrom . import lazy\n",
    "fakeai/_client.py": V1_0["fakeai/_client.py"] + "\nclass AsyncClient(Client):\n    ...\n",
}
RELEASES = {"0.9.0": V0_9, "1.0.0": V1_0, "1.1.0": V1_1, "1.2.0": V1_2, "2.0.0rc1": V1_2}


def wheel(files: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, source in files.items():
            archive.writestr(name, source)
        archive.writestr("fakeai-x.dist-info/METADATA", "Name: fakeai\n")
    return buffer.getvalue()


class FakeIndex:
    def __init__(self, releases=RELEASES):
        self.archives = {v: wheel(files) for v, files in releases.items()}
        self.downloads: list[str] = []

    def releases(self, package):
        if package != "fakeai":
            return None
        return {
            v: [ReleaseFile(f"fakeai-{v}-py3-none-any.whl", v, "bdist_wheel")]
            for v in self.archives
        }

    def download(self, file):
        self.downloads.append(file.url)
        return self.archives[file.url]


def check(target, version="1.0.0", provider=None, **call):
    provider = provider or PyPIProvider(FakeIndex())
    history = provider.history("fakeai", version, [target])
    assert history is not None
    usage = Usage(target=target, module="fakeai", file="app.py", line=1, **call)
    return check_usage(usage, package="fakeai", version=version, history=history)


def test_existing_symbol_is_clean_and_only_pinned_version_is_downloaded():
    index = FakeIndex()
    assert check("fakeai.Client.complete", provider=PyPIProvider(index)) == []
    assert index.downloads == ["1.0.0"]


def test_removed_symbol_found_by_bisecting_older_releases():
    [f] = check("fakeai.ChatCompletion.create", version="1.1.0")
    assert (f.rule, f.target) == ("removed-in", "fakeai.ChatCompletion")
    assert "removed in fakeai 1.0.0" in f.message


def test_moved_symbol_points_to_its_single_new_home():
    [f] = check("fakeai.count_tokens")
    assert f.rule == "import-path"
    assert f.fix == "Use `fakeai.utils.count_tokens`."


def test_removed_module():
    [f] = check("fakeai.legacy_utils.count_tokens")
    assert (f.rule, f.target) == ("removed-in", "fakeai.legacy_utils")


def test_too_new_ignores_prereleases():
    [f] = check("fakeai.AsyncClient")
    assert f.rule == "too-new"
    assert "added in fakeai 1.2.0" in f.message


def test_never_existed_gets_did_you_mean():
    [f] = check("fakeai.Clinet")
    assert f.rule == "unknown-symbol"
    assert f.fix == "Did you mean `fakeai.Client`?"


def test_dynamic_module_is_silent():
    assert check("fakeai.lazy.anything") == []


def test_parameters_checked_against_pinned_signature():
    [f] = check("fakeai.Client", n_positional=0, kwargs=("api_key", "timeout"))
    assert f.rule == "bad-parameter"
    [f] = check("fakeai.Client.complete", n_positional=0, kwargs=("prompt", "temprature"))
    assert f.rule == "bad-parameter"
    assert f.fix == "Did you mean `temperature`?"
    [f] = check("fakeai.Client", n_positional=1)
    assert f.rule == "arity"


def test_unknown_package_or_version_is_unverifiable():
    provider = PyPIProvider(FakeIndex())
    assert provider.history("not-on-pypi", "1.0.0", ["x"]) is None
    assert provider.history("fakeai", "9.9.9", ["fakeai.Client"]) is None
    assert provider.history("fakeai", None, ["fakeai.Client"]) is None


def test_surfaces_are_cached_on_disk(tmp_path):
    first = FakeIndex()
    check("fakeai.Client", provider=PyPIProvider(first, tmp_path))
    second = FakeIndex()
    check("fakeai.Client", provider=PyPIProvider(second, tmp_path))
    assert first.downloads == ["1.0.0"]
    assert second.downloads == []


def test_lifecycle_answers_are_cached_on_disk(tmp_path):
    first = FakeIndex()
    check("fakeai.ChatCompletion", version="1.1.0", provider=PyPIProvider(first, tmp_path))
    assert len(first.downloads) > 1  # had to look at older releases
    second = FakeIndex()
    [f] = check("fakeai.ChatCompletion", version="1.1.0", provider=PyPIProvider(second, tmp_path))
    assert f.rule == "removed-in"
    assert second.downloads == []


def test_extract_skips_path_traversal_and_metadata(tmp_path):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("../evil.py", "x = 1")
        archive.writestr("pkg/__init__.py", "")
        archive.writestr("pkg/_speedups.cpython-312-x86_64-linux-gnu.so", b"\0")
        archive.writestr("pkg-1.0.dist-info/RECORD", "")
    tops, compiled = extract_sources(buffer.getvalue(), "pkg-1.0-py3-none-any.whl", tmp_path)
    assert tops == ["pkg"]
    assert compiled == ["pkg._speedups"]
    assert not (tmp_path.parent / "evil.py").exists()


def test_extract_sdist_strips_prefix_and_skips_tests(tmp_path):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name in ("pkg-1.0/src/pkg/__init__.py", "pkg-1.0/tests/test_x.py", "pkg-1.0/setup.py"):
            info = tarfile.TarInfo(name)
            info.size = 0
            archive.addfile(info, io.BytesIO(b""))
    tops, _ = extract_sources(buffer.getvalue(), "pkg-1.0.tar.gz", tmp_path)
    assert tops == ["pkg"]


@pytest.mark.network
def test_real_openai_chat_completion_removed(monkeypatch):
    from ghostapi.fetch import Http, cache_dir
    from ghostapi.providers.pypi import PyPIIndex

    provider = PyPIProvider(PyPIIndex(Http(cache_dir(), offline=False)), cache_dir())
    history = provider.history(
        "openai", "1.40.0", ["openai.ChatCompletion.create", "openai.OpenAl"]
    )
    assert history is not None
    removed = history.symbols["openai.ChatCompletion"]
    assert removed.removed_in == "1.0.0"
    usage = Usage(target="openai.OpenAl", module="openai", file="a.py", line=1)
    [f] = check_usage(usage, package="openai", version="1.40.0", history=history)
    assert f.rule == "unknown-symbol" and f.fix == "Did you mean `openai.OpenAI`?"
