"""A fake Agent Harness daemon with the routes and answers the Hub relies on (synthetic data only).

Its shapes follow agent-harness docs/app-api.md (Zero-touch pairing, Hub claim) and docs/admin-api.md (Hub claim).
The real daemon is exercised by tests/e2e.
"""

from __future__ import annotations

import base64
import hashlib
import re
import secrets
import time
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from harness_hub.auth import create_signin_code
from harness_hub.config import Settings
from harness_hub.server import create_app

HUB_HOST = "127.0.0.1:8120"
TOKEN_SHAPE = re.compile(r"(?<![A-Za-z0-9])(?:ha|ho|hp)-[A-Za-z0-9_\-]{4,}")


def refusal(status: int, detail: str, code: str = "") -> JSONResponse:
    return JSONResponse({"detail": detail, "error": {"code": code, "message": detail, "retryable": False}},
                        status_code=status)


class FakeDaemon:
    def __init__(self):
        self.requests: dict[str, dict] = {}
        self.hub: dict | None = None
        self.hub_token = ""
        self.live_tokens: set[str] = set()
        self.minted: list[str] = []
        self.redeem_role = "hub"
        self.down = False
        self.app = self._build()

    # owner actions on the daemon host
    def approve(self, rid: str):
        self.requests[rid]["state"] = "approved"

    def deny(self, rid: str):
        self.requests[rid]["state"] = "denied"

    def expire(self, rid: str):
        self.requests[rid]["state"] = "expired"

    def claim_elsewhere(self):
        self.hub = {"key_id": "k-other000", "name": "Other Hub", "kind": "native", "origin": "",
                    "request_id": "pr-other", "claimed_at": time.time()}

    def release(self):
        self.live_tokens.discard(self.hub_token)
        self.hub = None

    def latest(self) -> dict:
        return list(self.requests.values())[-1]

    def _build(self) -> FastAPI:
        app = FastAPI()

        @app.get("/health")
        async def health():
            return {"ok": True, "profile": "service", "release": "0.9.0", "build_id": "2026.09.17.1",
                    "protocols": {"app": {"min": 1, "max": 2}, "admin": {"min": 1, "max": 2}},
                    "capabilities": {"profile": "service", "required": {"sessions": True, "storage": True},
                                     "modules": {"web": False, "hub": True}, "hosted_backends": ["claude"],
                                     "config_registry": True},
                    "config": {"revision": 3, "confirmed": True, "supervised_restart": False, "recovery": None}}

        @app.post("/api/v1/pair/requests", status_code=201)
        async def create(request: Request):
            body = await request.json()
            assert body["kind"] == "hub" and "scopes" not in body and body["code_challenge_method"] == "S256"
            assert "origin" not in {k.lower() for k in request.headers}
            if self.hub is not None:
                return refusal(409, "this harness already has a Hub; the owner releases it on the daemon host "
                                    "with `harness hub release --confirm`", "hub_claimed")
            rid = "pr-" + secrets.token_hex(12)
            self.requests[rid] = {"id": rid, "challenge": body["code_challenge"], "state": "pending",
                                  "match_code": f"{secrets.randbelow(10**6):06d}", "name": body["name"]}
            row = self.requests[rid]
            return JSONResponse({"id": rid, "state": "pending", "match_code": row["match_code"],
                                 "expires_at": time.time() + 600, "browser": False}, status_code=201)

        @app.post("/api/v1/pair/requests/{rid}/token")
        async def redeem(rid: str, request: Request):
            row = self.requests.get(rid)
            if row is None:
                return refusal(404, "no pairing request has that id", "not_found")
            verifier = (await request.json()).get("code_verifier", "")
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
            if row["state"] == "expired":
                return refusal(400, "this pairing request expired")
            if row["state"] == "redeemed":
                return refusal(400, "this pairing request was already redeemed")
            if row["state"] == "denied":
                return refusal(403, "the owner denied this pairing request")
            if challenge != row["challenge"]:
                return refusal(400, "invalid code_verifier")
            if row["state"] != "approved":
                return refusal(409, "waiting for the owner to approve this pairing request", "pairing_pending")
            if self.hub is not None:
                row["state"] = "denied"
                return refusal(409, "this harness already has a Hub", "hub_claimed")
            row["state"] = "redeemed"
            token = "ho-" + secrets.token_urlsafe(32)
            self.minted.append(token)
            key_id = "k-" + secrets.token_hex(4)
            if self.redeem_role == "hub":
                self.hub_token = token
                self.live_tokens.add(token)
                self.hub = {"key_id": key_id, "name": row["name"], "kind": "native", "origin": "",
                            "request_id": rid, "claimed_at": time.time()}
            return JSONResponse({"token": token, "api_version": "1.25", "app": {
                "id": key_id, "name": row["name"], "prefix": token[:10], "scopes": "admin", "kind": "owner",
                "role": self.redeem_role, "origins": [], "catalog_app_id": ""}}, status_code=201)

        def owner(request: Request) -> bool:
            auth = request.headers.get("authorization", "")
            return auth.startswith("Bearer ") and auth[7:] in self.live_tokens

        @app.get("/api/admin/v1")
        async def discovery(request: Request):
            if not owner(request):
                return JSONResponse({"detail": "requests from this machine need the local owner token"}, 401)
            # A field the Hub must not pass through: the key list's display prefix.
            return {"api_version": "1.24", "hub": {"claimed": self.hub is not None},
                    "operations": [{"method": "GET", "path": "/api/admin/v1/keys"}],
                    "debug_prefix": self.hub_token[:10]}

        @app.get("/api/admin/v1/hub-claim")
        async def hub_claim(request: Request):
            if not owner(request):
                return JSONResponse({"detail": "requests from this machine need the local owner token"}, 401)
            return {"claimed": self.hub is not None, "hub": self.hub, "requests": []}

        return app

    def transport(self) -> httpx.AsyncBaseTransport:
        fake = self

        class Transport(httpx.AsyncBaseTransport):
            def __init__(self):
                self.inner = httpx.ASGITransport(app=fake.app)

            async def handle_async_request(self, request):
                if fake.down:
                    raise httpx.ConnectError("refused", request=request)
                return await self.inner.handle_async_request(request)

        return Transport()


