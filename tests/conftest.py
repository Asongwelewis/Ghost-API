import pytest


@pytest.fixture(autouse=True)
def _offline_and_isolated_cache(tmp_path_factory, monkeypatch):
    """Tests never touch the network or the user's real cache."""
    monkeypatch.setenv("GHOSTAPI_OFFLINE", "1")
    monkeypatch.setenv("GHOSTAPI_CACHE_DIR", str(tmp_path_factory.mktemp("cache")))


def pytest_collection_modifyitems(config, items):
    import os

    if os.environ.get("GHOSTAPI_NETWORK_TESTS") == "1":
        return
    skip = pytest.mark.skip(reason="set GHOSTAPI_NETWORK_TESTS=1 to hit the real PyPI")
    for item in items:
        if "network" in item.keywords:
            item.add_marker(skip)
