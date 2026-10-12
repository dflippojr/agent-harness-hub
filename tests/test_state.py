"""Failed private writes leave the entire published state unchanged."""

from __future__ import annotations

import errno
from types import SimpleNamespace

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


@pytest.mark.parametrize("failure", [None, errno.EINVAL, errno.EIO])
def test_posix_replace_syncs_directory_and_closes_it(tmp_path, monkeypatch, failure):
    operations = []
    target = tmp_path / "state.json"

    def sync(fd):
        operations.append(("sync", fd))
        if failure is not None:
            raise OSError(failure, "synthetic directory sync error")

    monkeypatch.setattr(state, "os", SimpleNamespace(
        name="posix", O_RDONLY=0,
        replace=lambda source, destination: operations.append(("replace", source, destination)),
        open=lambda directory, flags: operations.append(("open", directory, flags)) or 42,
        fsync=sync, close=lambda fd: operations.append(("close", fd)),
    ))
    if failure == errno.EIO:
        with pytest.raises(OSError):
            state._replace_private("temporary", target)
    else:
        state._replace_private("temporary", target)
    assert operations == [("replace", "temporary", target), ("open", tmp_path, 0), ("sync", 42), ("close", 42)]


def test_private_write_syncs_file_before_replace(tmp_path, monkeypatch):
    target = tmp_path / "private.json"
    events = []
    real_sync, real_replace = state.os.fsync, state._replace_private

    def sync(fd):
        events.append("sync")
        real_sync(fd)

    def replace(source, destination):
        assert events == ["sync"]
        events.append("replace")
        real_replace(source, destination)

    monkeypatch.setattr(state.os, "fsync", sync)
    monkeypatch.setattr(state, "_replace_private", replace)
    state.write_private(target, "saved")
    assert target.read_text() == "saved"
    assert events == (["sync", "replace"] if state.os.name == "nt" else ["sync", "replace", "sync"])


def test_sync_failure_after_replace_is_retried_before_publishing(tmp_path, monkeypatch):
    store = StateStore(tmp_path)
    store.update(claim={"verifier": "synthetic-verifier"})
    real_replace = state._replace_private

    def failed_directory_sync(source, target):
        real_replace(source, target)
        raise OSError("synthetic directory sync failure after replacement")

    monkeypatch.setattr(state, "_replace_private", failed_directory_sync)
    with pytest.raises(OSError):
        store.update(key={"token": "ho-synthetic-key"}, claim=None)
    assert store.get("key") is None
    assert store.get("claim") == {"verifier": "synthetic-verifier"}
    assert list(tmp_path.glob(".hub-state.json.*")) == []
    monkeypatch.setattr(state, "_replace_private", real_replace)
    store.update(key={"token": "ho-synthetic-key"}, claim=None)
    assert store.get("key") == StateStore(tmp_path).get("key") == {"token": "ho-synthetic-key"}
    assert store.get("claim") is None