class HubClient:
    """A browser stand-in: it records every body the Hub sends so tests can grep them for secrets."""

    def __init__(self, client: TestClient, state_dir: Path):
        self.client = client
        self.state_dir = state_dir
        self.bodies: list[str] = []

    def _keep(self, response):
        self.bodies.append(response.text)
        # Every header but the session cookie, a random value that is not a daemon credential.
        self.bodies.extend(v for k, v in response.headers.items() if k.lower() != "set-cookie")
        return response

    def get(self, path, **kw):
        return self._keep(self.client.get(path, **kw))

    def post(self, path, **kw):
        headers = {"X-Hub-Request": "1", **kw.pop("headers", {})}
        return self._keep(self.client.post(path, headers=headers, **kw))

    def delete(self, path, **kw):
        headers = {"X-Hub-Request": "1", **kw.pop("headers", {})}
        return self._keep(self.client.delete(path, headers=headers, **kw))

    def sign_in(self):
        code = create_signin_code(self.state_dir)
        response = self.post("/api/session", json={"code": code})
        assert response.status_code == 204, response.text
        return response

    def wait_for(self, state: str, timeout: float = 5.0) -> dict:
        deadline = time.monotonic() + timeout
        while True:
            claim = self.get("/api/claim").json()
            if claim["state"] == state or time.monotonic() > deadline:
                assert claim["state"] == state, claim
                return claim
            time.sleep(0.02)


@pytest.fixture
def fake_daemon():
    return FakeDaemon()


@pytest.fixture
def make_hub(tmp_path, fake_daemon):
    clients = []

    def make(**overrides):
        settings = Settings(**{"daemon_url": "http://daemon.test", "state_dir": tmp_path / "state",
                               "poll_seconds": 0.02, **overrides})
        app = create_app(settings, transport=fake_daemon.transport())
        client = TestClient(app, base_url=f"http://{HUB_HOST}")
        client.__enter__()
        clients.append(client)
        return HubClient(client, settings.state_dir)

    yield make
    for client in clients:
        client.__exit__(None, None, None)


@pytest.fixture
def hub(make_hub):
    return make_hub()


def assert_no_secrets(bodies: list[str], *secrets_: str) -> None:
    for body in bodies:
        assert not TOKEN_SHAPE.search(body), body
        for secret in secrets_:
            assert secret not in body
