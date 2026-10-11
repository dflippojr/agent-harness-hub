"""Failed private writes leave the entire published state unchanged."""

from __future__ import annotations

import pytest

from harness_hub import state
from harness_hub.state import StateStore


@pytest.mark.parametrize("values", [
    {"key": {"token": "ho-new-key"}, "claim": None, "outcome": None},
    {"key": None, "claim": None, "outcome": {"state": "released"}},
    {"claim": None, "outcome": None},
])
def test_failed_update_preserves_memory_and_disk(tmp_path, monkeypatch, values):
    store = StateStore(tmp_path)
    original = {"key": {"token": "ho-old-key"}, "claim": {"verifier": "old-verifier"},
                "outcome": {"state": "denied"}}
    store.update(**original)
    disk = store.path.read_bytes()

    def fail_write(*_):
        raise OSError("synthetic storage failure")

    monkeypatch.setattr(state, "write_private", fail_write)
    with pytest.raises(OSError, match="synthetic storage failure"):
        store.update(**values)
    assert store.path.read_bytes() == disk
    reloaded = StateStore(tmp_path)
    for name, value in original.items():
        assert store.get(name) == reloaded.get(name) == value


def test_failed_first_write_publishes_nothing(tmp_path, monkeypatch):
    store = StateStore(tmp_path)

    def fail_write(*_):
        raise OSError("synthetic storage failure")

    monkeypatch.setattr(state, "write_private", fail_write)
    with pytest.raises(OSError):
        store.update(key={"token": "ho-new-key"}, claim={"verifier": "new-verifier"})
    assert store.get("key") is None and store.get("claim") is None
    assert not store.path.exists()
    assert store.secrets() == []
