"""All five route call sites confirm rejected keys before releasing the claim."""

import pytest

from harness_hub.daemon import DaemonClient, DaemonUnreachable, Reply

from .conftest import HUB_HOST, assert_no_secrets

CASES = [
    ("apps", "limits_save", "PUT", "/api/apps/k-minute/limits", {"max_running": 3},
     [("apps", "limits_save")]),
    ("apps", "keys", "GET", "/api/apps/k-minute/limits", None,
     [("apps", "limits"), ("apps", "keys")]),
    ("pairing", "list", "POST", "/api/pairing/pr-synthetic/approve", {"match": "123456"},
     [("pairing", "list")]),
    ("administration", "accounts", "GET", "/api/manage/accounts", None,
     [("administration", "accounts")]),
    ("settings", "restart", "POST", "/api/settings/restart", {"revision": 1, "confirm": True},
     [("settings", "restart")]),
]


@pytest.mark.parametrize("confirmation", [200, 401, "unreachable"], ids=["transient", "released", "unreachable"])
@pytest.mark.parametrize("client_method,operation,method,path,body,expected_calls", CASES,
                         ids=["apps-reply", "apps-keys", "pairing", "administration", "settings"])
def test_route_key_refusal(claimed, fake_daemon, monkeypatch, confirmation,
                          client_method, operation, method, path, body, expected_calls):
    calls = []
    verifier = "synthetic-verifier-sentinel"
    claimed.client.app.fastapi.state.store.update(claim={"verifier": verifier})
    original = getattr(DaemonClient, client_method)

    async def rejected(self, token, action, *args, **kwargs):
        calls.append((client_method, action))
        assert token == fake_daemon.hub_token
        if action == operation:
            return Reply(401, {"detail": fake_daemon.hub_token + verifier})
        return await original(self, token, action, *args, **kwargs)

    async def confirm(self, token, path=""):
        calls.append(("admin", "confirm"))
        assert token == fake_daemon.hub_token and path == ""
        if confirmation == "unreachable":
            raise DaemonUnreachable("synthetic refusal confirmation failure")
        return Reply(confirmation, {})

    monkeypatch.setattr(DaemonClient, client_method, rejected)
    monkeypatch.setattr(DaemonClient, "admin", confirm)
    reply = claimed._keep(claimed.client.request(
        method, path, json=body, headers={"Origin": "http://" + HUB_HOST, "X-Hub-Request": "1"}))
    if confirmation == "unreachable":
        assert reply.status_code == 503
    else:
        assert reply.status_code == 409
        assert reply.json() == {"detail": "The daemon refused this Hub's key. Check Overview before trying again."}
    assert claimed.get("/api/claim").json()["state"] == ("released" if confirmation == 401 else "claimed")
    # No approval, repeated write, or further detail read follows the rejection.
    assert calls == [*expected_calls, ("admin", "confirm")]
    assert not claimed.get("/api/settings/health").json()["restarting"]
    assert_no_secrets(claimed.bodies, fake_daemon.hub_token, verifier)
