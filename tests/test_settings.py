"""Owner Settings flows, concurrency, explicit writes, and response secrecy."""
import time

from .conftest import HUB_HOST, assert_no_secrets

ORIGIN = {"Origin": f"http://{HUB_HOST}"}


def write(hub, method, resource, body):
    return hub._keep(hub.client.request(method, "/api/settings/" + resource, json=body,
                                     headers={"X-Hub-Request": "1", **ORIGIN}))


def test_registry_validate_save_conflict_rollback_restart(claimed, fake_daemon):
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
    time.sleep(1.05)
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


def test_settings_auth_csrf_closed_dispatch_and_input(claimed, fake_daemon):
    hub = claimed
    before = len(fake_daemon.settings_calls)
    assert hub.client.get("/api/settings/config", headers={"X-Hub-Session": ""}).status_code == 401
    assert hub.client.patch("/api/settings/config", json={}).status_code == 403
    assert hub.post("/api/settings/notify", json={}).status_code == 403
    assert write(hub, "PUT", "backend", {"name": "../keys", "model": "sample"}).status_code == 400
    assert write(hub, "POST", "notify", {"token": "synthetic"}).status_code == 400
    assert write(hub, "PATCH", "config", {"changes": {}}).status_code == 400
    assert write(hub, "PATCH", "config", {"revision": True, "changes": {}}).status_code == 400
    assert hub.client.post("/api/settings/notify", json={}, headers={"X-Hub-Request": "1",
                           "Origin": "https://foreign.test"}).status_code == 403
    assert hub.client.post("/api/settings/notify", content="x" * 262145,
                           headers={"X-Hub-Request": "1", **ORIGIN}).status_code == 413
    assert len(fake_daemon.settings_calls) == before


def test_settings_unreachable_and_release_confirmation(claimed, fake_daemon):
    fake_daemon.down = True
    assert claimed.get("/api/settings/config").status_code == 503
    fake_daemon.down = False
    fake_daemon.release()
    assert claimed.get("/api/settings/config").status_code == 409
    assert claimed.get("/api/claim").json()["state"] == "released"


def test_settings_require_claim(hub):
    hub.sign_in()
    assert hub.get("/api/settings/config").status_code == 409


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
