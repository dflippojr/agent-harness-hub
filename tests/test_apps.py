"""Apps inventory, confirmed owner writes, host-only restore and response secrecy."""
import json

import pytest

from harness_hub.inventory import INVENTORY_FILE
from harness_hub.views import app_view

from .conftest import HUB_HOST, assert_no_secrets

ORIGIN = {"Origin": "http://" + HUB_HOST, "X-Hub-Request": "1"}


def write(claimed, method, resource, body, headers=None):
    reply = claimed.client.request(method, "/api/apps/" + resource, json=body,
                                   headers=ORIGIN if headers is None else headers)
    return claimed._keep(reply)


def test_states_warning_metrics_entries_and_hub_identity(claimed, fake_daemon):
    reply = claimed.get("/api/apps")
    assert reply.status_code == 200
    data = reply.json()
    rows = {row["id"]: row for row in data["apps"]}
    expected = {"never": "never_used", "minute": "active", "twenty": "idle", "three": "idle", "thirty": "stale",
                "revoked": "revoked", "pending": "erasure_pending", "errors": "active", "old": "active"}
    for label, state in expected.items():
        assert rows["k-" + label]["state"] == state
        assert rows["k-" + label]["token_age_warning"] is (label == "old")
    assert rows["k-errors"]["errors"] is True
    assert rows["k-errors"]["store"]["last_error"] == "provider_limit"
    assert rows["k-minute"]["store"]["sessions"] == {"running": 2, "waiting_app": 1, "done": 3}
    assert rows["k-minute"]["store"]["usage"]["cost_usd"] == 0.5
    assert rows[fake_daemon.hub["key_id"]]["this_hub"] is True
    assert data["entries"][0]["paired"] == ["k-minute"]
    assert data["entries"][1]["paired"] == []
    assert "prefix" not in reply.text and "manifest" not in reply.text
    assert_no_secrets(claimed.bodies, fake_daemon.hub_token, "provider-secret-sentinel")


def test_revoke_confirmation_erasures_and_no_restore_call(claimed, fake_daemon):
    assert write(claimed, "DELETE", "k-minute/revoke", {}).status_code == 400
    assert not fake_daemon.apps_calls
    reply = write(claimed, "DELETE", "k-minute/revoke", {"confirm": True})
    assert reply.status_code == 204
    row = next(row for row in claimed.get("/api/apps").json()["apps"] if row["id"] == "k-minute")
    assert row["state"] == "erasure_pending" and row["erase_after"] is not None
    assert "k-minute" in {row["id"] for row in claimed.get("/api/apps/erasures").json()}
    calls = list(fake_daemon.apps_calls)
    for method in ("POST", "PUT", "GET", "DELETE"):
        assert write(claimed, method, "k-minute/restore", {"confirm": True}).status_code == 404
    assert fake_daemon.apps_calls == calls
    assert write(claimed, "DELETE", "k-minute/revoke", {"confirm": True}).status_code == 404
    assert_no_secrets(claimed.bodies, fake_daemon.hub_token)


def test_this_hub_revoke_is_refused_before_daemon_call(claimed, fake_daemon):
    kid = fake_daemon.hub["key_id"]
    reply = write(claimed, "DELETE", kid + "/revoke", {"confirm": True})
    assert reply.status_code == 409 and reply.json()["release_command"] == "harness hub release --confirm"
    assert not fake_daemon.apps_calls
    fake_daemon.apps_error = (409, {"detail": "Release with harness hub release --confirm",
                                    "error": {"code": "hub_key"}})
    reply = write(claimed, "DELETE", "k-never/revoke", {"confirm": True})
    assert reply.status_code == 409 and "harness hub release --confirm" in reply.text


def test_caps_and_retention_are_allowlisted_and_validated(claimed, fake_daemon):
    data = claimed.get("/api/apps/k-minute/limits").json()
    assert data["configured"] == {"max_running": None, "max_queued": 10}
    assert data["retention_days"] == 30 and data["parked"] == 1
    reply = write(claimed, "PUT", "k-minute/limits", {"max_running": 4, "max_queued": None})
    assert reply.status_code == 200 and reply.json()["effective"] == {"max_running": 4, "max_queued": 20}
    assert write(claimed, "PUT", "k-minute/limits", {"max_running": 5}).status_code == 200
    for value in (0, 101, True, 1.5, "4"):
        assert write(claimed, "PUT", "k-minute/limits", {"max_running": value}).status_code == 400
    for body in ({}, {"max_queued": 1001}, {"secret": "ignored"}):
        assert write(claimed, "PUT", "k-minute/limits", body).status_code == 400
    assert write(claimed, "PUT", "k-minute/retention", {"retention_days": 7}).status_code == 400
    for value in (7.5, None):
        reply = write(claimed, "PUT", "k-minute/retention", {"retention_days": value, "confirm": True})
        assert reply.status_code == 200 and reply.json() == {"id": "k-minute", "retention_days": value}
    for value in (0, -1, 36501, True, "7", float("inf")):
        # JSON's non-finite extension is sent as raw bytes for the malformed-value regression.
        raw = json.dumps({"retention_days": value, "confirm": True})
        reply = claimed._keep(claimed.client.put("/api/apps/k-minute/retention", content=raw, headers=ORIGIN))
        assert reply.status_code == 400
    assert_no_secrets(claimed.bodies, fake_daemon.hub_token)


