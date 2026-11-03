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
import time
from functools import partial
from importlib import resources
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.types import ASGIApp, Receive, Scope, Send

from . import __version__
from .administration import OPERATIONS, TARGET, validate_fields
from .auth import Sessions
from .claim import CLAIMED, ClaimError, ClaimManager
from .config import Settings
from .daemon import ADMIN_ROUTES, DaemonClient, DaemonUnreachable, route_targets
from .guard import ResponseGuard, leaks
from .inventory import InventoryCache, valid_inventory
from .routes import apps as apps_routes
from .routes import common
from .routes import marketplace as marketplace_routes
from .routes import session as session_routes
from .routes.common import HubContext, concurrently, settled
from .routes.common import error as _error
from .routes.common import forwarded as _forwarded
from .routes.common import json_body as _json_body  # noqa: F401 (re-exported: tests import it from here)
from .routes.common import read_json as _read_json
from .routes.common import unclaimed as _unclaimed
from .routes.common import unreachable as _unreachable
from .routes.session import MAX_SIGNIN_BODY as MAX_SIGNIN_BODY  # re-exported: tests import it from here
from .routes.session import SIGNIN_COMMAND
from .state import SERVER_FILE, StateStore, write_private
from .status import StatusReporter
from .tailnet import PeerCheck, TailnetIdentity
from .views import (
    ID_SEGMENT,
    administration_view,
    apps_view,
    claim_view,
    control_view,
    health_view,
    memory_route_absent,
    module_view,
    overview,
    ownership_view,
    pairing_attention,
    registry_view,
    settings_error,
    spend_view,
)

log = logging.getLogger("harness_hub")

STATIC = resources.files("harness_hub") / "static"
REQUEST_HEADER = "x-hub-request"
# Timing constants stay here for every router, which reads them through `routes.common.tuning()` on each use.
ALLOWED_LOGINS_SECONDS = 5.0  # how long the Hub reuses the daemon's allowed_logins answer
OUTAGE_GRACE_SECONDS = 120.0  # how long after the daemon last listed a tailnet tab's login an outage keeps the tab
PAIRING_COUNT_SECONDS = 5.0  # how long Overview waits for its optional pairing count, consent lock included
SPEND_CAP_READS = 8  # App session-cap reads in flight at once; every live App is read
RESTART_GRACE_SECONDS = 1.0  # how long after a restart request the Hub ignores health that predates it
KNOWN_RECOVERIES = 16  # recoveries the Hub remembers from daemon replies, to name in a restart's 202 (#143)
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


