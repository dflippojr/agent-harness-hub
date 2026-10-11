"""The Hub's HTTP server: a no-build ES-module UI plus the small JSON API it calls.

Browser-facing routes (`/api/...`) answer only a signed-in owner (`auth.py`) and only with allowlisted view models
(`views.py`). The Hub key never leaves this process except in the Authorization header of the Hub's own calls to its
daemon.
"""

from __future__ import annotations

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
from .guard import ResponseGuard, leaks
from .inventory import InventoryCache, valid_inventory
from .state import SERVER_FILE, StateStore, write_private
from .views import (
    administration_view,
    app_view,
    apps_view,
    claim_view,
    control_view,
    erasures_view,
    health_view,
    limits_view,
    module_view,
    overview,
    ownership_view,
    pairing_request_view,
    pairing_view,
    registry_view,
    settings_error,
    valid_limits,
)

log = logging.getLogger("harness_hub")

STATIC = resources.files("harness_hub") / "static"
SIGNIN_COMMAND = "python -m harness_hub signin-link"
REQUEST_HEADER = "x-hub-request"
SESSION_HEADER = "x-hub-session"
MAX_SIGNIN_BODY = 1024
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
    inventory = InventoryCache(store.dir, settings.daemon_url)

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
    app.state.inventory = inventory
    restart = {"active": False, "target": None, "not_before": 0.0}

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
        return {"signed_in": sessions.valid(request.cookies.get(settings.cookie_name),
                                            request.headers.get(SESSION_HEADER)), "signin_command": SIGNIN_COMMAND,
                "version": __version__}

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
        code = body.get("code") if isinstance(body, dict) else None
        was_throttled = sessions.throttled()
        credentials = sessions.redeem_code(code)
        if credentials is None:
            if was_throttled:
                return _error(429, "too many sign-in attempts; wait a minute")
            return _error(401, "that sign-in link is not valid; links work once, for ten minutes",
                          signin_command=SIGNIN_COMMAND)
        session, binding = credentials
        response = Response(status_code=204, headers={SESSION_HEADER: binding})
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
                    # A transient rejection must not destroy the owner's persistent key.
                    confirmation = await daemon.admin(key["token"])
                    if confirmation.status == 401:
                        claims.released()
                    else:
                        error = "the owner API rejected the key once; retained it pending confirmation"
                elif discovery.ok and record.ok:
                    ownership = ownership_view(discovery.body, record.body, key["key_id"])
                else:
                    error = f"the owner API answered HTTP {discovery.status if not discovery.ok else record.status}"
        except DaemonUnreachable:
            error = f"the daemon at {settings.daemon_url} did not answer"
        result = overview(claims, settings.daemon_url, health, ownership, error)
        result["restarting"] = restart["active"]
        return result

    @app.get("/api/apps")
    @app.get("/api/modules")
    async def get_modules(request: Request):
        key = claims.key()
        if key is None:
            return _error(409, "Claim this Hub on Overview before reading inventory.")
        good = inventory.get(key["key_id"])
        state, error = "ready", ""
        try:
            reply = await daemon.inventory(key["token"])
            if reply.status == 401:
                confirmation = await daemon.admin(key["token"])
                if confirmation.status == 401:
                    claims.released()
                return _error(409, "The daemon refused this Hub's key. Check Overview before trying again.")
            if reply.ok:
                if not valid_inventory(reply.body):
                    state, error = "error", "The daemon returned an invalid inventory."
                elif leaks(json.dumps(apps_view(reply.body, key["key_id"], tuple(store.secrets()))
                                      if request.url.path == "/api/apps" else module_view(reply.body)).encode(),
                           store.secrets()):
                    state, error = "error", "Inventory withheld: it contained a credential-like string."
                else:
                    good = inventory.save(reply.body, key["key_id"])
            elif reply.status == 404:
                state, error = "absent", "The Hub inventory module is not installed."
            elif reply.status == 400 and "disabled" in reply.detail.lower():
                state, error = "disabled", "Hub inventory is disabled. Switch it on below."
            else:
                state, error = "error", settings_error(reply.body, reply.status)["detail"]
        except DaemonUnreachable:
            state, error = "error", "The daemon did not answer."
        except (OSError, ValueError):
            state, error = "error", "The inventory could not be saved in the private state directory."
        if request.url.path == "/api/apps":
            shown = apps_view(good["inventory"], key["key_id"], tuple(store.secrets())) if good else {
                "apps": None, "entries": []}
        else:
            shown = {"modules": module_view(good["inventory"]) if good else None}
        return {"state": state, "error": error, "stale": state != "ready" and good is not None,
                "fetched_at": good["fetched_at"] if good else None, **shown}

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
            reply = await daemon.apps(key["token"], operation, app_id, body)
            if reply.status == 401:
                confirmation = await daemon.admin(key["token"])
                if confirmation.status == 401:
                    claims.released()
                return _error(409, "The daemon refused this Hub's key. Check Overview before trying again.")
            if not reply.ok:
                return JSONResponse(settings_error(reply.body, reply.status), status_code=reply.status)
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
                # /hub does not carry retention; read only the token-free key list for this detail.
                keys = await daemon.apps(key["token"], "keys")
                if keys.status == 401:
                    confirmation = await daemon.admin(key["token"])
                    if confirmation.status == 401:
                        claims.released()
                    return _error(409, "The daemon refused this Hub's key. Check Overview before trying again.")
                if not keys.ok:
                    return _error(keys.status, f"App detail could not be read (HTTP {keys.status}).")
                if not isinstance(keys.body, list):
                    return _error(502, "The daemon returned invalid App detail.")
                row = next((row for row in keys.body if isinstance(row, dict) and row.get("id") == app_id), None)
                if row is None:
                    return _error(404, "The App no longer appears in the key list. Refresh Apps.")
                return {**shown, "retention_days": app_view(row)["retention_days"]}
            return {"id": app_view(reply.body)["id"], "retention_days": app_view(reply.body)["retention_days"]}
        except DaemonUnreachable:
            return _error(503, "The daemon did not answer. Refresh the state before repeating a write.")

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
            reply = None
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
                reply = await daemon.pairing(key["token"], operation, rid, body)
            if reply.status == 401:
                confirmation = await daemon.admin(key["token"])
                if confirmation.status == 401:
                    claims.released()
                return _error(409, "The daemon refused this Hub's key. Check Overview before trying again.")
        except DaemonUnreachable:
            return _error(503, "The daemon did not answer. Refresh the list before repeating a decision.")
        if not reply.ok:
            return JSONResponse(settings_error(reply.body, reply.status), status_code=reply.status)
        if operation == "list":
            if not isinstance(reply.body, list):
                return _error(502, "The daemon returned invalid pairing requests.")
            return pairing_view(reply.body, tuple(store.secrets()))
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
                confirmation = await daemon.admin(key["token"])
                if confirmation.status == 401:
                    claims.released()
                return _error(409, "The daemon refused this Hub's key. Check Overview before trying again.")
            if not reply.ok:
                return JSONResponse(settings_error(reply.body, reply.status), status_code=reply.status)
            if reply.status == 204:
                return Response(status_code=204)
            try:
                shown = administration_view(operation, reply.body, tuple(store.secrets()))
            except ValueError:
                return _error(502, "The daemon returned invalid administration data. Refresh before repeating a write.")
            return JSONResponse(shown, status_code=reply.status)
        except DaemonUnreachable:
            return _error(503, "The daemon did not answer. Read its current state before repeating a write.")

    @app.get("/api/settings/health")
    async def settings_health():
        try:
            reply = await daemon.health()
        except DaemonUnreachable:
            return {"reachable": False, "restarting": restart["active"], "health": None}
        shown = health_view(reply.body)["health"] if reply.ok else None
        if time.monotonic() >= restart["not_before"] and shown and shown["ok"] and shown["config_confirmed"] and (
                restart["target"] is None or shown["config_revision"] == restart["target"] or shown["recovery"]):
            restart["active"] = False
        return {"reachable": reply.ok, "restarting": restart["active"], "health": shown}

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
                confirmation = await daemon.admin(key["token"])
                if confirmation.status == 401:
                    claims.released()
                return _error(409, "The daemon refused this Hub's key. Check Overview before trying again.")
        except DaemonUnreachable:
            return _error(503, "The daemon did not answer. Check its health before repeating a write.")
        if not reply.ok:
            return JSONResponse(settings_error(reply.body, reply.status), status_code=reply.status)
        if operation == "restart" and isinstance(reply.body, dict) and reply.body.get("accepted") is True:
            restart.update(active=True, target=reply.body.get("target_revision"),
                           not_before=time.monotonic() + 1)
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
