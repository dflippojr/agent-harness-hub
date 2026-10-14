"""Every route call site confirms a rejected key before releasing the claim."""

import pytest

from harness_hub.daemon import DaemonClient, DaemonUnreachable, Reply

from .conftest import HUB_HOST, assert_no_secrets
from .marketplace_fake import manifest_entry

TRANSIENT, RELEASED, UNREACHABLE = "transient", "released", "unreachable"


def arm_body(claimed, fake_daemon):
    fake_daemon.app_entries = [manifest_entry("test.example.native", "Synthetic native")]
    entry = claimed.get("/api/marketplace").json()["entries"][0]
    return {"catalog_app_id": entry["catalog_app_id"], "scopes": ["sessions"],
            "consent_digest": entry["consent_digest"]}


# (id, DaemonClient method, the action it rejects ("" for a method with none), HTTP method, path, body or a function
# of (hub, fake daemon) that makes it, the calls expected before the confirmation, the statuses for transient,
# released and unreachable, and what a 200 must show). A call site that confirms inside its own try answers an
# unreachable confirmation as the daemon not answering (200 with the error shown) instead of a 503.
REFUSED, DOWN = (409, 409, 503), (409, 409, 200)
CASES = [
    ("apps-reply", "apps", "limits_save", "PUT", "/api/apps/k-minute/limits", {"max_running": 3},
     [("apps", "limits_save")], REFUSED, {}),
    ("apps-keys", "apps", "keys", "GET", "/api/apps/k-minute/limits", None,
     [("apps", "limits"), ("apps", "keys")], REFUSED, {}),
    ("apps-limits", "apps", "limits", "GET", "/api/apps/k-minute/limits", None,
     [("apps", "limits"), ("apps", "keys")], REFUSED, {}),
    ("pairing", "pairing", "list", "POST", "/api/pairing/pr-synthetic/approve", {"match": "123456"},
     [("pairing", "list")], REFUSED, {}),
    ("administration", "administration", "accounts", "GET", "/api/manage/accounts", None,
     [("administration", "accounts")], REFUSED, {}),
    ("settings", "settings", "restart", "POST", "/api/settings/restart", {"revision": 1, "confirm": True},
     [("settings", "restart")], REFUSED, {}),
    ("modules", "inventory", "", "GET", "/api/modules", None,
     [("inventory", "")], DOWN, {"state": "error", "reachable": False}),
    ("marketplace", "pairing", "list", "GET", "/api/marketplace", None,
     [("pairing", "list")], DOWN, {"slots_error": "Armed slots could not be read: the daemon did not answer."}),
    ("arm-inventory", "inventory", "", "POST", "/api/marketplace/arm", arm_body,
     [("inventory", "")], REFUSED, {}),
    ("arm", "pairing", "arm", "POST", "/api/marketplace/arm", arm_body,
     [("pairing", "arm")], REFUSED, {}),
    ("overview", "admin", "", "GET", "/api/overview", None,
     [("admin", ""), ("admin", "/hub-claim")], (200, 200, 200), {"ownership": None}),
]


@pytest.mark.parametrize("confirmation", [200, 401, "unreachable"], ids=[TRANSIENT, RELEASED, UNREACHABLE])
@pytest.mark.parametrize("case", CASES, ids=[case[0] for case in CASES])
def test_route_key_refusal(claimed, fake_daemon, monkeypatch, confirmation, case):
    _, client_method, operation, method, path, body, expected_calls, statuses, shown = case
    if callable(body):
        body = body(claimed, fake_daemon)
    if method == "GET":
        assert claimed.get(path).status_code == 200  # a good answer first, so a refusal has something to withhold
    calls = []
    verifier = "synthetic-verifier-sentinel"
    claimed.client.app.fastapi.state.store.update(claim={"verifier": verifier})
    original = getattr(DaemonClient, client_method)

    async def rejected(self, token, action="", *args, **kwargs):
        calls.append((client_method, action))
        assert token == fake_daemon.hub_token
        if action == operation:
            return Reply(401, {"detail": fake_daemon.hub_token + verifier})
        return await original(self, token, action, *args, **kwargs)

    async def admin(self, token, route=""):
        assert token == fake_daemon.hub_token
        if client_method == "admin" and len(calls) < len(expected_calls):
            # Overview's own reads: the discovery is refused and the claim record answers.
            calls.append(("admin", route))
            return Reply(401 if route == "" else 200, {"detail": fake_daemon.hub_token + verifier})
        calls.append(("admin", "confirm"))
        assert route == ""
        if confirmation == "unreachable":
            raise DaemonUnreachable("synthetic refusal confirmation failure")
        return Reply(confirmation, {})

    if client_method != "admin":
        monkeypatch.setattr(DaemonClient, client_method, rejected)
    monkeypatch.setattr(DaemonClient, "admin", admin)
    reply = claimed._keep(claimed.client.request(
        method, path, json=body, headers={"Origin": "http://" + HUB_HOST, "X-Hub-Request": "1"}))
    status = statuses[[200, 401, "unreachable"].index(confirmation)]
    assert reply.status_code == status, reply.text
    if status == 409:
        assert reply.json() == {"detail": "The daemon refused this Hub's key. Check Overview before trying again."}
    elif status == 200:
        assert {name: reply.json()[name] for name in shown} == shown
    assert claimed.get("/api/claim").json()["state"] == ("released" if confirmation == 401 else "claimed")
    # No approval, repeated write, or further detail read follows the rejection.
    assert calls == [*expected_calls, ("admin", "confirm")]
    assert not claimed.get("/api/settings/health").json()["restarting"]
    assert_no_secrets(claimed.bodies, fake_daemon.hub_token, verifier)
