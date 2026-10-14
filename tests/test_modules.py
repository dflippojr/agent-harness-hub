"""Module inventory errors, private last-good snapshots, Settings parity and secrecy."""
import json
import os

import pytest

from harness_hub.inventory import INVENTORY_FILE, InventoryCache
from harness_hub.views import module_view

from .conftest import assert_no_secrets
from .test_settings import write


def test_inventory_states_status_and_secret_projection(claimed, fake_daemon):
    reply = claimed.get("/api/modules")
    assert reply.status_code == 200
    data = reply.json()
    assert data["state"] == "ready" and not data["stale"]
    rows = {row["name"]: row for row in data["modules"]}
    assert rows["hub"]["state"] == "present" and rows["hub"]["status"] == {"entries": 1}
    assert rows["search"]["state"] == "switched_off"
    assert rows["missing"]["state"] == "absent"
    assert rows["sample"]["status"] == {"state": "error"}
    assert "apps" not in data and "entries" not in data
    schema = claimed.get("/api/settings/schema").json()
    assert next(row for row in schema["settings"] if row["key"] == "notify.enabled")["modules"] == ["notifications"]
    assert_no_secrets(claimed.bodies, fake_daemon.hub_token, "provider-secret-sentinel")


def test_error_keeps_complete_last_good_in_memory_and_on_disk(claimed, fake_daemon):
    good = claimed.get("/api/modules").json()
    path = claimed.state_dir / INVENTORY_FILE
    stored = path.read_bytes()
    fake_daemon.inventory_error = "hub.entries: invalid JSON"
    bad = claimed.get("/api/modules").json()
    assert bad["state"] == "error" and bad["error"] == fake_daemon.inventory_error and bad["reachable"] is True
    assert bad["stale"] and bad["modules"] == good["modules"] and bad["fetched_at"] == good["fetched_at"]
    assert path.read_bytes() == stored
    cache = InventoryCache(claimed.state_dir, "https://daemon.test")
    cached = cache.get(fake_daemon.hub["key_id"])
    assert cached["inventory"]["apps"] and cached["inventory"]["entries"]
    assert InventoryCache(claimed.state_dir, "https://other.test").get(fake_daemon.hub["key_id"]) is None
    assert cache.get("other-key") is None
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600
        assert claimed.state_dir.stat().st_mode & 0o777 == 0o700
    # A fresh server also reads the same private snapshot, before its first good fetch.
    from fastapi.testclient import TestClient

    from harness_hub.config import Settings
    from harness_hub.server import create_app

    from .conftest import HUB_HOST, HubClient

    app = create_app(Settings(daemon_url="https://daemon.test", state_dir=claimed.state_dir),
                     transport=fake_daemon.transport())
    with TestClient(app, base_url=f"http://{HUB_HOST}") as client:
        browser = HubClient(client, claimed.state_dir)
        browser.sign_in()
        restored = browser.get("/api/modules").json()
        assert restored == bad
        assert_no_secrets(browser.bodies, fake_daemon.hub_token)
    assert_no_secrets(claimed.bodies, fake_daemon.hub_token)


@pytest.mark.parametrize("problem,state", [("disabled", "disabled"), ("absent", "absent"),
                                          ("down", "error"), ("entries", "error")])
@pytest.mark.parametrize("cached", [False, True])
def test_inventory_unavailable(claimed, fake_daemon, problem, state, cached):
    good = claimed.get("/api/modules").json() if cached else None
    if problem == "disabled":
        fake_daemon.values["hub.enabled"] = False
    elif problem == "absent":
        fake_daemon.inventory_absent = True
    elif problem == "entries":
        fake_daemon.inventory_error = "hub.entries: invalid JSON"
    else:
        fake_daemon.down = True
    result = claimed.get("/api/modules").json()
    assert result["state"] == state and result["error"]
    assert result["stale"] is cached
    assert result["modules"] == (good["modules"] if cached else None)


def test_switch_revision_restart_and_disabled_inventory_recovery(claimed, fake_daemon):
    # Live Hub switch recovers the inventory even while its route is disabled.
    assert write(claimed, "PATCH", "config", {"revision": 3, "changes": {"hub.enabled": False}}).status_code == 200
    assert claimed.get("/api/modules").json()["state"] == "disabled"
    assert write(claimed, "PATCH", "config", {"revision": 4, "changes": {"hub.enabled": True}}).status_code == 200
    assert claimed.get("/api/modules").json()["state"] == "ready"
    payload = {"revision": 5, "changes": {"search.enabled": True}}
    saved = write(claimed, "PATCH", "config", payload)
    assert saved.status_code == 200 and saved.json()["restart_required"] is True
    assert ("PATCH", "config", payload) in fake_daemon.settings_calls
    assert write(claimed, "PATCH", "config", payload).status_code == 409
    assert write(claimed, "POST", "restart", {"revision": 6}).status_code == 400
    assert write(claimed, "POST", "restart", {"revision": 6, "confirm": True}).status_code == 202
    for _ in range(3):
        claimed.get("/api/settings/health")
    rows = claimed.get("/api/modules").json()["modules"]
    assert next(row for row in rows if row["name"] == "search")["state"] == "present"
    assert_no_secrets(claimed.bodies, fake_daemon.hub_token)


def test_partial_or_secret_snapshot_never_replaces_good(claimed, fake_daemon):
    good = claimed.get("/api/modules").json()
    stored = (claimed.state_dir / INVENTORY_FILE).read_bytes()
    for body in ({"modules": []}, {"modules": [{"name": "broken", "state": "unknown"}], "apps": [], "entries": []},
                 {"modules": [{"name": "hub", "state": "present", "status": {"secret": fake_daemon.hub_token}}],
                  "apps": [], "entries": []}):
        fake_daemon.inventory_override = body
        result = claimed.get("/api/modules").json()
        assert result["state"] == "error" and result["modules"] == good["modules"]
        assert (claimed.state_dir / INVENTORY_FILE).read_bytes() == stored
    assert_no_secrets(claimed.bodies, fake_daemon.hub_token)


