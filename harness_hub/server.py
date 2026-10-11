"""The Hub's HTTP server: a no-build ES-module UI plus the small JSON API it calls.

Browser-facing routes (`/api/...`) answer only a signed-in owner (`auth.py`) and only with allowlisted view models
(`views.py`). The Hub key never leaves this process except in the Authorization header of the Hub's own calls to its
daemon.
"""

from __future__ import annotations

import contextlib
import json
import logging
from importlib import resources
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.types import ASGIApp, Receive, Scope, Send

from . import __version__
from .auth import COOKIE, SESSION_TTL_SECONDS, Sessions
from .claim import CLAIMED, ClaimError, ClaimManager
from .config import Settings
from .daemon import DaemonClient, DaemonUnreachable
from .guard import ResponseGuard
from .state import SERVER_FILE, StateStore, write_private
from .views import claim_view, health_view, overview, ownership_view

log = logging.getLogger("harness_hub")

STATIC = resources.files("harness_hub") / "static"
SIGNIN_COMMAND = "python -m harness_hub signin-link"
REQUEST_HEADER = "x-hub-request"
CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; "
       "font-src 'self'; object-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
SECURITY_HEADERS = [
    (b"content-security-policy", CSP.encode()),
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"no-referrer"),
    (b"cross-origin-opener-policy", b"same-origin"),
    (b"permissions-policy", b"camera=(), microphone=(), geolocation=()"),
]


class HostCheck:
    """Refuse a Host header this Hub does not serve (DNS rebinding), and add the security headers."""

    def __init__(self, app: ASGIApp, allowed: frozenset[str]):
        self.app = app
        self.allowed = allowed
        self.fastapi: FastAPI | None = None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = dict(scope["headers"])
        host = headers.get(b"host", b"").decode("latin-1").lower()
        if host not in self.allowed:
            body = b'{"detail":"unknown host"}'
            await send({"type": "http.response.start", "status": 421, "headers": [
                (b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()),
                *SECURITY_HEADERS]})
            await send({"type": "http.response.body", "body": body})
            return

        async def with_headers(message):
            if message["type"] == "http.response.start":
                extra = list(SECURITY_HEADERS)
                if scope["path"].startswith("/api/"):
                    extra.append((b"cache-control", b"no-store"))
                message = {**message, "headers": [*message["headers"], *extra]}
            await send(message)

        await self.app(scope, receive, with_headers)


def _error(status: int, detail: str, **extra: Any) -> JSONResponse:
    return JSONResponse({"detail": detail, **extra}, status_code=status)


def create_app(settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None) -> HostCheck:
    store = StateStore(settings.state_dir)
    daemon = DaemonClient(settings.daemon_url, transport=transport)
    claims = ClaimManager(store, daemon, name=settings.claim_name, claim_file=settings.claim_file,
                          poll_seconds=settings.poll_seconds)
    sessions = Sessions(settings.state_dir)

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI):
        write_private(store.dir / SERVER_FILE, json.dumps({"url": settings.url, "version": __version__}) + "\n")
        if claims.pending():
            claims.ensure_polling()
        elif settings.claim_file is not None and claims.state() != CLAIMED:
            try:
                claim = await claims.start()
                log.info("Approve on the daemon host: %s", claims.approve_command(claim))
            except ClaimError as exc:
                log.warning("Hub claim not started: %s", exc)
        log.info("Hub %s for %s at %s", __version__, settings.daemon_url, settings.url)
        try:
            yield
        finally:
            await claims.stop()
            await daemon.aclose()

    app = FastAPI(title="Hub", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None,
                  openapi_url=None)
    app.state.claims = claims
    app.state.store = store
    app.state.sessions = sessions

    @app.middleware("http")
    async def require_owner(request: Request, call_next):
        path = request.url.path
        if not path.startswith("/api/"):
            return await call_next(request)
        if request.method not in ("GET", "HEAD"):
            # A custom header forces a CORS preflight, which this server never grants: no cross-site writes.
            if request.headers.get(REQUEST_HEADER) != "1":
                return _error(403, "missing X-Hub-Request header")
            origin = request.headers.get("origin")
            if origin is not None and origin.split("://", 1)[-1].lower() != request.headers.get("host", "").lower():
                return _error(403, "cross-origin request refused")
        if path == "/api/session":
            return await call_next(request)
        if not sessions.valid(request.cookies.get(COOKIE)):
            return _error(401, "sign in first", signin_command=SIGNIN_COMMAND)
        return await call_next(request)

    @app.get("/healthz", include_in_schema=False)
    async def healthz():
        return {"ok": True, "version": __version__}

    @app.get("/api/session")
    async def get_session(request: Request):
        return {"signed_in": sessions.valid(request.cookies.get(COOKIE)), "signin_command": SIGNIN_COMMAND,
                "version": __version__}

    @app.post("/api/session")
    async def sign_in(request: Request):
        if sessions.throttled():
            return _error(429, "too many sign-in attempts; wait a minute")
        try:
            body = await request.json()
        except ValueError:
            body = None
        code = body.get("code") if isinstance(body, dict) else None
        session = sessions.redeem_code(code) if isinstance(code, str) else None
        if session is None:
            return _error(401, "that sign-in link is not valid; links work once, for ten minutes",
                          signin_command=SIGNIN_COMMAND)
        response = Response(status_code=204)
        response.set_cookie(COOKIE, session, max_age=SESSION_TTL_SECONDS, httponly=True, samesite="strict",
                            secure=settings.secure_cookies, path="/")
        return response

    @app.delete("/api/session")
    async def sign_out(request: Request):
        sessions.end(request.cookies.get(COOKIE))
        response = Response(status_code=204)
        response.delete_cookie(COOKIE, path="/")
        return response

    @app.get("/api/overview")
    async def get_overview():
        health, ownership, error = None, None, ""
        try:
            reply = await daemon.health()
            if reply.ok:
                health = health_view(reply.body)
            else:
                error = f"the daemon's /health answered HTTP {reply.status}"
            key = claims.key()
            if key:
                discovery = await daemon.admin(key["token"])
                record = await daemon.admin(key["token"], "/hub-claim")
                if 401 in (discovery.status, record.status):
                    claims.released()
                elif discovery.ok and record.ok:
                    ownership = ownership_view(discovery.body, record.body, key["key_id"])
                else:
                    error = f"the owner API answered HTTP {discovery.status if not discovery.ok else record.status}"
        except DaemonUnreachable:
            error = f"the daemon at {settings.daemon_url} did not answer"
        return overview(claims, settings.daemon_url, health, ownership, error)

    @app.get("/api/claim")
    async def get_claim():
        return claim_view(claims)

    @app.post("/api/claim")
    async def start_claim():
        try:
            await claims.start()
        except ClaimError as exc:
            status = {"refused": 409, "rate_limited": 429, "claimed": 409}.get(exc.state, 502)
            return JSONResponse({"detail": str(exc), "claim": claim_view(claims)}, status_code=status)
        return JSONResponse(claim_view(claims), status_code=201)

    @app.delete("/api/claim")
    async def forget_claim():
        if claims.state() != CLAIMED:
            claims.forget()
        return claim_view(claims)

    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(str(STATIC / "index.html"), headers={"cache-control": "no-cache"})

    app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")

    served = HostCheck(ResponseGuard(app, store.secrets), settings.allowed_hosts())
    served.fastapi = app
    return served
