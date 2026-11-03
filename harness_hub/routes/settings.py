"""The manage router: the daemon administration the Hub offers (`/api/manage/*`, from `administration.OPERATIONS`), with
the Hub's state on `HubContext` (#190). The Settings routes join it next."""

from __future__ import annotations

import time

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from ..administration import OPERATIONS, TARGET, validate_fields
from ..daemon import ADMIN_ROUTES, DaemonUnreachable, route_targets
from ..views import administration_view
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
                 "cli": "harness keys create"}]}


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


def add_routes(app: FastAPI) -> None:
    """Add the manage routes to the app itself, in this order, as `session.add_routes` does (#190)."""
    app.add_api_route("/api/manage/catalog", administration_catalog, methods=["GET"])
    app.add_api_route("/api/manage/{operation}", administration_action,
                      methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
