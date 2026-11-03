"""The Marketplace routes (the listing and arming a slot), and what they share with the pairing listings: the consent
screen's copies and the backstop for a browser claim that skipped confirmation, with their state on `HubContext`."""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from ..daemon import DaemonUnreachable
from ..inventory import valid_inventory
from ..views import (
    display_text,
    entries_view,
    entry_digest,
    entry_view,
    marketplace_slots,
    normalize_origin,
    ownership_view,
    pairing_request_view,
    scope_disclosures,
    settings_error,
)
from .common import (
    HubContext,
    concurrently,
    error,
    error_status,
    hub,
    read_inventory,
    read_json,
    refused,
    settled,
    unclaimed,
    unreachable,
)


def observe_browser_slots(ctx: HubContext, rows: list, key_id: str, *, confirmed_id: str = "") -> bool:
    """Whether an observed browser claim looks like it skipped confirmation; `skipped_confirmation` rules on it."""
    states, warning = ctx.browser_slot_states, False
    for raw in rows:
        row = pairing_request_view(raw)
        if not (row["id"] and row["kind"] == "app" and row["armed"] and row["browser"]):
            continue
        identity = (key_id, row["id"])
        previous, bypassed = states.get(identity, ("", False))
        bypassed = bypassed or (previous == "armed" and row["state"] in ("approved", "redeemed")
                                and row["id"] != confirmed_id)
        if identity not in states and len(states) >= 256:
            states.pop(next(iter(states)))
        states[identity] = (row["state"], bypassed)
        warning = warning or bypassed
    return warning


async def skipped_confirmation(ctx: HubContext, key: dict, suspected: bool) -> str:
    """The warning for a suspected bypass, only from a daemon that does not enforce confirmation.

    Owner API 1.24 and later enforce typed-code confirmation, so from them an `armed` slot that turned `approved`
    between listings was confirmed outside the Hub (the host CLI, another Hub process, a Hub restart): no warning,
    and the stale suspicion is dropped. A daemon whose version is old or cannot be read keeps it."""
    if not suspected:
        return ""
    try:
        reply = await ctx.daemon.admin(key["token"])
    except DaemonUnreachable:
        reply = None
    if reply is not None and reply.ok and ownership_view(
            reply.body, {}, "")["compatibility"] in ("supported", "no_tailnet_signin"):
        states = ctx.browser_slot_states
        for identity, (state, _) in list(states.items()):
            if identity[0] == key["key_id"]:
                states[identity] = (state, False)
        return ""
    return "This daemon approved the claim without confirmation; update the daemon."


def consent_identity(entry: dict) -> str:
    return entry_digest({k: v for k, v in entry.items() if k not in {
        "state", "paired", "pending_pairing", "unverified_origin", "consent_digest", "slots",
        "acknowledgement_required"}})


def consent_entry(ctx: HubContext, entry: dict, key_id: str) -> dict:
    copied = ctx.consent_copies.get((key_id, entry["catalog_app_id"]))
    if copied and copied[0] == consent_identity(entry) and copied[1] > time.monotonic():
        entry["scopes"] = [{**item, **disclosure} for item, disclosure in zip(
            entry["scopes"], copied[2], strict=True)]
        entry["elevated"] = sorted(set(entry["elevated"]) | {
            item["scope"] for item in copied[2] if item["tier"] == "elevated"})
        if copied[3]:
            entry["acknowledgement_required"] = True
    entry["consent_digest"] = entry_digest(entry)
    return entry


def remember_consent(ctx: HubContext, entry: dict, key_id: str, disclosures: list[dict], *, acknowledge: bool = False):
    consent_copies = ctx.consent_copies
    if len(consent_copies) >= 256:
        consent_copies.pop(next(iter(consent_copies)))
    consent_copies[(key_id, entry["catalog_app_id"])] = (
        consent_identity(entry), time.monotonic() + 600, disclosures, acknowledge)


