"""Who may use the Hub, and what may leave it: sign-in, CSRF, Host checks, headers and the response guard."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from harness_hub import auth
from harness_hub.auth import Sessions, create_signin_code
from harness_hub.guard import ResponseGuard, leaks


def test_session_endpoint_and_shell_are_public(hub):
    assert hub.get("/api/session").json()["signed_in"] is False
    page = hub.get("/")
    assert page.status_code == 200 and "<title>Hub</title>" in page.text
    assert hub.get("/healthz").json()["ok"] is True


def test_sign_in_link_works_once(hub):
    code = create_signin_code(hub.state_dir)
    first = hub.post("/api/session", json={"code": code})
    assert first.status_code == 204
    cookie = first.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie and "path=/" in cookie
    hub.client.headers["X-Hub-Session"] = first.headers["x-hub-session"]
    assert hub.get("/api/session").json()["signed_in"] is True
    hub.client.cookies.clear()
    assert hub.post("/api/session", json={"code": code}).status_code == 401


def test_sign_out(hub):
    hub.sign_in()
    assert hub.delete("/api/session").status_code == 204
    hub.client.cookies.clear()
    assert hub.get("/api/overview").status_code == 401


def test_expired_code_and_session(tmp_path):
    now = [1000.0]
    code = create_signin_code(tmp_path, now=now[0])
    sessions = Sessions(tmp_path, clock=lambda: now[0])
    now[0] += auth.CODE_TTL_SECONDS + 1
    assert sessions.redeem_code(code) is None
    session = sessions.redeem_code(create_signin_code(tmp_path, now=now[0]))
    assert sessions.valid(*session)
    now[0] += auth.SESSION_TTL_SECONDS + 1
    assert not sessions.valid(*session)


def test_a_tailnet_binding_keeps_its_login_and_ends_alone(tmp_path):
    """#169: the login lives on the binding, so revoking a tailnet tab leaves the session's link tabs alone."""
    now = [1000.0]
    sessions = Sessions(tmp_path, clock=lambda: now[0])
    cookie, link = sessions.redeem_code(create_signin_code(tmp_path, now=now[0]))
    expiry = sessions._sessions[auth._digest(cookie)][0]
    now[0] += 60
    joined, tab = sessions.create(cookie, "owner@example.test")
    assert joined == cookie and sessions.valid(cookie, tab)
    assert sessions.login(cookie, tab) == "owner@example.test"
    assert sessions.login(cookie, link) is None  # a link's binding is never checked against the daemon
    assert sessions.login(cookie, "not-a-binding") is None and sessions.login(None, tab) is None
    sessions.revoke(cookie, tab)
    assert not sessions.valid(cookie, tab) and sessions.login(cookie, tab) is None
    assert sessions.valid(cookie, link) and sessions._sessions[auth._digest(cookie)][0] == expiry
    sessions.revoke(cookie, tab)  # already gone: nothing else changes
    sessions.revoke(None, link)
    assert sessions.valid(cookie, link)


def test_revoking_a_sessions_last_binding_ends_the_session(tmp_path):
    sessions = Sessions(tmp_path)
    cookie, tab = sessions.create(login="owner@example.test")
    sessions.revoke(cookie, tab)
    assert auth._digest(cookie) not in sessions._sessions
    # The cookie names no session now, so a link redeemed with it starts a new one.
    fresh, link = sessions.redeem_code(create_signin_code(tmp_path), cookie)
    assert fresh != cookie and sessions.valid(fresh, link) and sessions.login(fresh, link) is None


def test_a_tailnet_binding_records_when_its_login_was_last_listed(tmp_path):
    """#177: the server bounds an outage's grace from the last time the daemon listed the binding's login."""
    now = [1000.0]
    sessions = Sessions(tmp_path, clock=lambda: now[0])
    cookie, link = sessions.redeem_code(create_signin_code(tmp_path, now=now[0]))
    joined, tab = sessions.create(cookie, "owner@example.test")
    now[0] += 30
    assert sessions.binding(cookie, tab) == ("owner@example.test", 30.0)  # the sign-in was a listing
    assert sessions.binding(cookie, link) == (None, 30.0)
    sessions.confirm(cookie, tab)
    now[0] += 5
    assert sessions.binding(cookie, tab) == ("owner@example.test", 5.0)
    assert sessions.binding(cookie, link) == (None, 35.0)
    assert sessions.binding(cookie, "not-a-binding") is None and sessions.binding(None, tab) is None
    sessions.revoke(cookie, tab)
    sessions.confirm(cookie, tab)  # a binding that ended stays ended
    assert sessions.binding(cookie, tab) is None and sessions.valid(cookie, link)


