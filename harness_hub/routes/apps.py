"""The Apps router: App administration (revoke, session caps, retention, erasures) and App pairing requests (list,
approve, confirm, deny), with the Hub's state on `HubContext` (#190)."""

from __future__ import annotations

import math
import re

from fastapi import FastAPI, Request
from fastapi.responses import Response

from ..daemon import DaemonUnreachable
from ..views import (
    ID_SEGMENT,
    app_view,
    erasures_view,
    limits_view,
    pairing_request_view,
    pairing_view,
    valid_limits,
)
from .common import (
    concurrently,
    error,
    error_status,
    forwarded,
    hub,
    read_json,
    refused,
    settled,
    unclaimed,
    unreachable,
)
from .marketplace import observe_browser_slots, skipped_confirmation


async def apps_action(resource: str, request: Request):
    ctx = hub(request)
    operation, app_id = None, ""
    if resource == "erasures" and request.method == "GET":
        operation = "erasures"
    else:
        match = re.fullmatch(rf"({ID_SEGMENT.pattern})/(revoke|limits|retention)", resource)
        if match:
            app_id, action = match.groups()
            operation = {("DELETE", "revoke"): "revoke", ("GET", "limits"): "limits",
                         ("PUT", "limits"): "limits_save", ("PUT", "retention"): "retention"}.get(
                             (request.method, action))
    if operation is None:
        return error(404, "Unknown Apps operation. Restore is available only on the daemon host CLI.")
    key = ctx.claims.key()
    if key is None:
        return unclaimed("Claim this Hub on Overview before administering Apps.")
    body = None
    if request.method != "GET":
        body = await read_json(request, 1024, "Apps", "Apps writes require a same-origin Origin")
        fields = {"revoke": {"confirm"}, "limits_save": {"max_running", "max_queued"},
                  "retention": {"retention_days", "confirm"}}
        if not isinstance(body, dict) or not body or set(body) - fields[operation]:
            return error(400, "Unexpected Apps fields")
        if operation in ("revoke", "retention") and body.pop("confirm", None) is not True:
            return error(400, "Confirm this action in the page first")
        if operation == "limits_save" and any(value is not None and (
                type(value) is not int or not 1 <= value <= (100 if name == "max_running" else 1000))
                for name, value in body.items()):
            return error(400, "Session caps must be integers in range, or null for the daemon default")
        if operation == "retention":
            days = body.get("retention_days")
            if "retention_days" not in body or days is not None and (
                    type(days) not in (int, float) or not math.isfinite(days) or not 0 < days <= 36500):
                return error(400, "Retention must be 0 < days <= 36500, or null to keep until deleted")
        if operation == "revoke" and app_id == key["key_id"]:
            return error(409, "This Hub is released only on the daemon host: harness hub release --confirm",
                         release_command="harness hub release --confirm")
    try:
        if operation == "limits":
            # /hub does not carry retention, so the detail also needs the token-free key list: read both at once.
            reply, keys = await concurrently(ctx.daemon.apps(key["token"], "limits", app_id),
                                             ctx.daemon.apps(key["token"], "keys"))
            reply = settled(reply)
        else:
            reply, keys = await ctx.daemon.apps(key["token"], operation, app_id, body), None
        if reply.status == 401:
            return await refused(ctx, key["token"])
        if not reply.ok:
            return forwarded(reply)
        if operation == "revoke":
            return Response(status_code=204)
        if operation == "erasures":
            if not isinstance(reply.body, list):
                return error(502, "The daemon returned invalid erasure data.")
            return erasures_view(reply.body, tuple(ctx.store.secrets()))
        if operation in ("limits", "limits_save"):
            if not valid_limits(reply.body):
                return error(502, "The daemon returned invalid session limits.")
            shown = limits_view(reply.body)
            if operation == "limits_save":
                return shown
            keys = settled(keys)
            if keys.status == 401:
                return await refused(ctx, key["token"])
            if not keys.ok:
                return error(error_status(keys.status), f"App detail could not be read (HTTP {keys.status}).",
                             reachable=True)
            if not isinstance(keys.body, list):
                return error(502, "The daemon returned invalid App detail.")
            row = next((row for row in keys.body if isinstance(row, dict) and row.get("id") == app_id), None)
            if row is None:
                return error(404, "The App no longer appears in the key list. Refresh Apps.")
            return {**shown, "retention_days": app_view(row)["retention_days"]}
        view = app_view(reply.body)
        return {"id": view["id"], "retention_days": view["retention_days"]}
    except DaemonUnreachable:
        return unreachable("The daemon did not answer. Refresh the state before repeating a write.")