async def get_marketplace(request: Request):
    """Local entries (#520) with their armed slots. Listing grants nothing; nothing from an entry is fetched."""
    ctx = hub(request)
    key = ctx.claims.key()
    if key is None:
        return unclaimed("Claim this Hub on Overview before reading the Marketplace.")
    secrets = tuple(ctx.store.secrets())

    async def read_slots() -> tuple[Any, bool]:
        # Only the pairing read and what it observes hold the consent lock; the inventory read never waits on it.
        async with ctx.pairing_consent_lock:
            reply = await ctx.daemon.pairing(key["token"], "list")
            if reply.ok and isinstance(reply.body, list):
                return reply, observe_browser_slots(ctx, reply.body, key["key_id"])
            return reply, False

    # Both reads run together, but a refusal from the inventory read returns at once: the slots read can wait on
    # the consent lock for as long as another tab's arm request holds it, so it is cancelled, not awaited.
    slots_task = asyncio.ensure_future(read_slots())
    try:
        result = await read_inventory(ctx, key, lambda body: entries_view(body, secrets))
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
            return await refused(ctx, key["token"])
        if reply.ok and isinstance(reply.body, list):
            slots = marketplace_slots(reply.body, secrets)
            confirmation_warning = await skipped_confirmation(ctx, key, suspected)
        else:
            slots_error = f"Armed slots could not be read (HTTP {reply.status})."
    except DaemonUnreachable:
        slots_error = "Armed slots could not be read: the daemon did not answer."
    for entry in entries or []:
        consent_entry(ctx, entry, key["key_id"])
        entry["slots"] = slots.get(entry["catalog_app_id"], [])
    return {"state": state, "error": error, "stale": state != "ready" and good is not None,
            "reachable": reachable, "fetched_at": good["fetched_at"] if good else None, "entries": entries,
            "withheld": withheld,
            "slots_error": slots_error, "confirmation_warning": confirmation_warning,
            "daemon_url": ctx.settings.daemon_url, "now": time.time()}


