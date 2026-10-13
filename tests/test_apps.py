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
    fake_daemon.apps_rows[0]["name"] = fake_daemon.hub_token
    bad = claimed.get("/api/apps").json()
    assert bad["state"] == "error" and bad["stale"]
    assert bad["apps"] == good["apps"] and bad["fetched_at"] == good["fetched_at"]
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
        reply = claimed._keep(claimed.client.request("DELETE", "/api/apps/k-minute/revoke",
                                                    content=body, headers=ORIGIN))
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


@pytest.mark.parametrize("operation", ["limits", "keys"])
def test_transient_detail_rejection_retains_claim_without_exposing_data(claimed, fake_daemon, monkeypatch, operation):
    from harness_hub.daemon import DaemonClient, Reply

    original = DaemonClient.apps

    async def rejected(self, token, action, app_id="", body=None):
        if action == operation:
            return Reply(401, {"detail": "synthetic rejection"})
        return await original(self, token, action, app_id, body)

    monkeypatch.setattr(DaemonClient, "apps", rejected)
    reply = claimed.get("/api/apps/k-minute/limits")
    assert reply.status_code == 409 and "configured" not in reply.json()
    assert claimed.get("/api/claim").json()["state"] == "claimed"
    assert_no_secrets(claimed.bodies, fake_daemon.hub_token)


def test_caps_write_does_not_read_keys_or_repeat_write(claimed, fake_daemon):
    reply = write(claimed, "PUT", "k-minute/limits", {"max_running": 3})
    assert reply.status_code == 200
    assert fake_daemon.apps_calls == [("PUT", "/api/admin/v1/apps/k-minute/limits")]


@pytest.mark.parametrize("name", ["ha-tools", "Home hp-printer helper", "Web client"])
def test_app_display_redaction_preserves_both_inventory_views_and_revoke(claimed, fake_daemon, name):
    row = fake_daemon.apps_rows[0]
    row.update(name=name, catalog_app_id="ho-catalog", scopes="ha-scopes", origins=["https://hp-x.example", "https://ho-mesite.example"])
    row["store"]["last_error"] = "hp-error"
    fake_daemon.app_entries[0]["manifest"]["app"]["display_name"] = "ha-entry"
    fake_daemon.app_entries[0]["catalog_app_id"] = "hp-entry"
    marker = "[withheld: token-shaped]"
    for path in ("/api/apps", "/api/modules"):
        reply = claimed.get(path)
        assert reply.status_code == 200 and reply.json()["state"] == "ready" and not reply.json()["stale"]
    shown = claimed.get("/api/apps").json()
    app = next(app for app in shown["apps"] if app["id"] == row["id"])
    assert app["name"] == (name if name == "Web client" else marker)
    assert app["catalog_app_id"] == app["scopes"] == app["store"]["last_error"] == marker
    assert app["origins"] == ["https://hp-x.example", marker]
    assert shown["entries"][0]["name"] == shown["entries"][0]["catalog_app_id"] == marker
    assert write(claimed, "DELETE", row["id"] + "/revoke", {"confirm": True}).status_code == 204
    erased = next(app for app in claimed.get("/api/apps/erasures").json() if app["id"] == row["id"])
    assert erased["name"] == app["name"]
    assert_no_secrets(claimed.bodies, fake_daemon.hub_token)


@pytest.mark.parametrize("credential", ["key", "verifier"])
def test_app_display_exact_secrets_are_withheld_even_beyond_display_limit(claimed, fake_daemon, credential):
    verifier = "synthetic-verifier-sentinel"
    store = claimed.client.app.fastapi.state.store
    store.update(claim={"verifier": verifier})
    secret = fake_daemon.hub_token if credential == "key" else verifier
    fake_daemon.apps_rows[0]["name"] = "x" * 220 + secret
    data = claimed.get("/api/apps").json()
    assert data["state"] == "error" and data["apps"] is None
    # Apps display data cannot disable Module controls.
    assert claimed.get("/api/modules").json()["state"] == "ready"
    fake_daemon.apps_error = (200, [{"id": "k-pending", "name": "x" * 220 + secret}])
    assert claimed.get("/api/apps/erasures").status_code == 500
    assert_no_secrets(claimed.bodies, fake_daemon.hub_token, verifier)


@pytest.mark.parametrize("body", [None, {}, {"erasures": []}, "invalid"])
def test_invalid_erasures_reply_is_an_error_not_an_empty_list(claimed, fake_daemon, body):
    fake_daemon.apps_error = (200, body)
    reply = claimed.get("/api/apps/erasures")
    assert reply.status_code == 502 and "invalid erasure" in reply.json()["detail"]


def test_invalid_erasure_ids_are_skipped(claimed, fake_daemon):
    fake_daemon.apps_error = (200, [{"id": value, "name": "Bad"} for value in
                                  (None, "", "k; command", "x" * 81, [], 4)] +
                             [{"id": "k-valid", "name": "Valid", "erase_after": 123}])
    data = claimed.get("/api/apps/erasures").json()
    assert len(data) == 1 and data[0]["id"] == "k-valid"


@pytest.mark.parametrize("body", [None, [], {}, {"configured": {}}, {"effective": {}},
                                  {"configured": [], "effective": {}}, {"configured": {}, "effective": None},
                                  {"configured": {}, "effective": {}},
                                  {"configured": {"max_running": None, "max_queued": "10"},
                                   "effective": {"max_running": 2, "max_queued": 10}},
                                  {"configured": {"max_running": None, "max_queued": 10},
                                   "effective": {"max_running": True, "max_queued": 10}}])
@pytest.mark.parametrize("method", ["GET", "PUT"])
def test_invalid_limits_reply_is_an_error_before_loading_caps(claimed, fake_daemon, body, method):
    fake_daemon.apps_error = (200, body)
    reply = claimed.get("/api/apps/k-minute/limits") if method == "GET" else write(
        claimed, "PUT", "k-minute/limits", {"max_running": 3})
    assert reply.status_code == 502 and "configured" not in reply.json()
    # The detail reads the key list alongside the limits (#56), but nothing repeats after the invalid answer.
    assert len(fake_daemon.apps_calls) == (2 if method == "GET" else 1)