@pytest.mark.parametrize("resource,method", [("erasures", "GET"), ("k-minute/limits", "GET"),
                                             ("k-minute/limits", "PUT"), ("k-minute/revoke", "DELETE"),
                                             ("k-minute/retention", "PUT")])
def test_apps_require_signed_in_owner_and_secure_writes(claimed, fake_daemon, resource, method):
    if method != "GET":
        for headers in ({}, {"X-Hub-Request": "1"}, {**ORIGIN, "Origin": "https://other.test"},
                        {**ORIGIN, "Origin": "https://" + HUB_HOST}):
            assert write(claimed, method, resource, {"confirm": True}, headers).status_code == 403
        assert not fake_daemon.apps_calls
    claimed.delete("/api/session")
    assert write(claimed, method, resource, {"confirm": True}).status_code == 401
    assert claimed.get("/api/apps").status_code == 401


def test_inventory_cache_shared_with_modules_and_keeps_last_good(claimed, fake_daemon):
    claimed.get("/api/modules")
    path = claimed.state_dir / INVENTORY_FILE
    before = path.read_bytes()
    fake_daemon.down = True
    bad = claimed.get("/api/apps").json()
    assert bad["state"] == "error" and bad["stale"] and bad["apps"] and bad["entries"]
    assert path.read_bytes() == before
    fake_daemon.down = False
    good = claimed.get("/api/apps").json()
    fake_daemon.apps_rows[0]["name"] = "ha-synthetic-secret"
    bad = claimed.get("/api/apps").json()
    assert bad["stale"] and bad["apps"] == good["apps"] and bad["fetched_at"] == good["fetched_at"]
    assert_no_secrets(claimed.bodies, fake_daemon.hub_token)


def test_apps_key_rejection_withholds_cache_and_release(claimed, fake_daemon):
    claimed.get("/api/apps")
    fake_daemon.release()
    reply = claimed.get("/api/apps")
    assert reply.status_code == 409 and "apps" not in reply.json()
    assert claimed.get("/api/claim").json()["state"] == "released"
    assert claimed.get("/api/apps/k-minute/limits").status_code == 409


def test_bad_body_route_and_daemon_errors_fail_closed(claimed, fake_daemon):
    for body in (b"{invalid", b"x" * 1025):
        reply = claimed._keep(claimed.client.request("DELETE", "/api/apps/k-minute/revoke", content=body, headers=ORIGIN))
        assert reply.status_code in (400, 413)
    for resource in ("k-minute/limits/extra", "k.minute/revoke", "arbitrary", "k-minute/restore"):
        assert write(claimed, "POST", resource, {"confirm": True}).status_code == 404
    assert not fake_daemon.apps_calls
    fake_daemon.down = True
    assert write(claimed, "DELETE", "k-minute/revoke", {"confirm": True}).status_code == 503
    fake_daemon.down = False
    for secret in (fake_daemon.hub_token, "ha-synthetic-secret", "hp-synthetic-secret"):
        fake_daemon.apps_error = (409, {"detail": secret})
        assert claimed.get("/api/apps/erasures").status_code == 500
    assert_no_secrets(claimed.bodies, fake_daemon.hub_token)


def test_projection_rejects_nonfinite_and_nested_metrics():
    shown = app_view({"id": "bad; command", "state": [], "token_age_seconds": float("nan"),
                      "store": {"sessions": {"running": {"secret": "ignored"}, "unexpected": 2},
                                "usage": {"requests": True, "cost_usd": float("inf")}}})
    assert shown["id"] == "" and shown["state"] == "unknown" and not shown["token_age_warning"]
    assert shown["store"]["sessions"] == {"running": None}
    assert shown["store"]["usage"]["requests"] is None and shown["store"]["usage"]["cost_usd"] is None
    assert app_view({"token_age_seconds": 90 * 86400})["token_age_warning"] is True