async def checked_arm_slot(request: Request):
    """Arm a pre-approved slot for one entry once the owner approved its consent screen (#519, flow 4).

    The scopes and origin must be exactly what the entry declares as the daemon validates it now, so a stale page
    or a crafted body can neither widen what the owner saw nor arm a slot for an id that is not listed."""
    ctx = hub(request)
    key = ctx.claims.key()
    if key is None:
        return unclaimed("Claim this Hub on Overview before setting up an app.")
    body = await read_json(request, 4096, "Set-up", "Setting up an app requires a same-origin Origin")
    if not isinstance(body, dict) or set(body) - {"catalog_app_id", "scopes", "origin", "acknowledge_elevated",
                                                           "consent_digest"}:
        return error(400, "Unexpected set-up fields")
    catalog_app_id, scopes = body.get("catalog_app_id"), body.get("scopes")
    origin, acknowledged = body.get("origin", ""), body.get("acknowledge_elevated", False)
    if not isinstance(catalog_app_id, str) or not isinstance(scopes, list) or not all(
            isinstance(scope, str) for scope in scopes) or not isinstance(origin, str) or (
            type(acknowledged) is not bool):
        return error(400, "catalog_app_id, scopes, origin or acknowledge_elevated has the wrong type")
    try:
        reply = await ctx.daemon.inventory(key["token"])
        if reply.status == 401:
            return await refused(ctx, key["token"])
        if not reply.ok:
            detail = settings_error(reply.body, reply.status)["detail"]
            return error(409, f"The entries could not be read ({detail}). Fix them before setting up an app.")
        if not valid_inventory(reply.body):
            return error(502, "The daemon returned an invalid inventory.")
        raw_entry = next((row for row in reply.body.get("entries", [])
                          if isinstance(row, dict) and row.get("catalog_app_id") == catalog_app_id), None)
        entry = entry_view(raw_entry, guarded=tuple(ctx.store.secrets()))  # Its digest has no secrets.
        if entry is None:
            return error(404, "No local entry has that catalog app id. Refresh the Marketplace.")
        baseline = {**entry, "scopes": [dict(item) for item in entry["scopes"]]}
        consent_entry(ctx, entry, key["key_id"])
        raw_app = raw_entry["manifest"]["app"]
        declared = [item["scope"] for item in raw_app["scopes"]]
        if sorted(scopes) != sorted(declared):
            return error(409, "The entry's scopes changed since the consent screen was shown. Review it again.")
        declared_origins = raw_app.get("browser_origins") or []
        try:
            bound_origin = normalize_origin(origin) if origin else ""
            allowed = {normalize_origin(value) for value in declared_origins}
        except ValueError:
            return error(400, "Choose one of the entry's browser origins.")
        if allowed and bound_origin not in allowed:
            return error(400, "Choose one of the entry's browser origins.")
        if not allowed and origin:
            return error(400, "This entry declares no browser origin: it is set up as a native app.")
        if body.get("consent_digest") != entry_digest(entry):
            return error(409, "The entry changed since the consent screen was shown. Review it again.")
        if (entry["elevated"] or entry.get("acknowledgement_required") or any(
                not item["text"] for item in entry["scopes"])) and not acknowledged:
            return error(400, "Elevated scopes or scopes without a Hub description need the separate "
                         "acknowledgement before setup.",
                         error={"code": "acknowledgement_required"})
        arm = {"catalog_app_id": catalog_app_id, "scopes": declared,
               "name": display_text(raw_app["display_name"], 80, strip_controls=True).strip()[:60]}
        if bound_origin:
            arm["origin"] = bound_origin
        if acknowledged:
            arm["acknowledge_elevated"] = True
        reply = await ctx.daemon.pairing(key["token"], "arm", body=arm)
        if reply.status == 401:
            return await refused(ctx, key["token"])
    except DaemonUnreachable:
        return unreachable("The daemon did not answer. Refresh the Marketplace before setting up again.")
    if not reply.ok:
        refusal = settings_error(reply.body, reply.status)
        # The daemon refuses an unacknowledged arm with HTTP 400 `invalid_request` and this detail; the code
        # `acknowledgement_required` is only in its audit row (agent-harness harness/pairing_requests.py).
        if refusal["error"]["code"] == "acknowledgement_required" or (
                reply.status == 400 and "acknowledge_elevated" in refusal["detail"]):
            # A newer daemon may elevate a scope our pinned table does not yet know. It armed nothing.
            shown = [{k: item[k] for k in ("scope", "tier", "text")} for item in entry["scopes"]]
            remember_consent(ctx, baseline, key["key_id"], shown, acknowledge=True)
            return error(409, "The daemon requires a separate acknowledgement for these scopes. "
                         "Review the consent screen again.",
                         consent_entry=consent_entry(ctx, baseline, key["key_id"]))
        return JSONResponse(refusal, status_code=error_status(reply.status))
    actual = scope_disclosures(reply.body.get("disclosures") if isinstance(reply.body, dict) else None,
                               declared, tuple(ctx.store.secrets()))
    shown = [{k: item[k] for k in ("scope", "tier", "text")} for item in entry["scopes"]]
    if actual != shown or any(not item["text"].strip() for item in shown):
        # The id stays inside this server until it has been withdrawn: no hand-off on drift, including add-ons.
        unsafe = pairing_request_view(reply.body)
        if not unsafe["id"]:
            return error(502, "The daemon returned an invalid armed slot. Review Pairing requests on the host.")
        try:
            denied = await ctx.daemon.pairing(key["token"], "deny", rid=unsafe["id"])
        except DaemonUnreachable:
            return unreachable("Disclosure review failed and the slot could not be withdrawn. "
                               "Withdraw it on Pairing requests before trying again.")
        if not denied.ok or pairing_request_view(denied.body)["state"] != "denied":
            return error(502, "Disclosure review failed and the daemon refused to withdraw the slot. "
                         "Withdraw it on Pairing requests before trying again.")
        if actual is not None:
            remember_consent(ctx, baseline, key["key_id"], actual)
            reviewed = consent_entry(ctx, baseline, key["key_id"])
            return error(409, "The daemon describes these scopes differently from what you saw. "
                         "Review the consent screen again.", consent_entry=reviewed)
        return error(409, "The daemon's scope disclosures could not be displayed safely. "
                     "The slot was withdrawn. Review Pairing requests on the host.")
    slot = pairing_request_view(reply.body, tuple(ctx.store.secrets()))
    if not slot["id"] or slot["state"] != "armed":
        return error(502, "The daemon returned an invalid armed slot. Refresh the Marketplace.")
    observe_browser_slots(ctx, [slot], key["key_id"])
    return JSONResponse({"request": slot, "daemon_url": ctx.settings.daemon_url, "name": entry["name"]},
                        status_code=201)


def add_routes(app: FastAPI, ctx: HubContext) -> None:
    """Add the Marketplace routes to the app itself, in this order, as `session.add_routes` does (#190). `arm_slot`
    closes over this Hub's consent lock, as it did in `create_app`, so the lock stays readable from the route."""
    pairing_consent_lock = ctx.pairing_consent_lock

    async def arm_slot(request: Request):
        async with pairing_consent_lock:
            return await checked_arm_slot(request)

    app.add_api_route("/api/marketplace", get_marketplace, methods=["GET"])
    app.add_api_route("/api/marketplace/arm", arm_slot, methods=["POST"])
