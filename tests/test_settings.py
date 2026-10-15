"""Owner Settings flows, concurrency, explicit writes, and response secrecy."""
import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from harness_hub import server
from harness_hub.daemon import DaemonClient

from .conftest import HUB_HOST, assert_no_secrets

ORIGIN = {"Origin": f"http://{HUB_HOST}"}


def write(hub, method, resource, body):
    return hub._keep(hub.client.request(method, "/api/settings/" + resource, json=body,
                                     headers={"X-Hub-Request": "1", **ORIGIN}))


def test_registry_validate_save_conflict_rollback_restart(claimed, fake_daemon, monkeypatch):
    monkeypatch.setattr(server, "RESTART_GRACE_SECONDS", 0)  # the fake daemon's two 503s keep the restart showing
    hub = claimed
    schema = hub.get("/api/settings/schema").json()
    config = hub.get("/api/settings/config").json()
    assert len(schema["settings"]) == len(fake_daemon.schema)
    assert config["revision"] == 3
    path = next(s for s in config["settings"] if s["key"] == "provider.secret_ref")
    assert path["effective"] == "/synthetic/token-file"
    hidden = next(s for s in schema["settings"] if s["key"] == "provider.credential")
    assert hidden["default"] is None and hidden["writable"] is False
    invalid = write(hub, "POST", "validate", {"revision": 3, "changes": {"queue.limit": -1, "missing": True}})
    assert invalid.status_code == 400
    assert set(invalid.json()["error"]["keys"]) == {"queue.limit", "missing"}
    payload = {"revision": 3, "changes": {"queue.limit": 4, "listen.idle": 2.0,
                                        "provider.secret_ref": "/synthetic/another-file"}}
    validated = write(hub, "POST", "validate", payload)
    assert validated.status_code == 200 and validated.json()["restart_required"] is True
    assert fake_daemon.revision == 3
    saved = write(hub, "PATCH", "config", payload)
    assert saved.status_code == 200 and saved.json()["revision"] == 4
    assert saved.json()["pending_revision"] == 4
    pending = next(s for s in saved.json()["settings"] if s["key"] == "listen.idle")
    assert pending["effective"] == 1.5 and pending["pending"] == 2.0
    stale = write(hub, "PATCH", "config", payload)
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "revision_conflict"
    assert hub.get("/api/settings/config").json()["revision"] == 4
    assert write(hub, "POST", "rollback", {"revision": 4}).status_code == 400
    rolled = write(hub, "POST", "rollback", {"revision": 4, "confirm": True})
    assert rolled.status_code == 200 and rolled.json()["revision"] == 5
    assert write(hub, "POST", "restart", {"revision": 5, "confirm": False}).status_code == 400
    started = write(hub, "POST", "restart", {"revision": 5, "confirm": True})
    assert started.status_code == 202 and started.json()["status"] == "restarting"
    assert hub.get("/api/overview").json()["restarting"] is True
    assert hub.get("/api/settings/health").json()["restarting"] is True
    healthy = hub.get("/api/settings/health").json()
    assert healthy["reachable"] and not healthy["restarting"]
    assert hub.get("/api/overview").json()["restarting"] is False
    fake_daemon.recovery = {"recovery": "lkg_restore", "reason": "synthetic recovery", "credential": "secret"}
    recovery = hub.get("/api/settings/health").json()["health"]["recovery"]
    assert recovery["recovery"] == "lkg_restore" and "credential" not in recovery
    assert_no_secrets(hub.bodies, fake_daemon.hub_token, "provider-secret-sentinel")


def test_all_daemon_controls_and_secrecy(claimed, fake_daemon):
    hub = claimed
    for resource in ("smart-approvals", "backends", "memory", "github", "google"):
        assert hub.get("/api/settings/" + resource).status_code == 200
    for resource, body in (
        ("smart-approvals", {"mode": "shadow"}),
        ("backend", {"name": "sample", "model": "another-model", "effort": "high"}),
        ("memory", {"content": "Updated synthetic profile", "summary": "Synthetic update"}),
        ("github", {"enabled": True}),
    ):
        assert write(hub, "PUT", resource, body).status_code == 200
    assert fake_daemon.mode == "shadow" and fake_daemon.backend["effort"] == "high"
    assert fake_daemon.profile == "Updated synthetic profile" and fake_daemon.github_enabled
    assert write(hub, "POST", "notify", {}).json() == {"sent": True}
    assert write(hub, "PUT", "google", {"enabled": False}).status_code == 404
    assert_no_secrets(hub.bodies, fake_daemon.hub_token, "provider-secret-sentinel")


def test_settings_input_is_closed_before_any_daemon_call(claimed, fake_daemon):
    hub = claimed
    before = len(fake_daemon.settings_calls)
    assert write(hub, "PUT", "backend", {"name": "../keys", "model": "sample"}).status_code == 400
    assert write(hub, "POST", "notify", {"token": "synthetic"}).status_code == 400
    assert write(hub, "PATCH", "config", {"changes": {}}).status_code == 400
    assert write(hub, "PATCH", "config", {"revision": True, "changes": {}}).status_code == 400
    assert len(fake_daemon.settings_calls) == before


