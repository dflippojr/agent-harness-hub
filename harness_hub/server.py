"""The Hub's HTTP server: a no-build ES-module UI plus the small JSON API it calls.

Browser-facing routes (`/api/...`) answer only a signed-in owner (`auth.py`) and only with allowlisted view models
(`views.py`). The Hub key never leaves this process except in the Authorization header of the Hub's own calls to its
daemon.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import re
import time
from importlib import resources
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.types import ASGIApp, Receive, Scope, Send

from . import __version__
from .administration import OPERATIONS, TARGET, validate_fields
from .auth import SESSION_TTL_SECONDS, Sessions
from .claim import CLAIMED, ClaimError, ClaimManager
from .config import Settings
from .daemon import ADMIN_ROUTES, DaemonClient, DaemonUnreachable
from .guard import TOKEN_SHAPE, ResponseGuard, leaks
from .inventory import InventoryCache, valid_inventory
from .state import SERVER_FILE, StateStore, write_private
from .tailnet import PeerCheck, TailnetIdentity, login
from .views import (
    REDACTED,
    administration_view,
    app_view,
    apps_view,
    claim_view,
    control_view,
    entries_view,
    entry_digest,
    entry_text,
    entry_view,
    erasures_view,
    health_view,
    limits_view,
    marketplace_slots,
    memory_route_absent,
    module_view,
    normalize_origin,
    overview,
    ownership_view,
    pairing_attention,
    pairing_request_view,
    pairing_view,
    registry_view,
    scope_disclosures,
    settings_error,
    spend_view,
    valid_limits,
)

log = logging.getLogger("harness_hub")

STATIC = resources.files("harness_hub") / "static"
SIGNIN_COMMAND = "python -m harness_hub signin-link"
REQUEST_HEADER = "x-hub-request"
SESSION_HEADER = "x-hub-session"
MAX_SIGNIN_BODY = 1024
ALLOWED_LOGINS_SECONDS = 5.0  # how long a tailnet sign-in reuses the daemon's allowed_logins answer
PAIRING_COUNT_SECONDS = 5.0  # how long Overview waits for its optional pairing count, consent lock included
SPEND_CAP_READS = 8  # App session-cap reads in flight at once; every live App is read
RESTART_GRACE_SECONDS = 1.0  # how long after a restart request the Hub ignores health that predates it
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


# An error says whether the daemon answered, so the shell's status tells "the Hub could not reach the daemon" (a 503 of
# the Hub's own, reachable false) from "the daemon answered with an error" (forwarded, any status, reachable true).
def _unreachable(detail: str, **extra: Any) -> JSONResponse:
    return _error(503, detail, reachable=False, **extra)


def _forwarded(reply: Any) -> JSONResponse:
    return JSONResponse({**settings_error(reply.body, reply.status), "reachable": True}, status_code=reply.status)


async def concurrently(*reads: Any) -> list[Any]:
    """Await independent daemon reads together, so a slow daemon costs one round trip instead of one per read.

    Each item is the read's reply or the exception it raised, and every read has finished when this returns. A caller
    passes each item through `settled` in the order the reads used to run, so the first failure it raises is the
    same one a sequential route raised."""
    return await asyncio.gather(*reads, return_exceptions=True)


def settled(result: Any) -> Any:
    """One item from `concurrently`: the reply, or the exception the read raised, re-raised here."""
    if isinstance(result, BaseException):
        raise result
    return result


def create_app(settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None,
               peer_check: PeerCheck | None = None) -> HostCheck:
    store = StateStore(settings.state_dir)
    daemon = DaemonClient(settings.daemon_url, transport=transport)
    claims = ClaimManager(store, daemon, name=settings.claim_name, claim_file=settings.claim_file,
                          poll_seconds=settings.poll_seconds)
    sessions = Sessions(settings.state_dir)
    inventory = InventoryCache(store.dir, settings.daemon_url)
    peer_check = peer_check or PeerCheck()

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

    app = FastAPI(title="Hub", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None,
                  openapi_url=None)
    app.state.claims = claims
    app.state.store = store
    app.state.sessions = sessions
    app.state.inventory = inventory
    restart = {"active": False, "target": None, "not_before": 0.0}

    def observe_restart(shown: dict | None) -> None:
        """End a restart the Hub started once the daemon answers healthy with confirmed settings, at the target revision
        or after a recovery. Overview and the Settings health poll both look, so neither page has to stay open."""
        if time.monotonic() >= restart["not_before"] and shown and shown["ok"] and shown["config_confirmed"] and (
                restart["target"] is None or shown["config_revision"] == restart["target"] or shown["recovery"]):
            restart["active"] = False

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
            expected_origin = f"{'https' if settings.secure_cookies else 'http'}://{request.headers.get('host', '')}"
            if origin is not None and (origin.lower() not in settings.allowed_origins()
                                       or origin.lower() != expected_origin.lower()):
                return _error(403, "cross-origin request refused")
        if path == "/api/session" and request.method in ("GET", "POST"):
            return await call_next(request)
        if not sessions.valid(request.cookies.get(settings.cookie_name), request.headers.get(SESSION_HEADER)):
            return _error(401, "sign in first", signin_command=SIGNIN_COMMAND)
        return await call_next(request)

    @app.get("/healthz", include_in_schema=False)
    async def healthz():
        return {"ok": True, "version": __version__}

    @app.get("/api/session")
    async def get_session(request: Request):
        # `tailnet_login` is the caller's own identity, present only when tailscaled added it (tailnet.py). Whether
        # it is the owner's is decided at sign-in, against the daemon's list. A token-shaped login is redacted here, so
        # the response guard never withholds this route: the page reads it first on every load.
        caller = TOKEN_SHAPE.sub(REDACTED, login(request.headers))
        return {"signed_in": sessions.valid(request.cookies.get(settings.cookie_name),
                                            request.headers.get(SESSION_HEADER)), "signin_command": SIGNIN_COMMAND,
                "tailnet_login": caller or None, "version": __version__}

    owner_logins: dict[str, Any] = {}  # the daemon's allowed_logins, kept briefly: {"until": ..., "logins": [...]}

    async def allowed_logins(token: str) -> list[str] | JSONResponse:
        if owner_logins and time.monotonic() < owner_logins["until"]:
            return owner_logins["logins"]
        try:
            reply = await daemon.admin(token)
        except DaemonUnreachable:
            return _unreachable(f"the daemon at {settings.daemon_url} did not answer; use a sign-in link",
                                signin_command=SIGNIN_COMMAND)
        if not reply.ok:
            return _error(502, f"the owner API answered HTTP {reply.status}; use a sign-in link",
                          signin_command=SIGNIN_COMMAND)
        allowed = reply.body.get("allowed_logins") if isinstance(reply.body, dict) else None
        if not isinstance(allowed, list) or not all(isinstance(item, str) for item in allowed):
            return _error(502, "the daemon does not list the owner's tailnet logins (it needs Owner API 1.26 or "
                          "later); use a sign-in link", signin_command=SIGNIN_COMMAND)
        owner_logins.update(until=time.monotonic() + ALLOWED_LOGINS_SECONDS, logins=list(allowed))
        return owner_logins["logins"]

    async def tailnet_sign_in(request: Request) -> JSONResponse | None:
        """Refuse a tailnet sign-in unless the daemon lists this request's trusted login; None means accept. An
        unclaimed Hub, an open-owner daemon and an unlisted login get the same answer; the log tells them apart."""
        caller = login(request.headers)
        if not caller:
            return _error(401, "this request carries no tailnet identity from tailscaled; use a sign-in link",
                          signin_command=SIGNIN_COMMAND)
        if not sessions.tailnet_attempt(caller):
            return _error(429, "too many tailnet sign-in attempts; wait a minute")
        refused = _error(403, "this tailnet login cannot sign in to this Hub; use a sign-in link",
                         signin_command=SIGNIN_COMMAND)
        # A raw TCP forward lets tailscaled relay a client's own identity headers (tailnet.py). Only where the peer
        # was verified: with unverified headers trusted, any local process can sign in anyway.
        server = request.scope.get("server")
        if peer_check.platform_ok() and await asyncio.to_thread(peer_check.raw_forward_to,
                                                                server[1] if server else None):
            log.warning("refused tailnet sign-in: this node's tailscale serve config has a raw TCP forward to the "
                        "Hub's port, or could not be read")
            return refused
        key = claims.key()
        if not key:
            log.warning("refused tailnet sign-in: this Hub has not claimed its daemon")
            return refused
        allowed = await allowed_logins(key["token"])
        if isinstance(allowed, JSONResponse):
            return allowed
        if not allowed:
            # Open-owner legacy mode: the daemon names no owner, so no tailnet login proves ownership.
            log.warning("refused tailnet sign-in: the daemon has no owner allowlist (allowed_logins)")
            return refused
        if caller not in allowed:
            log.warning("refused tailnet sign-in for a login the daemon does not list as the owner's")
            return refused
        return None

    @app.post("/api/session")
    async def sign_in(request: Request):
        length = request.headers.get("content-length")
        if length is not None:
            try:
                if int(length) < 0:
                    return _error(400, "invalid Content-Length")
                if int(length) > MAX_SIGNIN_BODY:
                    return _error(413, "sign-in body is too large")
            except ValueError:
                return _error(400, "invalid Content-Length")
        data = bytearray()
        async for chunk in request.stream():
            if len(data) + len(chunk) > MAX_SIGNIN_BODY:
                return _error(413, "sign-in body is too large")
            data.extend(chunk)
        try:
            body = json.loads(data)
        except ValueError:
            body = None
        if isinstance(body, dict) and body.get("method") == "tailnet":
            refusal = await tailnet_sign_in(request)
            if refusal is not None:
                return refusal
            credentials = sessions.create(request.cookies.get(settings.cookie_name))
        else:
            code = body.get("code") if isinstance(body, dict) else None
            was_throttled = sessions.throttled()
            credentials = sessions.redeem_code(code, request.cookies.get(settings.cookie_name))
            if credentials is None:
                if was_throttled:
                    return _error(429, "too many sign-in attempts; wait a minute")
                return _error(401, "that sign-in link is not valid; links work once, for ten minutes",
                              signin_command=SIGNIN_COMMAND)
        session, binding = credentials
        response = Response(status_code=204, headers={SESSION_HEADER: binding})
        if session == request.cookies.get(settings.cookie_name):
            return response  # another tab of the signed-in session: the cookie and its expiry stay as they were
        response.set_cookie(settings.cookie_name, session, max_age=SESSION_TTL_SECONDS,
                            httponly=True, samesite="strict",
                            secure=settings.secure_cookies, path="/")
        return response

    @app.delete("/api/session")
    async def sign_out(request: Request):
        sessions.end(request.cookies.get(settings.cookie_name))
        response = Response(status_code=204)
        response.delete_cookie(settings.cookie_name, path="/", secure=settings.secure_cookies,
                               httponly=True, samesite="strict")
        return response

    # Listings must not publish a new slot while its disclosures are being checked or it is being withdrawn.
    pairing_consent_lock = asyncio.Lock()

    # A bounded, process-local backstop for an observed browser claim that skipped the confirmation gate.
    # Share observations across pages/tabs; record successful Hub confirmations under the same lock as polls.
    browser_slot_states: dict[tuple[str, str], tuple[str, bool]] = {}

    def observe_browser_slots(rows: list, key_id: str, *, confirmed_id: str = "") -> bool:
        """Whether an observed browser claim looks like it skipped confirmation; `skipped_confirmation` rules on it."""
        warning = False
        for raw in rows:
            row = pairing_request_view(raw)
            if not (row["id"] and row["kind"] == "app" and row["armed"] and row["browser"]):
                continue
            identity = (key_id, row["id"])
            previous, bypassed = browser_slot_states.get(identity, ("", False))
            bypassed = bypassed or (previous == "armed" and row["state"] in ("approved", "redeemed")
                                    and row["id"] != confirmed_id)
            if identity not in browser_slot_states and len(browser_slot_states) >= 256:
                browser_slot_states.pop(next(iter(browser_slot_states)))
            browser_slot_states[identity] = (row["state"], bypassed)
            warning = warning or bypassed
        return warning

    async def skipped_confirmation(key: dict, suspected: bool) -> str:
        """The warning for a suspected bypass, only from a daemon that does not enforce confirmation.

        Owner API 1.24 and later enforce typed-code confirmation, so from them an `armed` slot that turned `approved`
        between listings was confirmed outside the Hub (the host CLI, another Hub process, a Hub restart): no warning,
        and the stale suspicion is dropped. A daemon whose version is old or cannot be read keeps it."""
        if not suspected:
            return ""
        try:
            reply = await daemon.admin(key["token"])
        except DaemonUnreachable:
            reply = None
        if reply is not None and reply.ok and ownership_view(
                reply.body, {}, "")["compatibility"] in ("supported", "no_tailnet_signin"):
            for identity, (state, _) in list(browser_slot_states.items()):
                if identity[0] == key["key_id"]:
                    browser_slot_states[identity] = (state, False)
            return ""
        return "This daemon approved the claim without confirmation; update the daemon."

    async def retained(token: str) -> str:
        """The daemon answered 401: drop the key only if a second read confirms it, else say it was kept.

        A transient rejection must not destroy the owner's persistent key."""
        confirmation = await daemon.admin(token)
        if confirmation.status == 401:
            await claims.released(token)
            return ""
        return "the owner API rejected the key once; retained it pending confirmation"

    @app.get("/api/overview")
    async def get_overview():
        health, ownership, attention, error, answered = None, None, None, "", False
        key = claims.key()

        async def read_pairing() -> Any:
            # Under the consent lock, as the other listings, so no count is published during a withdrawal.
            async with pairing_consent_lock:
                reply = await daemon.pairing(key["token"], "list")
                if reply.ok and isinstance(reply.body, list):
                    observe_browser_slots(reply.body, key["key_id"])
                return reply

        # The pairing read can wait on the consent lock for as long as an arm request holds it, so it runs beside the
        # other reads and is awaited only when all of them succeed, and then only until its deadline; a refused or
        # failed read, or the deadline, cancels it instead.
        pairing_task = asyncio.ensure_future(read_pairing()) if key else None
        deadline = asyncio.get_running_loop().time() + PAIRING_COUNT_SECONDS
        try:
            reads = await concurrently(daemon.health(), *(
                (daemon.admin(key["token"]), daemon.admin(key["token"], "/hub-claim")) if key else ()))
            reply = settled(reads[0])
            answered = True
            if reply.ok:
                health = health_view(reply.body)
            else:
                error = f"the daemon's /health answered HTTP {reply.status}"
            if key:
                discovery, record = settled(reads[1]), settled(reads[2])
                if 401 in (discovery.status, record.status):
                    error = await retained(key["token"]) or error
                elif not (discovery.ok and record.ok):
                    error = f"the owner API answered HTTP {discovery.status if not discovery.ok else record.status}"
                else:
                    ownership = ownership_view(discovery.body, record.body, key["key_id"])
                    if health is not None:
                        remaining = deadline - asyncio.get_running_loop().time()
                        await asyncio.wait({pairing_task}, timeout=max(0.0, remaining))
                        pairing = None
                        # A missing count never changes what Overview reports about the daemon.
                        with contextlib.suppress(DaemonUnreachable):
                            pairing = pairing_task.result() if pairing_task.done() else None
                        if pairing is not None and pairing.status == 401:
                            # Outside the suppress above: an unanswered confirmation is the daemon not answering.
                            refusal = await retained(key["token"])
                            error, ownership = (refusal, ownership) if refusal else (error, None)
                        elif pairing is not None and pairing.ok:
                            attention = pairing_attention(pairing.body, time.time())
        except DaemonUnreachable:
            # As after a discovery refusal, ownership the daemon refused once is not shown when it cannot confirm it.
            ownership = None
            error = f"the daemon at {settings.daemon_url} did not answer"
        finally:
            if pairing_task:
                pairing_task.cancel()
                await concurrently(pairing_task)
        result = overview(claims, settings.daemon_url, health, ownership, error)
        observe_restart(health["health"] if health else None)
        result["answered"] = answered
        result["restarting"] = restart["active"]
        result["attention"] = attention
        return result

    async def refused(token: str) -> JSONResponse:
        """The daemon answered 401: drop the key only if a second read confirms it, then point to Overview."""
        await retained(token)
        return _error(409, "The daemon refused this Hub's key. Check Overview before trying again.")

    def cacheable(body: Any, key_id: str) -> bool:
        """Whether no page that reads the shared cache would show a credential-like string from this snapshot."""
        secrets = tuple(store.secrets())
        views = (module_view(body), apps_view(body, key_id, secrets), entries_view(body, secrets))
        return not leaks(json.dumps(views).encode(), store.secrets())

    async def read_inventory(key: dict, shown) -> tuple[dict | None, str, str, bool] | JSONResponse:
        """The last good inventory (refreshed when the daemon answers with a valid one), its state, any error, and
        whether the daemon answered at all (the shell's status reads that instead of the error text).

        `shown` projects an inventory into the asking page's browser view; a snapshot whose view would leak is not
        shown. Modules, Apps and Marketplace share the cache, so a snapshot that would leak on any of them is never
        cached either: the page whose own view is clean shows it once, and the earlier last-good stays."""
        good = inventory.get(key["key_id"])
        state, error, reachable = "ready", "", True
        try:
            reply = await daemon.inventory(key["token"])
            if reply.status == 401:
                return await refused(key["token"])
            if reply.ok:
                if not valid_inventory(reply.body):
                    state, error = "error", "The daemon returned an invalid inventory."
                elif leaks(json.dumps(shown(reply.body)).encode(), store.secrets()):
                    state, error = "error", "Inventory withheld: it contained a credential-like string."
                elif cacheable(reply.body, key["key_id"]):
                    good = inventory.save(reply.body, key["key_id"])
                else:
                    good = {"inventory": reply.body, "fetched_at": time.time()}
            elif reply.status == 404:
                state, error = "absent", "The Hub inventory module is not installed."
            elif reply.status == 400 and "disabled" in reply.detail.lower():
                state, error = "disabled", "Hub inventory is disabled. Switch it on below."
            else:
                # A stale or unreadable entries file is a named 400 (`hub.entries: invalid JSON`, `entries[0]...`).
                state, error = "error", settings_error(reply.body, reply.status)["detail"]
        except DaemonUnreachable:
            state, error, reachable = "error", "The daemon did not answer.", False
        except (OSError, ValueError):
            state, error = "error", "The inventory could not be saved in the private state directory."
        return good, state, error, reachable

    @app.get("/api/apps")
    @app.get("/api/modules")
    async def get_modules(request: Request):
        key = claims.key()
        if key is None:
            return _error(409, "Claim this Hub on Overview before reading inventory.")
        apps_page = request.url.path == "/api/apps"
        result = await read_inventory(key, (lambda body: apps_view(body, key["key_id"], tuple(store.secrets())))
                                      if apps_page else module_view)
        if isinstance(result, JSONResponse):
            return result
        good, state, error, reachable = result
        if apps_page:
            shown = apps_view(good["inventory"], key["key_id"], tuple(store.secrets())) if good else {
                "apps": None, "entries": []}
        else:
            shown = {"modules": module_view(good["inventory"]) if good else None}
        return {"state": state, "error": error, "stale": state != "ready" and good is not None,
                "reachable": reachable, "fetched_at": good["fetched_at"] if good else None, **shown}

    async def refused_spend(token: str) -> JSONResponse:
        """A 401 during a Spend read; the refusal's own confirmation read may also go unanswered."""
        try:
            return await refused(token)
        except DaemonUnreachable:
            return _error(503, "The daemon did not answer. Spend is unavailable until it does.")

    @app.get("/api/spend")
    async def get_spend():
        """Read-only: each App's usage and session-cap use, and each backend's windows. It writes nothing, not even
        the inventory cache, so its poll adds no disk writes; a failed read falls back to the cached inventory."""
        key = claims.key()
        if key is None:
            return _error(409, "Claim this Hub on Overview before reading spend.")
        secrets = tuple(store.secrets())
        good = inventory.get(key["key_id"])
        body, error, fetched_at = (good["inventory"], "", good["fetched_at"]) if good else (None, "", None)
        # Each read stands alone: one that fails or times out never hides what another one returned.
        reply, backends = await concurrently(daemon.inventory(key["token"]), daemon.settings(key["token"], "backends"))
        for result in (reply, backends):
            if isinstance(result, BaseException) and not isinstance(result, DaemonUnreachable):
                raise result
        if 401 in (getattr(reply, "status", None), getattr(backends, "status", None)):
            return await refused_spend(key["token"])
        if isinstance(reply, DaemonUnreachable):
            error = "The daemon did not answer."
        elif reply.ok and valid_inventory(reply.body):
            body, fetched_at = reply.body, time.time()
        else:
            error = "The daemon returned an invalid inventory." if reply.ok else settings_error(
                reply.body, reply.status)["detail"]
        live = [row["id"] for row in (body or {}).get("apps", []) if isinstance(row, dict)
                and row.get("kind") == "app" and row.get("revoked_at") is None and row.get("id") != key["key_id"]
                and isinstance(row.get("id"), str) and re.fullmatch(r"[A-Za-z0-9_-]{1,80}", row["id"])]
        gate = asyncio.Semaphore(SPEND_CAP_READS)

        async def read_caps(app_id: str):
            async with gate:
                return await daemon.apps(key["token"], "limits", app_id)

        replies = await concurrently(*(read_caps(app_id) for app_id in live))
        for result in replies:
            if isinstance(result, BaseException) and not isinstance(result, DaemonUnreachable):
                raise result
        if any(getattr(result, "status", None) == 401 for result in replies):
            return await refused_spend(key["token"])
        limits = {app_id: result.body for app_id, result in zip(live, replies, strict=True)
                  if getattr(result, "ok", False)}
        backend_rows = backends.body if getattr(backends, "ok", False) else None
        shown = spend_view(body, backend_rows, limits, key["key_id"], secrets)
        if body is None:
            shown["apps"] = None
        if leaks(json.dumps(shown).encode(), store.secrets()):
            return _error(502, "Spend withheld: it contained a credential-like string.")
        return {"error": error, "stale": bool(error) and body is not None, "fetched_at": fetched_at,
                # Any read the daemon answered shows it is up; the shell's status reads this, not the error text.
                "reachable": not all(isinstance(result, DaemonUnreachable) for result in (reply, backends)),
                "backends_error": "" if backend_rows is not None else "The daemon's backend usage was unavailable.",
                **shown}

    # Bound to the Hub key and the entry's displayed identity, with a bounded ten-minute lifetime.
    consent_copies: dict[tuple[str, str], tuple[str, float, list[dict], bool]] = {}

    def consent_identity(entry: dict) -> str:
        return entry_digest({k: v for k, v in entry.items() if k not in {
            "state", "paired", "pending_pairing", "unverified_origin", "consent_digest", "slots",
            "acknowledgement_required"}})

    def consent_entry(entry: dict, key_id: str) -> dict:
        copied = consent_copies.get((key_id, entry["catalog_app_id"]))
        if copied and copied[0] == consent_identity(entry) and copied[1] > time.monotonic():
            entry["scopes"] = [{**item, **disclosure} for item, disclosure in zip(
                entry["scopes"], copied[2], strict=True)]
            entry["elevated"] = sorted(set(entry["elevated"]) | {
                item["scope"] for item in copied[2] if item["tier"] == "elevated"})
            if copied[3]:
                entry["acknowledgement_required"] = True
        entry["consent_digest"] = entry_digest(entry)
        return entry

    def remember_consent(entry: dict, key_id: str, disclosures: list[dict], *, acknowledge: bool = False):
        if len(consent_copies) >= 256:
            consent_copies.pop(next(iter(consent_copies)))
        consent_copies[(key_id, entry["catalog_app_id"])] = (
            consent_identity(entry), time.monotonic() + 600, disclosures, acknowledge)

    @app.get("/api/marketplace")
    async def get_marketplace():
        """Local entries (#520) with their armed slots. Listing grants nothing; nothing from an entry is fetched."""
        key = claims.key()
        if key is None:
            return _error(409, "Claim this Hub on Overview before reading the Marketplace.")
        secrets = tuple(store.secrets())

        async def read_slots() -> tuple[Any, bool]:
            # Only the pairing read and what it observes hold the consent lock; the inventory read never waits on it.
            async with pairing_consent_lock:
                reply = await daemon.pairing(key["token"], "list")
                if reply.ok and isinstance(reply.body, list):
                    return reply, observe_browser_slots(reply.body, key["key_id"])
                return reply, False

        # Both reads run together, but a refusal from the inventory read returns at once: the slots read can wait on
        # the consent lock for as long as another tab's arm request holds it, so it is cancelled, not awaited.
        slots_task = asyncio.ensure_future(read_slots())
        try:
            result = await read_inventory(key, lambda body: entries_view(body, secrets))
            if isinstance(result, JSONResponse):
                return result
            [slots_read] = await concurrently(slots_task)
        finally:
            if not slots_task.done():
                slots_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await slots_task
        good, state, error, reachable = result
        # Only the daemon's validated answer, or the last one: never entries the daemon did not validate.
        entries, withheld = entries_view(good["inventory"], secrets) if good else (None, 0)
        slots, slots_error, confirmation_warning = {}, "", ""
        try:
            reply, suspected = settled(slots_read)
            if reply.status == 401:
                return await refused(key["token"])
            if reply.ok and isinstance(reply.body, list):
                slots = marketplace_slots(reply.body, secrets)
                confirmation_warning = await skipped_confirmation(key, suspected)
            else:
                slots_error = f"Armed slots could not be read (HTTP {reply.status})."
        except DaemonUnreachable:
            slots_error = "Armed slots could not be read: the daemon did not answer."
        for entry in entries or []:
            consent_entry(entry, key["key_id"])
            entry["slots"] = slots.get(entry["catalog_app_id"], [])
        return {"state": state, "error": error, "stale": state != "ready" and good is not None,
                "reachable": reachable, "fetched_at": good["fetched_at"] if good else None, "entries": entries,
                "withheld": withheld,
                "slots_error": slots_error, "confirmation_warning": confirmation_warning,
                "daemon_url": settings.daemon_url, "now": time.time()}

    @app.post("/api/marketplace/arm")
    async def arm_slot(request: Request):
        async with pairing_consent_lock:
            return await checked_arm_slot(request)

    async def checked_arm_slot(request: Request):
        """Arm a pre-approved slot for one entry once the owner approved its consent screen (#519, flow 4).

        The scopes and origin must be exactly what the entry declares as the daemon validates it now, so a stale page
        or a crafted body can neither widen what the owner saw nor arm a slot for an id that is not listed."""
        key = claims.key()
        if key is None:
            return _error(409, "Claim this Hub on Overview before setting up an app.")
        if not request.headers.get("origin"):
            return _error(403, "Setting up an app requires a same-origin Origin")
        data = bytearray()
        async for chunk in request.stream():
            if len(data) + len(chunk) > 4096:
                return _error(413, "Set-up body is too large")
            data.extend(chunk)
        try:
            body = json.loads(data or b"{}")
        except ValueError:
            return _error(400, "Set-up body must be JSON")
        if not isinstance(body, dict) or set(body) - {"catalog_app_id", "scopes", "origin", "acknowledge_elevated",
                                                               "consent_digest"}:
            return _error(400, "Unexpected set-up fields")
        catalog_app_id, scopes = body.get("catalog_app_id"), body.get("scopes")
        origin, acknowledged = body.get("origin", ""), body.get("acknowledge_elevated", False)
        if not isinstance(catalog_app_id, str) or not isinstance(scopes, list) or not all(
                isinstance(scope, str) for scope in scopes) or not isinstance(origin, str) or (
                type(acknowledged) is not bool):
            return _error(400, "catalog_app_id, scopes, origin or acknowledge_elevated has the wrong type")
        try:
            reply = await daemon.inventory(key["token"])
            if reply.status == 401:
                return await refused(key["token"])
            if not reply.ok:
                detail = settings_error(reply.body, reply.status)["detail"]
                return _error(409, f"The entries could not be read ({detail}). Fix them before setting up an app.")
            if not valid_inventory(reply.body):
                return _error(502, "The daemon returned an invalid inventory.")
            raw_entry = next((row for row in reply.body.get("entries", [])
                              if isinstance(row, dict) and row.get("catalog_app_id") == catalog_app_id), None)
            entry = entry_view(raw_entry)
            if entry is None:
                return _error(404, "No local entry has that catalog app id. Refresh the Marketplace.")
            baseline = {**entry, "scopes": [dict(item) for item in entry["scopes"]]}
            consent_entry(entry, key["key_id"])
            raw_app = raw_entry["manifest"]["app"]
            declared = [item["scope"] for item in raw_app["scopes"]]
            if sorted(scopes) != sorted(declared):
                return _error(409, "The entry's scopes changed since the consent screen was shown. Review it again.")
            declared_origins = raw_app.get("browser_origins") or []
            try:
                bound_origin = normalize_origin(origin) if origin else ""
                allowed = {normalize_origin(value) for value in declared_origins}
            except ValueError:
                return _error(400, "Choose one of the entry's browser origins.")
            if allowed and bound_origin not in allowed:
                return _error(400, "Choose one of the entry's browser origins.")
            if not allowed and origin:
                return _error(400, "This entry declares no browser origin: it is set up as a native app.")
            if body.get("consent_digest") != entry_digest(entry):
                return _error(409, "The entry changed since the consent screen was shown. Review it again.")
            if (entry["elevated"] or entry.get("acknowledgement_required") or any(
                    not item["text"] for item in entry["scopes"])) and not acknowledged:
                return _error(400, "Elevated scopes or scopes without a Hub description need the separate "
                              "acknowledgement before setup.",
                              error={"code": "acknowledgement_required"})
            arm = {"catalog_app_id": catalog_app_id, "scopes": declared,
                   "name": entry_text(raw_app["display_name"], 80).strip()[:60]}
            if bound_origin:
                arm["origin"] = bound_origin
            if acknowledged:
                arm["acknowledge_elevated"] = True
            reply = await daemon.pairing(key["token"], "arm", body=arm)
            if reply.status == 401:
                return await refused(key["token"])
        except DaemonUnreachable:
            return _unreachable("The daemon did not answer. Refresh the Marketplace before setting up again.")
        if not reply.ok:
            error = settings_error(reply.body, reply.status)
            if error["error"]["code"] == "acknowledgement_required":
                # A newer daemon may elevate a scope our pinned table does not yet know. It armed nothing.
                shown = [{k: item[k] for k in ("scope", "tier", "text")} for item in entry["scopes"]]
                remember_consent(baseline, key["key_id"], shown, acknowledge=True)
                return _error(409, "The daemon requires a separate acknowledgement for these scopes. "
                              "Review the consent screen again.", consent_entry=consent_entry(baseline, key["key_id"]))
            return JSONResponse(error, status_code=reply.status)
        actual = scope_disclosures(reply.body.get("disclosures") if isinstance(reply.body, dict) else None,
                                   declared, tuple(store.secrets()))
        shown = [{k: item[k] for k in ("scope", "tier", "text")} for item in entry["scopes"]]
        if actual != shown or any(not item["text"].strip() for item in shown):
            # The id stays inside this server until it has been withdrawn: no hand-off on drift, including add-ons.
            unsafe = pairing_request_view(reply.body)
            if not unsafe["id"]:
                return _error(502, "The daemon returned an invalid armed slot. Review Pairing requests on the host.")
            try:
                denied = await daemon.pairing(key["token"], "deny", rid=unsafe["id"])
            except DaemonUnreachable:
                return _unreachable("Disclosure review failed and the slot could not be withdrawn. "
                                    "Withdraw it on Pairing requests before trying again.")
            if not denied.ok or pairing_request_view(denied.body)["state"] != "denied":
                return _error(502, "Disclosure review failed and the daemon refused to withdraw the slot. "
                              "Withdraw it on Pairing requests before trying again.")
            if actual is not None:
                remember_consent(baseline, key["key_id"], actual)
                reviewed = consent_entry(baseline, key["key_id"])
                return _error(409, "The daemon describes these scopes differently from what you saw. "
                              "Review the consent screen again.", consent_entry=reviewed)
            return _error(409, "The daemon's scope disclosures could not be displayed safely. "
                          "The slot was withdrawn. Review Pairing requests on the host.")
        slot = pairing_request_view(reply.body, tuple(store.secrets()))
        if not slot["id"] or slot["state"] != "armed":
            return _error(502, "The daemon returned an invalid armed slot. Refresh the Marketplace.")
        observe_browser_slots([slot], key["key_id"])
        return JSONResponse({"request": slot, "daemon_url": settings.daemon_url, "name": entry["name"]},
                            status_code=201)

    @app.api_route("/api/apps/{resource:path}", methods=["GET", "PUT", "DELETE", "POST"])
    async def apps_action(resource: str, request: Request):
        operation, app_id = None, ""
        if resource == "erasures" and request.method == "GET":
            operation = "erasures"
        else:
            match = re.fullmatch(r"([A-Za-z0-9_-]{1,80})/(revoke|limits|retention)", resource)
            if match:
                app_id, action = match.groups()
                operation = {("DELETE", "revoke"): "revoke", ("GET", "limits"): "limits",
                             ("PUT", "limits"): "limits_save", ("PUT", "retention"): "retention"}.get(
                                 (request.method, action))
        if operation is None:
            return _error(404, "Unknown Apps operation. Restore is available only on the daemon host CLI.")
        key = claims.key()
        if key is None:
            return _error(409, "Claim this Hub on Overview before administering Apps.")
        body = None
        if request.method != "GET":
            if not request.headers.get("origin"):
                return _error(403, "Apps writes require a same-origin Origin")
            data = bytearray()
            async for chunk in request.stream():
                if len(data) + len(chunk) > 1024:
                    return _error(413, "Apps body is too large")
                data.extend(chunk)
            try:
                body = json.loads(data or b"{}")
            except ValueError:
                return _error(400, "Apps body must be JSON")
            fields = {"revoke": {"confirm"}, "limits_save": {"max_running", "max_queued"},
                      "retention": {"retention_days", "confirm"}}
            if not isinstance(body, dict) or not body or set(body) - fields[operation]:
                return _error(400, "Unexpected Apps fields")
            if operation in ("revoke", "retention") and body.pop("confirm", None) is not True:
                return _error(400, "Confirm this action in the page first")
            if operation == "limits_save" and any(value is not None and (
                    type(value) is not int or not 1 <= value <= (100 if name == "max_running" else 1000))
                    for name, value in body.items()):
                return _error(400, "Session caps must be integers in range, or null for the daemon default")
            if operation == "retention":
                days = body.get("retention_days")
                if "retention_days" not in body or days is not None and (
                        type(days) not in (int, float) or not math.isfinite(days) or not 0 < days <= 36500):
                    return _error(400, "Retention must be 0 < days <= 36500, or null to keep until deleted")
            if operation == "revoke" and app_id == key["key_id"]:
                return _error(409, "This Hub is released only on the daemon host: harness hub release --confirm",
                              release_command="harness hub release --confirm")
        try:
            if operation == "limits":
                # /hub does not carry retention, so the detail also needs the token-free key list: read both at once.
                reply, keys = await concurrently(daemon.apps(key["token"], "limits", app_id),
                                                 daemon.apps(key["token"], "keys"))
                reply = settled(reply)
            else:
                reply, keys = await daemon.apps(key["token"], operation, app_id, body), None
            if reply.status == 401:
                return await refused(key["token"])
            if not reply.ok:
                return _forwarded(reply)
            if operation == "revoke":
                return Response(status_code=204)
            if operation == "erasures":
                if not isinstance(reply.body, list):
                    return _error(502, "The daemon returned invalid erasure data.")
                return erasures_view(reply.body, tuple(store.secrets()))
            if operation in ("limits", "limits_save"):
                if not valid_limits(reply.body):
                    return _error(502, "The daemon returned invalid session limits.")
                shown = limits_view(reply.body)
                if operation == "limits_save":
                    return shown
                keys = settled(keys)
                if keys.status == 401:
                    return await refused(key["token"])
                if not keys.ok:
                    return _error(keys.status, f"App detail could not be read (HTTP {keys.status}).", reachable=True)
                if not isinstance(keys.body, list):
                    return _error(502, "The daemon returned invalid App detail.")
                row = next((row for row in keys.body if isinstance(row, dict) and row.get("id") == app_id), None)
                if row is None:
                    return _error(404, "The App no longer appears in the key list. Refresh Apps.")
                return {**shown, "retention_days": app_view(row)["retention_days"]}
            return {"id": app_view(reply.body)["id"], "retention_days": app_view(reply.body)["retention_days"]}
        except DaemonUnreachable:
            return _unreachable("The daemon did not answer. Refresh the state before repeating a write.")

    @app.api_route("/api/pairing", methods=["GET"])
    @app.api_route("/api/pairing/{rid}/{action}", methods=["POST"])
    async def pairing_action(request: Request, rid: str = "", action: str = ""):
        """App pairing requests: list, approve, confirm or deny. Tokens go from the daemon straight to the App."""
        operation = action or "list"
        if operation not in ("list", "approve", "confirm", "deny") or rid and not re.fullmatch(
                r"[A-Za-z0-9_-]{1,80}", rid):
            return _error(404, "Unknown pairing operation")
        key = claims.key()
        if key is None:
            return _error(409, "Claim this Hub on Overview before deciding pairing requests.")
        body = None
        if request.method == "POST":
            if not request.headers.get("origin"):
                return _error(403, "Pairing decisions require a same-origin Origin")
            data = bytearray()
            async for chunk in request.stream():
                if len(data) + len(chunk) > 1024:
                    return _error(413, "Pairing body is too large")
                data.extend(chunk)
            try:
                body = json.loads(data or b"{}")
            except ValueError:
                return _error(400, "Pairing body must be JSON")
            fields = {"approve": {"match", "acknowledge_elevated"}, "confirm": {"match"}, "deny": {"confirm"}}
            if not isinstance(body, dict) or set(body) - fields[operation]:
                return _error(400, "Unexpected pairing fields")
            if operation == "deny":
                if body.pop("confirm", None) is not True:
                    return _error(400, "Confirm this action in the page first")
                body = None
            else:
                match = body.get("match")
                # Typed by the owner from the App's own screen; the daemon compares it.
                if not isinstance(match, str) or not re.fullmatch(r"[0-9][0-9 -]{0,31}", match.strip()):
                    return _error(400, "Type the match code the App shows")
                body["match"] = match.strip()
            if operation == "approve":
                acknowledged = body.get("acknowledge_elevated", False)
                if type(acknowledged) is not bool:
                    return _error(400, "acknowledge_elevated must be a boolean")
                body.pop("acknowledge_elevated", None)
                if acknowledged:
                    body["acknowledge_elevated"] = True
        try:
            reply, suspected = None, False
            if operation == "approve" and "acknowledge_elevated" not in body:
                # Never send an approval for elevated scopes without the owner's separate acknowledgement.
                reply = await daemon.pairing(key["token"], "list")
                if reply.ok:
                    if not isinstance(reply.body, list):
                        return _error(502, "The daemon returned invalid pairing requests.")
                    row = next((pairing_request_view(row) for row in reply.body
                                if isinstance(row, dict) and row.get("id") == rid), None)
                    if row and row["elevated"]:
                        return _error(400, "Elevated scopes need the separate acknowledgement before approval.",
                                      error={"code": "acknowledgement_required"})
                    reply = None
            if reply is None:
                async with pairing_consent_lock:
                    reply = await daemon.pairing(key["token"], operation, rid, body)
                    if reply.ok:
                        if operation == "list" and isinstance(reply.body, list):
                            suspected = observe_browser_slots(reply.body, key["key_id"])
                        elif operation == "confirm":
                            observe_browser_slots([reply.body], key["key_id"], confirmed_id=rid)
            if reply.status == 401:
                return await refused(key["token"])
        except DaemonUnreachable:
            return _unreachable("The daemon did not answer. Refresh the list before repeating a decision.")
        if not reply.ok:
            return _forwarded(reply)
        if operation == "list":
            if not isinstance(reply.body, list):
                return _error(502, "The daemon returned invalid pairing requests.")
            return {**pairing_view(reply.body, tuple(store.secrets())),
                    "confirmation_warning": await skipped_confirmation(key, suspected)}
        return pairing_request_view(reply.body, tuple(store.secrets()))

    @app.get("/api/manage/catalog")
    async def administration_catalog():
        return {"operations": [{"id": name, "group": spec[0], "label": spec[1], "cli": spec[2],
                                "fields": [{"name": field, "type": kind, "required": field in spec[4]}
                                           for field, kind in spec[3].items()],
                                "confirmation": spec[5], "method": ADMIN_ROUTES[name][0],
                                "targets": re.findall(r"{(\w+)}", ADMIN_ROUTES[name][1])}
                               for name, spec in OPERATIONS.items()],
                "not_offered": [
                    {"label": "Mac runner pairing codes", "reason": "A Hub key cannot mint owner keys "
                               "(daemon refuses with hub_key).",
                     "cli": "harness runner-pairing-codes create"},
                    {"label": "Creating owner keys",
                     "reason": "A Hub key cannot mint owner keys (daemon refuses with hub_key).",
                     "cli": "harness keys create"}]}

    @app.api_route("/api/manage/{operation}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    async def administration_action(operation: str, request: Request):
        if operation not in OPERATIONS or request.method != ADMIN_ROUTES[operation][0]:
            return _error(404, "Unknown administration operation")
        key = claims.key()
        if key is None:
            return _error(409, "Claim this Hub on Overview before administering the daemon.")
        target_names = re.findall(r"{(\w+)}", ADMIN_ROUTES[operation][1])
        targets = {name: request.query_params.get(name, "") for name in target_names}
        if any(not TARGET.fullmatch(value) for value in targets.values()):
            return _error(400, "A valid target identifier is required")
        if len(request.query_params.multi_items()) != len(set(request.query_params)):
            return _error(400, "Repeated query fields are refused")
        params = {name: value for name, value in request.query_params.items() if name not in targets}
        body = None
        try:
            if request.method == "GET":
                fields = OPERATIONS[operation][3]
                if set(params) - set(fields):
                    return _error(400, "Unexpected query fields")
                for name, value in params.items():
                    if fields[name] == "integer":
                        params[name] = int(value)
                    elif fields[name] == "number":
                        params[name] = float(value)
                params = validate_fields(operation, params)
            else:
                if params:
                    return _error(400, "Unexpected query fields")
                if not request.headers.get("origin"):
                    return _error(403, "Administration writes require a same-origin Origin")
                data = bytearray()
                async for chunk in request.stream():
                    if len(data) + len(chunk) > 65536:
                        return _error(413, "Administration body is too large")
                    data.extend(chunk)
                body = validate_fields(operation, json.loads(data or b"{}"))
        except (ValueError, OverflowError):
            return _error(400, "Invalid administration fields; check required values and confirmation")
        try:
            if operation == "skill_install":
                review = await daemon.administration(key["token"], "proposal", targets)
                try:
                    reviewed = administration_view("proposal", review.body, tuple(store.secrets())) if review.ok else {}
                except ValueError:
                    reviewed = {}
                if not reviewed.get("review_complete") or reviewed.get("content_hash") != body["content_hash"]:
                    return _error(409, "The complete proposal must be reviewed at this hash. Review on the host CLI.")
            reply = await daemon.administration(key["token"], operation, targets, body, params)
            if reply.status == 401:
                return await refused(key["token"])
            if not reply.ok:
                return _forwarded(reply)
            if reply.status == 204:
                return Response(status_code=204)
            try:
                shown = administration_view(operation, reply.body, tuple(store.secrets()))
            except ValueError:
                return _error(502, "The daemon returned invalid administration data. Refresh before repeating a write.")
            return JSONResponse(shown, status_code=reply.status)
        except DaemonUnreachable:
            return _unreachable("The daemon did not answer. Read its current state before repeating a write.")

    @app.get("/api/settings/health")
    async def settings_health():
        try:
            reply = await daemon.health()
        except DaemonUnreachable:
            return {"reachable": False, "answered": False, "restarting": restart["active"], "health": None}
        shown = health_view(reply.body)["health"] if reply.ok else None
        observe_restart(shown)
        # reachable: a health answer was read; answered: the daemon replied at all, a 503 mid-restart included.
        return {"reachable": reply.ok, "answered": True, "status": reply.status, "restarting": restart["active"],
                "health": shown}

    @app.api_route("/api/settings/{resource:path}", methods=["GET", "POST", "PUT", "PATCH"])
    async def settings_action(resource: str, request: Request):
        operations = {
            ("GET", "schema"): "schema", ("GET", "config"): "config",
            ("POST", "validate"): "validate", ("PATCH", "config"): "save",
            ("POST", "rollback"): "rollback", ("POST", "restart"): "restart",
            ("GET", "smart-approvals"): "smart", ("PUT", "smart-approvals"): "smart_save",
            ("GET", "backends"): "backends", ("PUT", "backend"): "backend_save",
            ("GET", "memory"): "memory", ("PUT", "memory"): "memory_save",
            ("GET", "github"): "github", ("PUT", "github"): "github_save",
            ("GET", "google"): "google", ("POST", "notify"): "notify",
        }
        operation = operations.get((request.method, resource))
        if operation is None:
            return _error(404, "unknown Settings operation")
        key = claims.key()
        if key is None:
            return _error(409, "Claim this Hub on Overview before administering Settings.")
        body, name = None, ""
        if request.method != "GET":
            if not request.headers.get("origin"):
                return _error(403, "Settings writes require a same-origin Origin")
            data = bytearray()
            async for chunk in request.stream():
                if len(data) + len(chunk) > 262144:
                    return _error(413, "Settings body is too large")
                data.extend(chunk)
            try:
                body = json.loads(data or b"{}")
            except ValueError:
                return _error(400, "Settings body must be JSON")
            fields = {"validate": {"revision", "changes"}, "save": {"revision", "changes"},
                      "rollback": {"revision", "confirm"}, "restart": {"revision", "confirm"},
                      "smart_save": {"mode"}, "backend_save": {"name", "model", "effort"},
                      "memory_save": {"content", "summary"}, "github_save": {"enabled"}, "notify": set()}
            if not isinstance(body, dict) or set(body) - fields[operation]:
                return _error(400, "unexpected Settings fields")
            if operation in ("validate", "save", "rollback", "restart") and (
                    type(body.get("revision")) is not int or body["revision"] < 0):
                return _error(400, "a non-negative settings revision is required")
            if operation in ("validate", "save") and not isinstance(body.get("changes"), dict):
                return _error(400, "changes must be an object")
            if operation in ("rollback", "restart") and body.get("confirm") is not True:
                return _error(400, "confirm this action in the page first")
            if operation == "backend_save":
                name = body.pop("name", "")
                if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", name):
                    return _error(400, "invalid backend name")
            if operation == "github_save" and type(body.get("enabled")) is not bool:
                return _error(400, "enabled must be a boolean")
        try:
            reply = await daemon.settings(key["token"], operation, body, name)
            if reply.status == 401:
                return await refused(key["token"])
        except DaemonUnreachable:
            return _unreachable("The daemon did not answer. Check its health before repeating a write.")
        if operation == "memory" and reply.status == 404 and memory_route_absent(reply.body):
            return JSONResponse(control_view(operation, {"enabled": False}, installed=False))
        if not reply.ok:
            return _forwarded(reply)
        if operation == "restart" and isinstance(reply.body, dict) and reply.body.get("accepted") is True:
            restart.update(active=True, target=reply.body.get("target_revision"),
                           not_before=time.monotonic() + RESTART_GRACE_SECONDS)
        shown = registry_view(reply.body) if operation in (
            "schema", "config", "validate", "save", "rollback", "restart") else control_view(operation, reply.body)
        return JSONResponse(shown, status_code=reply.status)

    @app.get("/api/claim")
    async def get_claim():
        return claim_view(claims)

    @app.post("/api/claim")
    async def start_claim():
        try:
            await claims.start()
        except ClaimError as exc:
            status = exc.status or {"refused": 409, "rate_limited": 429, "claimed": 409,
                      "persisting": 503, "pending": 503}.get(exc.state, 502)
            return JSONResponse({"detail": str(exc), "claim": claim_view(claims)}, status_code=status)
        return JSONResponse(claim_view(claims), status_code=201)

    @app.delete("/api/claim")
    async def forget_claim():
        try:
            await claims.forget()
        except ClaimError as exc:
            return _error(503, str(exc), claim=claim_view(claims))
        return claim_view(claims)

    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(str(STATIC / "index.html"), headers={"cache-control": "no-cache"})

    app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")

    identity = TailnetIdentity(ResponseGuard(app, claims.secrets), check=peer_check,
                               trust_unverified=settings.trust_unverified_identity_headers)
    served = HostCheck(identity, settings.allowed_hosts())
    served.fastapi = app
    return served
