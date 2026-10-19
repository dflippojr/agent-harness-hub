"""The heartbeat file the production-signals sweep reads (status.py, docs/production-signals.md)."""

from __future__ import annotations

import json
import time

import pytest

from harness_hub import __main__ as cli
from harness_hub import __version__
from harness_hub.status import STATUS_FILE, UNKNOWN_BUILD, build_commit

SHA = "0123456789abcdef0123456789abcdef01234567"


def read_status(hub) -> dict:
    return json.loads((hub.state_dir / STATUS_FILE).read_text(encoding="utf-8"))


def wait_for_status(hub, predicate, timeout: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout
    while True:
        try:
            status = read_status(hub)
        except (OSError, json.JSONDecodeError):  # Windows refuses a read while the writer is replacing the file
            status = {}
        if predicate(status) or time.monotonic() > deadline:
            assert predicate(status), status
            return status
        time.sleep(0.02)


def test_no_status_file_unless_asked_for(hub):
    hub.get("/healthz")
    assert not (hub.state_dir / STATUS_FILE).exists()


def test_heartbeat_records_typed_values_only(make_hub, fake_daemon):
    hub = make_hub(status_seconds=0.02, build=SHA)
    status = wait_for_status(hub, lambda s: s.get("daemon_ok") is True)
    assert set(status) == {"version", "build", "started_at", "written_at", "claim", "daemon_ok", "daemon_last_ok_at"}
    assert status["version"] == __version__ and status["build"] == SHA and status["claim"] == "unclaimed"
    assert status["daemon_last_ok_at"] <= status["written_at"]
    assert "/health" in "".join(fake_daemon.sent)
    assert "token" not in json.dumps(status)


def test_heartbeat_follows_the_claim(make_hub, fake_daemon):
    hub = make_hub(status_seconds=0.02)
    hub.sign_in()
    pending = hub.post("/api/claim").json()
    wait_for_status(hub, lambda s: s.get("claim") == "pending")
    fake_daemon.approve(pending["request_id"])
    wait_for_status(hub, lambda s: s.get("claim") == "claimed")


def test_a_down_daemon_keeps_the_last_answer(make_hub, fake_daemon):
    hub = make_hub(status_seconds=0.02)
    answered = wait_for_status(hub, lambda s: s.get("daemon_ok") is True)
    fake_daemon.down = True
    down = wait_for_status(hub, lambda s: s.get("daemon_ok") is False)
    assert down["daemon_last_ok_at"] is not None and down["daemon_last_ok_at"] >= answered["daemon_last_ok_at"]
    fake_daemon.down = False
    wait_for_status(hub, lambda s: s.get("daemon_ok") is True)


def test_a_daemon_that_never_answered_has_no_last_answer(make_hub, fake_daemon):
    fake_daemon.down = True
    hub = make_hub(status_seconds=0.02)
    status = wait_for_status(hub, lambda s: "daemon_ok" in s)
    assert status["daemon_ok"] is False and status["daemon_last_ok_at"] is None


def test_an_unhealthy_answer_is_not_an_answer(make_hub, fake_daemon):
    fake_daemon.restart_polls = 10_000  # /health answers 503
    hub = make_hub(status_seconds=0.02)
    assert wait_for_status(hub, lambda s: "daemon_ok" in s)["daemon_ok"] is False


def test_a_failed_write_does_not_stop_the_heartbeat(make_hub, monkeypatch):
    import harness_hub.status as status_module

    real, calls = status_module.write_private, []

    def flaky(path, text):
        calls.append(path)
        if len(calls) == 1:
            raise OSError("disk full")
        real(path, text)

    monkeypatch.setattr(status_module, "write_private", flaky)
    hub = make_hub(status_seconds=0.02)
    wait_for_status(hub, lambda s: s.get("daemon_ok") is True)
    assert len(calls) >= 2


@pytest.mark.parametrize("value, expected", [(SHA, SHA), (SHA.upper(), SHA), (f" {SHA}\n", SHA), ("", UNKNOWN_BUILD),
                                             (None, UNKNOWN_BUILD), ("main", UNKNOWN_BUILD), (SHA[:-1], UNKNOWN_BUILD),
                                             (SHA + "0", UNKNOWN_BUILD), ("g" * 40, UNKNOWN_BUILD)])
def test_build_commit(value, expected):
    assert build_commit(value) == expected


def test_command_line_enables_the_heartbeat(tmp_path, monkeypatch):
    captured = {}
    monkeypatch.setattr("harness_hub.server.create_app", lambda settings: captured.update(settings=settings))
    monkeypatch.setattr("uvicorn.run", lambda *_args, **_kwargs: None)
    monkeypatch.setenv("HUB_BUILD_COMMIT", SHA)
    cli.main(["--state-dir", str(tmp_path), "--status-interval", "30"])
    assert captured["settings"].status_seconds == 30 and captured["settings"].build == SHA
    monkeypatch.delenv("HUB_BUILD_COMMIT")
    cli.main(["--state-dir", str(tmp_path)])
    assert captured["settings"].status_seconds == 0 and captured["settings"].build == UNKNOWN_BUILD