async def pairing_action(request: Request, rid: str = "", action: str = ""):
    """App pairing requests: list, approve, confirm or deny. Tokens go from the daemon straight to the App."""
    ctx = hub(request)
    operation = action or "list"
    if operation not in ("list", "approve", "confirm", "deny") or rid and not ID_SEGMENT.fullmatch(rid):
        return error(404, "Unknown pairing operation")
    key = ctx.claims.key()
    if key is None:
        return unclaimed("Claim this Hub on Overview before deciding pairing requests.")
    body = None
    if request.method == "POST":
        body = await read_json(request, 1024, "Pairing", "Pairing decisions require a same-origin Origin")
        fields = {"approve": {"match", "acknowledge_elevated"}, "confirm": {"match"}, "deny": {"confirm"}}
        if not isinstance(body, dict) or set(body) - fields[operation]:
            return error(400, "Unexpected pairing fields")
        if operation == "deny":
            if body.pop("confirm", None) is not True:
                return error(400, "Confirm this action in the page first")
            body = None
        else:
            match = body.get("match")
            # Typed by the owner from the App's own screen; the daemon compares it.
            if not isinstance(match, str) or not re.fullmatch(r"[0-9][0-9 -]{0,31}", match.strip()):
                return error(400, "Type the match code the App shows")
            body["match"] = match.strip()
        if operation == "approve":
            acknowledged = body.get("acknowledge_elevated", False)
            if type(acknowledged) is not bool:
                return error(400, "acknowledge_elevated must be a boolean")
            body.pop("acknowledge_elevated", None)
            if acknowledged:
                body["acknowledge_elevated"] = True
    try:
        reply, suspected = None, False
        if operation == "approve" and "acknowledge_elevated" not in body:
            # Never send an approval for elevated scopes without the owner's separate acknowledgement.
            reply = await ctx.daemon.pairing(key["token"], "list")
            if reply.ok:
                if not isinstance(reply.body, list):
                    return error(502, "The daemon returned invalid pairing requests.")
                row = next((pairing_request_view(row) for row in reply.body
                            if isinstance(row, dict) and row.get("id") == rid), None)
                if row and row["elevated"]:
                    return error(400, "Elevated scopes need the separate acknowledgement before approval.",
                                 error={"code": "acknowledgement_required"})
                reply = None
        if reply is None:
            async with ctx.pairing_consent_lock:
                reply = await ctx.daemon.pairing(key["token"], operation, rid, body)
                if reply.ok:
                    if operation == "list" and isinstance(reply.body, list):
                        suspected = observe_browser_slots(ctx, reply.body, key["key_id"])
                    elif operation == "confirm":
                        observe_browser_slots(ctx, [reply.body], key["key_id"], confirmed_id=rid)
        if reply.status == 401:
            return await refused(ctx, key["token"])
    except DaemonUnreachable:
        return unreachable("The daemon did not answer. Refresh the list before repeating a decision.")
    if not reply.ok:
        return forwarded(reply)
    if operation == "list":
        if not isinstance(reply.body, list):
            return error(502, "The daemon returned invalid pairing requests.")
        return {**pairing_view(reply.body, tuple(ctx.store.secrets())),
                "confirmation_warning": await skipped_confirmation(ctx, key, suspected)}
    return pairing_request_view(reply.body, tuple(ctx.store.secrets()))


def add_routes(app: FastAPI) -> None:
    """Add the Apps and pairing routes to the app itself, in this order, as `session.add_routes` does (#190)."""
    app.add_api_route("/api/apps/{resource:path}", apps_action, methods=["GET", "PUT", "DELETE", "POST"])
    app.add_api_route("/api/pairing/{rid}/{action}", pairing_action, methods=["POST"])
    app.add_api_route("/api/pairing", pairing_action, methods=["GET"])
