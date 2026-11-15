import pytest

from ghostapi import registry
from ghostapi.registry import Location, RegistryError


def test_register_creates_and_merges(tmp_path):
    loc = Location(file="app.py", symbol="run", line_hint=3)
    registry.register(tmp_path, api="openai", kind="python", target="openai.OpenAI", location=loc)
    registry.register(
        tmp_path,
        api="openai",
        kind="python",
        target="openai.OpenAI",
        location=Location(file="app.py", symbol="run", line_hint=9),
        version="1.40.0",
    )
    [entry] = registry.load(tmp_path)
    assert entry.version == "1.40.0"
    [call] = entry.calls
    assert [loc.line_hint for loc in call.used_in] == [9]  # same file+symbol replaced


def test_kind_conflict_is_rejected(tmp_path):
    loc = Location(file="a.py")
    registry.register(tmp_path, api="x", kind="python", target="x.y", location=loc)
    with pytest.raises(RegistryError):
        registry.register(tmp_path, api="x", kind="npm", target="x.y", location=loc)


def test_unregister_by_file_then_everything(tmp_path):
    for file in ("a.py", "b.py"):
        registry.register(
            tmp_path, api="x", kind="python", target="x.y", location=Location(file=file)
        )
    assert registry.unregister(tmp_path, api="x", file="a.py") == 1
    assert registry.unregister(tmp_path, api="x") == 1
    assert registry.load(tmp_path) == []


def test_invalid_yaml_raises_registry_error(tmp_path):
    path = registry.entry_path(tmp_path, "bad")
    path.parent.mkdir(parents=True)
    path.write_text("api: bad\nkind: cobol\n", encoding="utf-8")
    with pytest.raises(RegistryError):
        registry.load(tmp_path)


def test_parallel_registrations_are_never_lost(tmp_path):
    # Regression: 20 agents registering at once used to keep only one of them, silently.
    import threading

    barrier = threading.Barrier(20)

    def agent(i: int) -> None:
        barrier.wait()
        registry.register(
            tmp_path, api="openai", kind="python", target=f"openai.T{i}",
            location=Location(file="a.py", symbol=f"f{i}"),
        )  # fmt: skip

    threads = [threading.Thread(target=agent, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    [entry] = registry.load(tmp_path)
    assert len(entry.calls) == 20


def test_one_file_per_target_and_owner(tmp_path):
    for target in ("openai.OpenAI", "openai.AsyncOpenAI"):
        registry.register(
            tmp_path, api="openai", kind="python", target=target,
            location=Location(file="a.py"), owner="billing-agent",
        )  # fmt: skip
    calls = sorted(p.name for p in (tmp_path / ".ghost/apis/openai/calls").iterdir())
    assert len(calls) == 2 and all(name.startswith("openai.") for name in calls)
    [entry] = registry.load(tmp_path)
    assert entry.owner == "billing-agent"


def test_legacy_single_file_entries_are_read_and_migrated(tmp_path):
    legacy = tmp_path / ".ghost" / "apis" / "openai.yaml"
    legacy.parent.mkdir(parents=True)
    legacy.write_text(
        "api: openai\nkind: python\ncalls:\n- target: openai.OpenAI\n  used_in:\n  - file: a.py\n",
        encoding="utf-8",
    )
    assert [c.target for c in registry.load(tmp_path)[0].calls] == ["openai.OpenAI"]
    registry.register(
        tmp_path, api="openai", kind="python", target="openai.X", location=Location(file="b.py")
    )
    assert not legacy.exists()
    assert sorted(c.target for c in registry.load(tmp_path)[0].calls) == [
        "openai.OpenAI",
        "openai.X",
    ]


def test_provide_and_unprovide(tmp_path):
    from ghostapi.registry import ProvidedEndpoint

    registry.provide(tmp_path, ProvidedEndpoint(method="post", path="/api/orders", owner="a"))
    registry.provide(tmp_path, ProvidedEndpoint(method="POST", path="/api/orders", description="d"))
    [endpoint] = registry.load_provided(tmp_path)
    assert (endpoint.method, endpoint.owner, endpoint.description) == ("POST", "a", "d")
    assert registry.unprovide(tmp_path, "POST", "/api/orders")
    assert registry.load_provided(tmp_path) == []
