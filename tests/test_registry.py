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
