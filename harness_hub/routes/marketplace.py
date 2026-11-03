"""The Marketplace listing, and what the Marketplace, arming and the pairing listings share: the consent screen's
copies and the backstop for a browser claim that skipped confirmation, with their state on `HubContext`. Arming moves
here next (#190)."""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from ..daemon import DaemonUnreachable
from ..views import entries_view, entry_digest, marketplace_slots, ownership_view, pairing_request_view
from .common import HubContext, concurrently, hub, read_inventory, refused, settled, unclaimed


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


def add_routes(app: FastAPI) -> None:
    """Add the Marketplace listing to the app itself, as `session.add_routes` does (#190)."""
    app.add_api_route("/api/marketplace", get_marketplace, methods=["GET"])