def test_codes_are_stored_hashed(tmp_path):
    code = create_signin_code(tmp_path)
    assert code not in (tmp_path / "signin-codes.json").read_text(encoding="utf-8")


def test_failed_sign_ins_are_throttled(hub):
    for _ in range(auth.FAILURES_PER_MINUTE):
        assert hub.post("/api/session", json={"code": "x" * 43}).status_code == 401
    code = create_signin_code(hub.state_dir)
    assert hub.post("/api/session", json={"code": "x" * 43}).status_code == 429
    assert hub.post("/api/session", json={"code": code}).status_code == 204


def test_unknown_host_is_refused(hub):
    response = hub.client.get("/", headers={"Host": "rebind.example:8120"})
    assert response.status_code == 421


def test_security_headers(hub):
    page = hub.get("/")
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
    assert "script-src 'self'" in page.headers["content-security-policy"]
    assert page.headers["x-content-type-options"] == "nosniff"
    assert page.headers["referrer-policy"] == "no-referrer"
    assert hub.get("/api/session").headers["cache-control"] == "no-store"


def test_overview_drops_fields_it_does_not_know(hub, fake_daemon):
    hub.sign_in()
    fake_daemon.approve(hub.post("/api/claim").json()["request_id"])
    hub.wait_for("claimed")
    body = hub.get("/api/overview")
    assert body.status_code == 200
    assert "debug_prefix" not in body.text and "operations" not in body.text


@pytest.mark.parametrize("text, leaked", [
    ('{"token": "ho-' + "AbCdEfGh" * 5 + 'Ijk"}', True),
    ('"ha-' + "1234abcd_-" * 5 + '"', True),
    ('"hk-' + "Z" * 43 + '"', True),
    ('"code hp-9f8e7d6c9f8e7d6c9f8e7d6c"', True),
    ('"code hrp-9f8e7d6c9f8e7d6c9f8e7d6c"', True),
    # Display prefixes and ordinary words are redacted field by field in views.py, not by the guard.
    ('{"prefix": "ho-LW5TqUX"}', False),
    ('"ha-1234abcd"', False),
    ('"code hp-9f8e7d6c"', False),
    ('{"login": "ha-jung.kim@example.test", "model": "hp-instruct-7b", "labels": ["ha-core", "hp-laserjet"]}', False),
    ('"ha-' + "x" * 42 + '"', False),
    # JSON escapes put a letter before the token in the raw body; the decoded string still counts.
    ('{"model": "padding\\nha-' + "S" * 43 + '"}', True),
    ('{"model": "padding\\tho-' + "S" * 43 + '"}', True),
    ('{"padding\\nhp-' + "c" * 24 + '": 1}', True),
    ('{"name": "echo-server", "id": "k-5a433700", "photo-album": 1}', False),
    ('{"cmd": "harness hub approve pr-8921239e4d65160ca49d00ec --match 763647"}', False),
])
def test_leak_detection(text, leaked):
    assert leaks(text.encode(), []) is leaked


def test_guard_withholds_a_leaking_response():
    app = FastAPI()

    @app.get("/api/leak")
    async def leak():
        return {"value": "plain-secret-value"}

    @app.get("/api/token-shaped")
    async def token_shaped():
        return {"value": "ho-" + "abcdefgh" * 6}

    @app.get("/api/fine")
    async def fine():
        return {"value": "fine"}

    client = TestClient(ResponseGuard(app, lambda: ["plain-secret-value"]))
    for path in ("/api/leak", "/api/token-shaped"):
        response = client.get(path)
        assert response.status_code == 500
        assert "withheld" in response.json()["detail"]
    assert client.get("/api/fine").json() == {"value": "fine"}
