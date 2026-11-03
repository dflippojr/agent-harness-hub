"""What every `/api` route guarantees, in one table: sign-in, the write headers, a claim and a body limit.

The route families each enforce part of this themselves (`server.py`), so one row per concrete request is checked here
instead of a copy in every page's tests. `tests/test_refusal.py` is the sibling table for the daemon's 401.
"""

from __future__ import annotations

import re
from urllib.parse import urlencode, urlsplit

import pytest
from starlette.routing import Match

from harness_hub.daemon import ADMIN_ROUTES

from .conftest import HUB_HOST

TARGETS = {"user_id": "u-synthetic", "pid": "p-synthetic", "slug": "synthetic", "name": "synthetic",
           "credential_id": "pc-synthetic", "project": "synthetic", "scan_id": "scan-synthetic",
           "candidate_id": "c-synthetic"}
ARM = {"catalog_app_id": "test.example.native", "scopes": ["sessions"]}
MATCH = {"match": "123456"}
CONFIRM = {"confirm": True}
REVISION = {"revision": 3, "confirm": True}


def manage(operation: str) -> tuple[str, str, dict | None, str]:
    method, template = ADMIN_ROUTES[operation]
    query = urlencode({name: TARGETS[name] for name in re.findall(r"{(\w+)}", template)})
    kind = "read" if method == "GET" else "write"
    return method, f"/api/manage/{operation}" + (f"?{query}" if query else ""), None if kind == "read" else {}, kind


# (method, path, body, kind): one row per concrete request. A catch-all route has a row for every request it serves.
ROUTES = [
    ("GET", "/api/session", None, "read"),
    ("POST", "/api/session", {"code": "synthetic"}, "write"),
    ("DELETE", "/api/session", None, "write"),
    ("GET", "/api/claim", None, "read"),
    ("POST", "/api/claim", None, "write"),
    ("DELETE", "/api/claim", None, "write"),
    ("GET", "/api/overview", None, "read"),
    ("GET", "/api/modules", None, "read"),
    ("GET", "/api/apps", None, "read"),
    ("GET", "/api/spend", None, "read"),
    ("GET", "/api/apps/erasures", None, "read"),
    ("GET", "/api/apps/k-minute/limits", None, "read"),
    ("PUT", "/api/apps/k-minute/limits", {"max_running": 3}, "write"),
    ("DELETE", "/api/apps/k-minute/revoke", CONFIRM, "write"),
    ("PUT", "/api/apps/k-minute/retention", {"retention_days": 7, **CONFIRM}, "write"),
    ("GET", "/api/marketplace", None, "read"),
    ("POST", "/api/marketplace/arm", ARM, "write"),
    ("GET", "/api/pairing", None, "read"),
    ("POST", "/api/pairing/pr-synthetic/approve", MATCH, "write"),
    ("POST", "/api/pairing/pr-synthetic/confirm", MATCH, "write"),
    ("POST", "/api/pairing/pr-synthetic/deny", CONFIRM, "write"),
    ("GET", "/api/manage/catalog", None, "read"),
    *(manage(operation) for operation in ADMIN_ROUTES),
    ("GET", "/api/settings/health", None, "read"),
    ("GET", "/api/settings/schema", None, "read"),
    ("GET", "/api/settings/config", None, "read"),
    ("POST", "/api/settings/validate", {"revision": 3, "changes": {}}, "write"),
    ("PATCH", "/api/settings/config", {"revision": 3, "changes": {}}, "write"),
    ("POST", "/api/settings/rollback", REVISION, "write"),
    ("POST", "/api/settings/restart", REVISION, "write"),
    ("GET", "/api/settings/smart-approvals", None, "read"),
    ("PUT", "/api/settings/smart-approvals", {"mode": "shadow"}, "write"),
    ("GET", "/api/settings/backends", None, "read"),
    ("PUT", "/api/settings/backend", {"name": "sample", "model": "synthetic"}, "write"),
    ("GET", "/api/settings/memory", None, "read"),
    ("PUT", "/api/settings/memory", {"content": "Synthetic", "summary": "Synthetic"}, "write"),
    ("GET", "/api/settings/github", None, "read"),
    ("PUT", "/api/settings/github", {"enabled": True}, "write"),
    ("GET", "/api/settings/google", None, "read"),
    ("POST", "/api/settings/notify", {}, "write"),
]

# What a route does without a daemon key. These are the only exceptions, so a new route is held to every rule below.
PUBLIC = {("GET", "/api/session"), ("POST", "/api/session")}  # the page reads who is signed in, then signs in
NO_CLAIM = PUBLIC | {("DELETE", "/api/session"), ("GET", "/api/claim"), ("POST", "/api/claim"),
                     ("DELETE", "/api/claim"), ("GET", "/api/overview"), ("GET", "/api/settings/health"),
                     ("GET", "/api/manage/catalog")}
NO_ORIGIN_NEEDED = {("POST", "/api/session"), ("DELETE", "/api/session"), ("POST", "/api/claim"),
                    ("DELETE", "/api/claim")}  # only the header and, when sent, a same-origin Origin
NO_BODY = {("DELETE", "/api/session"), ("POST", "/api/claim"), ("DELETE", "/api/claim")}  # never read a body

WRITE = {"X-Hub-Request": "1", "Origin": "http://" + HUB_HOST}
BIGGER_THAN_ANY_LIMIT = b"x" * (300 * 1024)  # Settings, the largest, takes 256 KiB