def test_error_secrets_fail_closed(claimed, fake_daemon):
    for secret in (fake_daemon.hub_token, "ha-" + "synthetic-secret" * 3, "hp-" + "synthetic-secret" * 2):
        fake_daemon.inventory_error = "hub.entries: " + secret
        assert claimed.get("/api/modules").status_code == 500
    fake_daemon.inventory_error = "hub.entries: ha-synthetic-secret"
    shown = claimed.get("/api/modules")
    assert shown.status_code == 200 and "ha-synthetic" not in shown.text
    assert_no_secrets(claimed.bodies, fake_daemon.hub_token)


def test_inventory_needs_owner_and_claim(hub, claimed, fake_daemon):
    # 'hub' is the same fixture used by 'claimed'; end its owner session first.
    claimed.delete("/api/session")
    assert hub.get("/api/modules").status_code == 401
    hub.sign_in()
    fake_daemon.release()
    assert hub.get("/api/modules").status_code == 409
    assert hub.get("/api/claim").json()["state"] == "released"
    assert hub.get("/api/modules").status_code == 409


def test_corrupt_cache_is_ignored(claimed, fake_daemon):
    path = claimed.state_dir / INVENTORY_FILE
    path.write_text("{invalid", encoding="utf-8")
    assert InventoryCache(claimed.state_dir, "https://daemon.test").get("sample") is None
    path.write_text(json.dumps({"daemon_url": "https://daemon.test", "inventory": {}}), encoding="utf-8")
    assert InventoryCache(claimed.state_dir, "https://daemon.test").get("sample") is None


def test_status_is_bounded_to_eight_short_scalars():
    body = {"modules": [{"name": "sample", "state": "present", "status": {
        "message": "x" * 201, "nested": {"secret": "ignored"}, "list": [1], "nan": float("nan"),
        "huge": 2 ** 65, "enabled": False, "count": 2, "empty": None, "ninth": "ignored"}}]}
    assert module_view(body)[0]["status"] == {"message": "x" * 200, "enabled": False, "count": 2, "empty": None}


def test_failed_private_write_preserves_last_good(claimed, fake_daemon, monkeypatch):
    good = claimed.get("/api/modules").json()
    path = claimed.state_dir / INVENTORY_FILE
    stored = path.read_bytes()

    def fail(*args):
        raise OSError("synthetic failure")

    monkeypatch.setattr("harness_hub.inventory.write_private", fail)
    result = claimed.get("/api/modules").json()
    assert result["state"] == "error" and result["stale"]
    assert result["modules"] == good["modules"] and result["fetched_at"] == good["fetched_at"]
    assert path.read_bytes() == stored


def test_transient_key_rejection_withholds_inventory_but_retains_claim(claimed, fake_daemon, monkeypatch):
    from harness_hub.daemon import Reply

    claimed.get("/api/modules")

    async def rejected(self, token):
        return Reply(401, {"detail": "synthetic rejection"})

    monkeypatch.setattr("harness_hub.daemon.DaemonClient.inventory", rejected)
    reply = claimed.get("/api/modules")
    assert reply.status_code == 409 and "modules" not in reply.json()
    assert claimed.get("/api/claim").json()["state"] == "claimed"
    assert_no_secrets(claimed.bodies, fake_daemon.hub_token)


LEAK = "auth failed for ha-" + "A1b2C3d4" * 6
LEAKING_MODULES = [{"name": "hub", "state": "present", "status": {"last_error": LEAK}}]


@pytest.mark.parametrize("writer", ["/api/apps", "/api/marketplace"])
def test_other_pages_never_cache_a_snapshot_modules_would_leak(claimed, fake_daemon, writer):
    from .apps_fake import inventory_apps
    good = claimed.get("/api/modules").json()
    stored = (claimed.state_dir / INVENTORY_FILE).read_bytes()
    fake_daemon.inventory_override = {"modules": LEAKING_MODULES, "apps": inventory_apps(fake_daemon), "entries": []}
    for path in ("/api/modules", writer, "/api/modules", writer):
        reply = claimed.get(path)
        assert reply.status_code == 200
        # Only Modules would show the string; the other page's own view is clean, and neither caches the snapshot.
        assert reply.json()["state"] == ("error" if path == "/api/modules" else "ready")
        assert path != "/api/modules" or "Inventory withheld" in reply.json()["error"]
    result = claimed.get("/api/modules").json()
    assert result["modules"] == good["modules"] and result["fetched_at"] == good["fetched_at"]
    assert (claimed.state_dir / INVENTORY_FILE).read_bytes() == stored
    assert_no_secrets(claimed.bodies, fake_daemon.hub_token)


def test_modules_page_never_caches_a_snapshot_apps_would_leak(claimed, fake_daemon):
    good = claimed.get("/api/apps").json()
    stored = (claimed.state_dir / INVENTORY_FILE).read_bytes()
    fake_daemon.apps_rows[0]["name"] = "x" * 220 + fake_daemon.hub_token
    for path in ("/api/modules", "/api/apps", "/api/modules", "/api/apps"):
        reply = claimed.get(path).json()
        assert reply["state"] == ("ready" if path == "/api/modules" else "error")
    assert (claimed.state_dir / INVENTORY_FILE).read_bytes() == stored
    fake_daemon.apps_rows[0]["name"] = good["apps"][0]["name"]
    assert claimed.get("/api/apps").json()["state"] == "ready"
    assert_no_secrets(claimed.bodies, fake_daemon.hub_token)
