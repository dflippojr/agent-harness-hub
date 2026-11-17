"""The manage and Settings router: the daemon administration the Hub offers (`/api/manage/*`, from
`administration.OPERATIONS`), and Settings (`/api/settings/*`). The restart and recovery it tracks live in `restart.py`
(#190, #255)."""

from __future__ import annotations

import time

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from ..administration import OPERATIONS, TARGET, validate_fields
from ..daemon import ADMIN_ROUTES, DaemonUnreachable, route_targets
from ..restart import observe_recovery, observe_restart
from ..views import ID_SEGMENT, administration_view, control_view, health_view, memory_route_absent, registry_view
from .common import error, forwarded, hub, read_json, refused, unclaimed, unreachable


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
                 "cli": "harness keys create <name> --kind owner --scopes admin"}]}


async def administration_action(operation: str, request: Request):
    ctx = hub(request)
    if operation not in OPERATIONS or request.method != ADMIN_ROUTES[operation][0]:
        return error(404, "Unknown administration operation")
    key = ctx.claims.key()
    if key is None:
        return unclaimed("Claim this Hub on Overview before administering the daemon.")
    target_names = route_targets(ADMIN_ROUTES[operation][1])
    targets = {name: request.query_params.get(name, "") for name in target_names}
    if any(not TARGET.fullmatch(value) for value in targets.values()):
        return error(400, "A valid target identifier is required")
    if len(request.query_params.multi_items()) != len(set(request.query_params)):
        return error(400, "Repeated query fields are refused")
    params = {name: value for name, value in request.query_params.items() if name not in targets}
    body, invalid = None, "Invalid administration fields; check required values and confirmation"
    try:
        if request.method == "GET":
            fields = OPERATIONS[operation].fields
            if set(params) - set(fields):
                return error(400, "Unexpected query fields")
            for name, value in params.items():
                if fields[name] == "integer":
                    params[name] = int(value)
                elif fields[name] == "number":
                    params[name] = float(value)
            params = validate_fields(operation, params)
        else:
            if params:
                return error(400, "Unexpected query fields")
            body = validate_fields(operation, await read_json(
                request, 65536, "Administration", "Administration writes require a same-origin Origin", invalid))
    except (ValueError, OverflowError):
        return error(400, invalid)
    try:
        if operation == "skill_install":
            review = await ctx.daemon.administration(key["token"], "proposal", targets)
            try:
                reviewed = administration_view("proposal", review.body, tuple(ctx.store.secrets())) if review.ok else {}
            except ValueError:
                reviewed = {}
            if not reviewed.get("review_complete") or reviewed.get("content_hash") != body["content_hash"]:
                return error(409, "The complete proposal must be reviewed at this hash. Review on the host CLI.")
        reply = await ctx.daemon.administration(key["token"], operation, targets, body, params)
        if reply.status == 401:
            return await refused(ctx, key["token"])
        if not reply.ok:
            return forwarded(reply)
        if reply.status == 204:
            return Response(status_code=204)
        try:
            shown = administration_view(operation, reply.body, tuple(ctx.store.secrets()))
        except ValueError:
            return error(502, "The daemon returned invalid administration data. Refresh before repeating a write.")
        if operation in ("google_invite", "account"):
            # The page times the link code and its "open until" against this clock, not the browser's (#132).
            shown["now"] = time.time()
        return JSONResponse(shown, status_code=reply.status)
    except DaemonUnreachable:
        return unreachable("The daemon did not answer. Read its current state before repeating a write.")


async def settings_health(request: Request):
    ctx = hub(request)
    health_generation = ctx.restart["generation"]
    try:
        reply = await ctx.daemon.health()
    except DaemonUnreachable:
        return {"reachable": False, "answered": False, "restarting": ctx.restart["active"], "health": None}
    shown = health_view(reply.body)["health"] if reply.ok else None
    if shown:
        observe_recovery(ctx, shown["recovery"])
    observe_restart(ctx, shown, health_generation)
    # reachable: a health answer was read; answered: the daemon replied at all, a 503 mid-restart included.
    return {"reachable": reply.ok, "answered": True, "status": reply.status, "restarting": ctx.restart["active"],
            "health": shown}


async def settings_action(resource: str, request: Request):
    ctx = hub(request)
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
        return error(404, "unknown Settings operation")
    key = ctx.claims.key()
    if key is None:
        return unclaimed("Claim this Hub on Overview before administering Settings.")
    body, name = None, ""
    if request.method != "GET":
        body = await read_json(request, 262144, "Settings", "Settings writes require a same-origin Origin")
        fields = {"validate": {"revision", "changes"}, "save": {"revision", "changes"},
                  "rollback": {"revision", "confirm"}, "restart": {"revision", "confirm"},
                  "smart_save": {"mode"}, "backend_save": {"name", "model", "effort"},
                  "memory_save": {"content", "summary"}, "github_save": {"enabled"}, "notify": set()}
        if not isinstance(body, dict) or set(body) - fields[operation]:
            return error(400, "unexpected Settings fields")
        if operation in ("validate", "save", "rollback", "restart") and (
                type(body.get("revision")) is not int or body["revision"] < 0):
            return error(400, "a non-negative settings revision is required")
        if operation in ("validate", "save") and not isinstance(body.get("changes"), dict):
            return error(400, "changes must be an object")
        if operation in ("rollback", "restart") and body.get("confirm") is not True:
            return error(400, "confirm this action in the page first")
        if operation == "backend_save":
            name = body.pop("name", "")
            if not isinstance(name, str) or not ID_SEGMENT.fullmatch(name):
                return error(400, "invalid backend name")
        if operation == "github_save" and type(body.get("enabled")) is not bool:
            return error(400, "enabled must be a boolean")
    # Taken before the restart is asked for: a reply that comes in while it is under way may already be from it.
    known = list(ctx.recoveries)
    try:
        reply = await ctx.daemon.settings(key["token"], operation, body, name)
        if reply.status == 401:
            return await refused(ctx, key["token"])
    except DaemonUnreachable:
        return unreachable("The daemon did not answer. Check its health before repeating a write.")
    if operation == "memory" and reply.status == 404 and memory_route_absent(reply.body):
        return JSONResponse(control_view(operation, {"enabled": False}, installed=False))
    if not reply.ok:
        return forwarded(reply)
    shown = registry_view(reply.body) if operation in (
        "schema", "config", "validate", "save", "rollback", "restart") else control_view(operation, reply.body)
    if operation in ("config", "save", "rollback", "restart"):
        observe_recovery(ctx, shown["recovery"])
    if operation == "restart" and isinstance(reply.body, dict) and reply.body.get("accepted") is True:
        accepted_at = time.monotonic()
        ctx.restart.update(active=True, target=reply.body.get("target_revision"),
                           generation=ctx.restart["generation"] + 1,
                           not_before=accepted_at + ctx.tuning.restart_grace_seconds)
        # The recoveries known before this restart: the shell keeps them from ending its target (#143).
        shown["known_recoveries"] = known
    return JSONResponse(shown, status_code=reply.status)


def add_routes(app: FastAPI) -> None:
    """Add the manage and Settings routes to the app itself, in this order, as `session.add_routes` does (#190)."""
    app.add_api_route("/api/manage/catalog", administration_catalog, methods=["GET"])
    app.add_api_route("/api/manage/{operation}", administration_action,
                      methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    app.add_api_route("/api/settings/health", settings_health, methods=["GET"])
    app.add_api_route("/api/settings/{resource:path}", settings_action, methods=["GET", "POST", "PUT", "PATCH"])