def create_app(settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None,
               peer_check: PeerCheck | None = None) -> HostCheck:
    store = StateStore(settings.state_dir)
    daemon = DaemonClient(settings.daemon_url, transport=transport)
    claims = ClaimManager(store, daemon, name=settings.claim_name, claim_file=settings.claim_file,
                          poll_seconds=settings.poll_seconds)
    sessions = Sessions(settings.state_dir)
    inventory = InventoryCache(store.dir, settings.daemon_url)
    peer_check = peer_check or PeerCheck()
    status = StatusReporter(store.dir, daemon, claims.state, interval=settings.status_seconds, build=settings.build)

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
        status.start()
        try:
            yield
        finally:
            await status.stop()
            await claims.stop()

    app = FastAPI(title="Hub", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None,
                  openapi_url=None)
    app.state.claims = claims
    app.state.store = store
    app.state.sessions = sessions
    app.state.inventory = inventory
    app.state.hub = hub = HubContext(settings=settings, store=store, daemon=daemon, claims=claims, sessions=sessions,
                                     inventory=inventory, peer_check=peer_check)
    restart = {"active": False, "target": None, "generation": 0, "not_before": 0.0}
    recoveries: list[dict] = []  # the newest distinct recoveries any daemon reply reported, oldest first

    def observe_recovery(recovery: dict | None) -> None:
        """Remember a recovery a daemon reply reported. Any reply the Hub has before it asks for a restart was sampled
        before that restart, so these are the recoveries the shell must not take for one of it (#143). One the response
        guard would withhold is not kept: the page never had it, and naming it would withhold every restart reply."""
        if recovery and recovery not in recoveries and not leaks(json.dumps(recovery).encode(), store.secrets()):
            recoveries.append(recovery)
            del recoveries[:-KNOWN_RECOVERIES]

    def observe_restart(shown: dict | None, health_generation: int) -> None:
        """End a restart the Hub started once the daemon answers healthy with confirmed settings, at the target revision
        or a later one (a save made during the restart, which the restarted daemon reports), or after a recovery. Only
        a health request begun since this restart can end it. Overview and the Settings health poll both look, so
        neither page has to stay open."""
        revision = shown["config_revision"] if shown else None
        if (health_generation == restart["generation"] and time.monotonic() >= restart["not_before"]
                and shown and shown["ok"] and shown["config_confirmed"]) and (
                restart["target"] is None or isinstance(revision, int) and revision >= restart["target"]
                or shown["recovery"]):
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
        if not await session_routes.signed_in(hub, request):
            return _error(401, "sign in first", signin_command=SIGNIN_COMMAND)
        return await call_next(request)

    @app.get("/healthz", include_in_schema=False)
    async def healthz():
        return {"ok": True, "version": __version__}

    session_routes.add_routes(app)

    pairing_consent_lock = hub.pairing_consent_lock
    retained, refused = partial(common.retained, hub), partial(common.refused, hub)
    read_inventory = partial(common.read_inventory, hub)
    observe_browser_slots = partial(marketplace_routes.observe_browser_slots, hub)

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
        health_generation = restart["generation"]
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
        if health:
            observe_recovery(health["health"]["recovery"])
        observe_restart(health["health"] if health else None, health_generation)
        result["answered"] = answered
        result["restarting"] = restart["active"]
        result["attention"] = attention
        return result

    @app.get("/api/apps")
    @app.get("/api/modules")
    async def get_modules(request: Request):
        key = claims.key()
        if key is None:
            return _unclaimed("Claim this Hub on Overview before reading inventory.")
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
            return _unclaimed("Claim this Hub on Overview before reading spend.")
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
                and isinstance(row.get("id"), str) and ID_SEGMENT.fullmatch(row["id"])]
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

    marketplace_routes.add_routes(app, hub)

    apps_routes.add_routes(app)

    @app.get("/api/manage/catalog")
    async def administration_catalog():
        return {"operations": [{"id": name, "group": spec.group, "label": spec.label, "cli": spec.cli,
                                "fields": [{"name": field, "type": kind, "required": field in spec.required}
                                           for field, kind in spec.fields.items()],
                                "confirmation": spec.confirmation, "method": ADMIN_ROUTES[name][0],
                                "targets": route_targets(ADMIN_ROUTES[name][1])}
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
            return _unclaimed("Claim this Hub on Overview before administering the daemon.")
        target_names = route_targets(ADMIN_ROUTES[operation][1])
        targets = {name: request.query_params.get(name, "") for name in target_names}
        if any(not TARGET.fullmatch(value) for value in targets.values()):
            return _error(400, "A valid target identifier is required")
        if len(request.query_params.multi_items()) != len(set(request.query_params)):
            return _error(400, "Repeated query fields are refused")
        params = {name: value for name, value in request.query_params.items() if name not in targets}
        body, invalid = None, "Invalid administration fields; check required values and confirmation"
        try:
            if request.method == "GET":
                fields = OPERATIONS[operation].fields
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
                body = validate_fields(operation, await _read_json(
                    request, 65536, "Administration", "Administration writes require a same-origin Origin", invalid))
        except (ValueError, OverflowError):
            return _error(400, invalid)
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
            if operation in ("google_invite", "account"):
                # The page times the link code and its "open until" against this clock, not the browser's (#132).
                shown["now"] = time.time()
            return JSONResponse(shown, status_code=reply.status)
        except DaemonUnreachable:
            return _unreachable("The daemon did not answer. Read its current state before repeating a write.")

    @app.get("/api/settings/health")
    async def settings_health():
        health_generation = restart["generation"]
        try:
            reply = await daemon.health()
        except DaemonUnreachable:
            return {"reachable": False, "answered": False, "restarting": restart["active"], "health": None}
        shown = health_view(reply.body)["health"] if reply.ok else None
        if shown:
            observe_recovery(shown["recovery"])
        observe_restart(shown, health_generation)
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
            return _unclaimed("Claim this Hub on Overview before administering Settings.")
        body, name = None, ""
        if request.method != "GET":
            body = await _read_json(request, 262144, "Settings", "Settings writes require a same-origin Origin")
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
                if not isinstance(name, str) or not ID_SEGMENT.fullmatch(name):
                    return _error(400, "invalid backend name")
            if operation == "github_save" and type(body.get("enabled")) is not bool:
                return _error(400, "enabled must be a boolean")
        # Taken before the restart is asked for: a reply that comes in while it is under way may already be from it.
        known = list(recoveries)
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
        shown = registry_view(reply.body) if operation in (
            "schema", "config", "validate", "save", "rollback", "restart") else control_view(operation, reply.body)
        if operation in ("config", "save", "rollback", "restart"):
            observe_recovery(shown["recovery"])
        if operation == "restart" and isinstance(reply.body, dict) and reply.body.get("accepted") is True:
            accepted_at = time.monotonic()
            restart.update(active=True, target=reply.body.get("target_revision"), generation=restart["generation"] + 1,
                           not_before=accepted_at + RESTART_GRACE_SECONDS)
            # The recoveries known before this restart: the shell keeps them from ending its target (#143).
            shown["known_recoveries"] = known
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
