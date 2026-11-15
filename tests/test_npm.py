"""npm provider and checker integration against an in-memory fake registry."""

import io
import json
import tarfile

import pytest

from ghostapi.checker import run_check
from ghostapi.engine import check_usage
from ghostapi.models import Usage
from ghostapi.providers.npm import NpmProvider, extract_declarations, types_package


def tarball(files: dict[str, str], prefix: str = "package") -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, source in files.items():
            data = source.encode()
            info = tarfile.TarInfo(f"{prefix}/{name}")
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def pkg(index: str) -> dict[str, str]:
    return {"package.json": json.dumps({"types": "index.d.ts"}), "index.d.ts": index}


FAKEJS = {
    "1.0.0": pkg(
        "export declare function legacyFetch(url: string): void;\n"
        "export declare function get(url: string): void;\n"
    ),  # fmt: skip
    "2.0.0": pkg("export declare function get(url: string, config?: object): void;\n"),
    "2.1.0": pkg(
        "export declare function get(url: string, config?: object): void;\n"
        "export declare function stream(url: string): void;\n"
    ),  # fmt: skip
    "3.0.0-beta.1": pkg("export declare function everything(): void;\n"),
}
UNTYPED = {"1.0.0": {"package.json": "{}", "index.js": "module.exports = {}"}}
TYPES_UNTYPED = {
    "1.0.4": {"package.json": "{}", "index.d.ts": "export declare function hello(): void;\n"}
}


class FakeRegistry:
    def __init__(self):
        self.packages = {"fakejs": FAKEJS, "untyped": UNTYPED, "@types/untyped": TYPES_UNTYPED}
        self.downloads: list[str] = []

    def tarballs(self, package):
        if package not in self.packages:
            return None
        return {v: f"{package}@{v}" for v in self.packages[package]}

    def download(self, url):
        self.downloads.append(url)
        name, _, version = url.rpartition("@")
        return tarball(self.packages[name][version])


def check(target, version="2.0.0", **call):
    provider = NpmProvider(FakeRegistry())
    history = provider.history("fakejs", version, [target])
    assert history is not None
    usage = Usage(target=target, module="fakejs", file="a.ts", line=1, **call)
    return check_usage(usage, package="fakejs", version=version, history=history)


def test_types_package_names():
    assert types_package("lodash") == "@types/lodash"
    assert types_package("@babel/core") == "@types/babel__core"


def test_npm_rules():
    assert check("fakejs:get") == []
    [f] = check("fakejs:legacyFetch")
    assert (f.rule, "removed in fakejs 2.0.0" in f.message) == ("removed-in", True)
    [f] = check("fakejs:stream")
    assert (f.rule, "added in fakejs 2.1.0" in f.message) == ("too-new", True)  # skips the beta
    [f] = check("fakejs:gett")
    assert (f.rule, f.fix) == ("unknown-symbol", "Did you mean `fakejs:get`?")
    [f] = check("fakejs:get", n_positional=3)
    assert f.rule == "arity"


def test_untyped_package_has_no_surface():
    assert NpmProvider(FakeRegistry()).history("untyped", "1.0.0", ["untyped:x"]) is None


def test_extract_skips_traversal_links_and_bundled_node_modules(tmp_path):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name in ("package/../evil.d.ts", "package/node_modules/x/index.d.ts", "package/a.d.ts"):
            info = tarfile.TarInfo(name)
            info.size = 0
            archive.addfile(info, io.BytesIO(b""))
        link = tarfile.TarInfo("package/link.d.ts")
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc/passwd"
        archive.addfile(link)
    extract_declarations(buffer.getvalue(), tmp_path)
    assert sorted(p.name for p in tmp_path.rglob("*")) == ["a.d.ts"]


@pytest.fixture
def js_project(tmp_path):
    lock = {
        "lockfileVersion": 3,
        "packages": {
            "": {},
            "node_modules/fakejs": {"version": "2.0.0"},
            "node_modules/untyped": {"version": "1.0.0"},
            "node_modules/@types/untyped": {"version": "1.0.4"},
        },
    }
    (tmp_path / "package-lock.json").write_text(json.dumps(lock), encoding="utf-8")
    (tmp_path / "app.ts").write_text(
        'import { get, stream } from "fakejs";\nimport { hello, helo } from "untyped";\n'
        'import { local } from "./local";\nget("/x");\n',
        encoding="utf-8",
    )
    return tmp_path


def test_checker_uses_lockfile_and_definitely_typed_fallback(js_project):
    report = run_check(js_project, providers={"npm": NpmProvider(FakeRegistry())})
    errors = sorted((f.rule, f.target) for f in report.findings if f.severity == "error")
    assert errors == [("too-new", "fakejs:stream"), ("unknown-symbol", "untyped:helo")]
    [helo] = [f for f in report.findings if f.target == "untyped:helo"]
    assert (helo.api, helo.version) == ("@types/untyped", "1.0.4")
    assert report.unverifiable == 0


def test_unregistered_npm_call_suggests_npm_register_command(js_project):
    report = run_check(js_project, providers={})
    [get] = [f for f in report.findings if f.target == "fakejs:get"]
    assert get.fix is not None and get.fix.startswith("ghostapi register --kind npm --api fakejs")


@pytest.mark.network
def test_real_react_types_and_axios():
    from ghostapi.fetch import Http, cache_dir
    from ghostapi.providers.npm import NpmRegistry

    provider = NpmProvider(NpmRegistry(Http(cache_dir(), offline=False)), cache_dir())
    react = provider.history("@types/react", "18.3.12", ["react:useStat", "react:useActionState"])
    assert react is not None
    assert react.symbols["react:useActionState"].added_in == "19.0.0"
    usage = Usage(target="react:useStat", module="react", file="a.tsx", line=1)
    [f] = check_usage(usage, package="react", version="18.3.1", history=react)
    assert f.fix == "Did you mean `react:useState`?"

    axios = provider.history("axios", "1.7.9", ["axios:default.get"])
    assert axios is not None
    assert [p.name for p in axios.symbols["axios:default.get"].params or ()] == ["url", "config"]