def send(hub, row, headers=None, content=None):
    method, path, body, kind = row
    if headers is None:
        headers = WRITE if kind == "write" else {}
    options = {"content": content} if content is not None else {"json": body}
    return hub.client.request(method, path, headers=headers, **options)


def label(row) -> str:
    return f"{row[0]} {row[1]}"


def key(row) -> tuple[str, str]:
    return row[0], urlsplit(row[1]).path


def template(app, row) -> str | None:
    scope = {"type": "http", "method": row[0], "path": urlsplit(row[1]).path}
    return next((route.path for route in app.routes if route.matches(scope)[0] == Match.FULL), None)


def test_the_table_names_every_api_route(hub):
    routes = {route.path for route in hub.client.app.fastapi.routes if route.path.startswith("/api")}
    named = {template(hub.client.app.fastapi, row) for row in ROUTES}
    missing, unserved = sorted(routes - named), sorted(map(str, named - routes))
    assert named == routes, f"add a ROUTES row for {missing}; no route serves {unserved}"
    assert len(set(map(label, ROUTES))) == len(ROUTES)
    assert {key(row) for row in ROUTES} >= NO_CLAIM | NO_ORIGIN_NEEDED | NO_BODY  # no exception names a missing row


def test_every_console_route_needs_sign_in(hub, fake_daemon):
    for row in ROUTES:
        if key(row) in PUBLIC:
            continue
        reply = send(hub, row)
        assert reply.status_code == 401, label(row)
        assert reply.json()["signin_command"] == "python -m harness_hub signin-link", label(row)
    assert fake_daemon.sent == []


def test_every_write_needs_the_hub_header_and_same_origin(claimed, fake_daemon):
    before = list(fake_daemon.sent)
    foreign, https = {**WRITE, "Origin": "https://other.test"}, {**WRITE, "Origin": "https://" + HUB_HOST}
    for row in ROUTES:
        if row[3] != "write":
            continue
        variants = [{}, {"Origin": WRITE["Origin"]}, foreign, https]
        if key(row) not in NO_ORIGIN_NEEDED:
            variants.append({"X-Hub-Request": "1"})
        for headers in variants:
            assert send(claimed, row, headers).status_code == 403, f"{label(row)} with {headers}"
    assert fake_daemon.sent == before


def test_every_daemon_route_needs_a_claim(hub, fake_daemon):
    hub.sign_in()
    for row in ROUTES:
        if key(row) not in NO_CLAIM:
            reply = send(hub, row)
            # claimed false is what lets each page show Claim this Hub first instead of the refusal's sentence.
            assert (reply.status_code, reply.json().get("claimed")) == (409, False), label(row)
    assert fake_daemon.sent == []


def test_oversized_write_bodies_are_refused(claimed, fake_daemon):
    before = list(fake_daemon.sent)
    for row in ROUTES:
        if row[3] == "write" and key(row) not in NO_BODY:
            assert send(claimed, row, content=BIGGER_THAN_ANY_LIMIT).status_code == 413, label(row)
    assert fake_daemon.sent == before


# The five routes that read a JSON body after their own same-origin check: (row, byte cap, 403 detail, 413 detail,
# 400 detail). Administration answers an unparsable body with the same 400 as an invalid field.
BODY_READERS = [
    (("POST", "/api/marketplace/arm", ARM, "write"), 4096, "Setting up an app requires a same-origin Origin",
     "Set-up body is too large", "Set-up body must be JSON"),
    (("PUT", "/api/apps/k-minute/limits", {"max_running": 3}, "write"), 1024,
     "Apps writes require a same-origin Origin", "Apps body is too large", "Apps body must be JSON"),
    (("POST", "/api/pairing/pr-synthetic/approve", MATCH, "write"), 1024,
     "Pairing decisions require a same-origin Origin", "Pairing body is too large", "Pairing body must be JSON"),
    (manage("google_invite"), 65536, "Administration writes require a same-origin Origin",
     "Administration body is too large", "Invalid administration fields; check required values and confirmation"),
    (("POST", "/api/settings/validate", {"revision": 3, "changes": {}}, "write"), 262144,
     "Settings writes require a same-origin Origin", "Settings body is too large", "Settings body must be JSON"),
]


@pytest.mark.parametrize(("row", "cap", "no_origin", "too_large", "not_json"), BODY_READERS,
                         ids=[label(reader[0]) for reader in BODY_READERS])
def test_each_body_reader_keeps_its_refusals(claimed, fake_daemon, row, cap, no_origin, too_large, not_json):
    before = list(fake_daemon.sent)
    nested = b"[" * 10_000  # deeper than json's recursion limit; over the cap of the routes that take 4 KiB or less
    cases = [({"X-Hub-Request": "1"}, b"{}", 403, no_origin),
             (WRITE, b" " * cap, 400, not_json),  # the cap itself is read, then refused only as empty JSON
             (WRITE, b" " * (cap + 1), 413, too_large),
             (WRITE, nested, 413 if len(nested) > cap else 400, too_large if len(nested) > cap else not_json),
             (WRITE, b"{", 400, not_json)]
    for headers, content, status, detail in cases:
        reply = send(claimed, row, headers, content)
        assert (reply.status_code, reply.json()["detail"]) == (status, detail), f"{label(row)} {content[:8]!r}"
    assert fake_daemon.sent == before