def test_registry_sensitive_values_and_guard_fail_closed(claimed, fake_daemon):
    hidden = next(row for row in fake_daemon.schema if row["key"] == "provider.credential")
    hidden["default"] = "provider-secret-sentinel"
    fake_daemon.values["provider.credential"] = "provider-secret-sentinel"
    fake_daemon.values["labels.names"] = {"credential": "provider-secret-sentinel"}
    shown = claimed.get("/api/settings/config")
    assert shown.status_code == 200
    fields = {row["key"]: row for row in shown.json()["settings"]}
    assert fields["provider.credential"]["default"] is None
    assert fields["provider.credential"]["effective"] is None
    assert fields["labels.names"]["effective"] is None
    fake_daemon.values["queue.limit"] = fake_daemon.hub_token
    assert claimed.get("/api/settings/config").status_code == 500
    assert_no_secrets(claimed.bodies, fake_daemon.hub_token, "provider-secret-sentinel")


def test_absent_memory_module_is_a_state_not_a_fault(claimed, fake_daemon):
    hub = claimed
    assert hub.get("/api/settings/memory").json()["installed"] is True
    fake_daemon.memory_installed = False
    absent = hub.get("/api/settings/memory")
    assert absent.status_code == 200
    assert absent.json()["installed"] is False and absent.json()["enabled"] is False
    assert "Not Found" not in absent.text
    saved = write(hub, "PUT", "memory", {"content": "Synthetic", "summary": "Synthetic"})
    assert saved.status_code == 404


def test_errors_say_whether_the_daemon_answered(claimed, fake_daemon):
    # The shell tells "the Hub could not reach the daemon" from "the daemon answered with an error" by this marker,
    # never by the status alone: the Hub also forwards the daemon's own 503.
    fake_daemon.down = True
    for path in ("/api/settings/config", "/api/pairing"):
        gone = claimed.get(path)
        assert gone.status_code == 503 and gone.json()["reachable"] is False
    fake_daemon.down = False
    stale = write(claimed, "POST", "validate", {"revision": 1, "changes": {}})
    assert stale.status_code == 409 and stale.json()["reachable"] is True
    assert stale.json()["error"]["code"] == "revision_conflict"
    # A refusal of the Hub's own never says either way.
    assert "reachable" not in claimed.get("/api/settings/nothing").json()


def test_overview_ends_a_restart_that_recovery_resolved_at_a_lower_revision(claimed, fake_daemon, monkeypatch):
    monkeypatch.setattr(server, "RESTART_GRACE_SECONDS", 0)
    saved = write(claimed, "PATCH", "config", {"revision": 3, "changes": {"listen.idle": 2.0}})
    assert saved.json()["pending_revision"] == 4
    started = write(claimed, "POST", "restart", {"revision": 4, "confirm": True})
    assert started.status_code == 202 and started.json()["target_revision"] == 4
    # Revision 4 fails to boot: the daemon restores its last good revision 3 and drops the pending one.
    fake_daemon.restart_requested, fake_daemon.restart_polls = False, 1
    fake_daemon.revision, fake_daemon.pending, fake_daemon.pending_revision = 3, {}, None
    fake_daemon.recovery = {"recovery": "lkg_restore", "reason": "synthetic boot failure", "at": 50}
    assert claimed.get("/api/overview").json()["restarting"] is True  # the daemon still answers 503
    # The owner left Settings: Overview alone ends the restart, so its banner does not stay up.
    shown = claimed.get("/api/overview").json()
    assert shown["restarting"] is False
    assert claimed.get("/api/settings/health").json()["restarting"] is False


def test_a_health_error_is_the_daemon_answering_not_silence(claimed, fake_daemon):
    # A daemon that answers /health with 503 mid-restart is answering: the shell shows the restart, not an outage.
    started = write(claimed, "POST", "restart", {"revision": 3, "confirm": True})
    assert started.status_code == 202
    fake_daemon.restart_polls = 2
    health = claimed.get("/api/settings/health").json()
    assert health["answered"] is True and health["reachable"] is False and health["status"] == 503
    assert health["restarting"] is True and health["health"] is None
    shown = claimed.get("/api/overview").json()
    assert shown["answered"] is True and shown["reachable"] is False and shown["restarting"] is True
    # Only a daemon the Hub could not reach is not answering.
    fake_daemon.down = True
    assert claimed.get("/api/settings/health").json()["answered"] is False
    assert claimed.get("/api/overview").json()["answered"] is False
    fake_daemon.down = False
    assert claimed.get("/api/settings/health").json()["answered"] is True


@pytest.mark.parametrize("source", ["overview", "settings/health"])
def test_health_read_before_restart_cannot_end_it(claimed, monkeypatch, source):
    monkeypatch.setattr(server, "RESTART_GRACE_SECONDS", 0)
    health_read, parked, release = threading.Event(), threading.Event(), threading.Event()
    health, pairing = DaemonClient.health, DaemonClient.pairing

    async def delayed_health(self):
        reply = await health(self)
        health_read.set()
        if source == "settings/health":
            parked.set()
            assert await asyncio.to_thread(release.wait, 5)
        return reply

    async def delayed_pairing(self, *args, **kwargs):
        assert await asyncio.to_thread(health_read.wait, 5)
        parked.set()
        assert await asyncio.to_thread(release.wait, 5)
        return await pairing(self, *args, **kwargs)

    monkeypatch.setattr(DaemonClient, "health", delayed_health)
    monkeypatch.setattr(DaemonClient, "pairing", delayed_pairing)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(claimed.get, "/api/" + source)
        try:
            assert parked.wait(5)
            accepted = write(claimed, "POST", "restart", {"revision": 3, "confirm": True})
            assert accepted.status_code == 202
        finally:
            release.set()
        stale = pending.result(timeout=5).json()
    assert stale["restarting"] is True
    assert claimed.get("/api/settings/health").json()["restarting"] is True
    assert claimed.get("/api/settings/health").json()["restarting"] is True
    assert claimed.get("/api/settings/health").json()["restarting"] is False
