"""The Overview router: Overview, the inventory listing (`/api/apps`, `/api/modules`) and Spend, with the Hub's state on
`HubContext` (#190)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from ..daemon import DaemonUnreachable
from ..guard import leaks
from ..inventory import valid_inventory
from ..views import (
    ID_SEGMENT,
    apps_view,
    health_view,
    module_view,
    overview,
    ownership_view,
    pairing_attention,
    settings_error,
    spend_view,
)
from .common import HubContext, concurrently, error, hub, read_inventory, refused, retained, settled, tuning, unclaimed
from .marketplace import observe_browser_slots
from .settings import observe_recovery, observe_restart


async def get_overview(request: Request):
    ctx = hub(request)
    health, ownership, attention, error, answered = None, None, None, "", False
    key = ctx.claims.key()

    async def read_pairing() -> Any:
        # Under the consent lock, as the other listings, so no count is published during a withdrawal.
        async with ctx.pairing_consent_lock:
            reply = await ctx.daemon.pairing(key["token"], "list")
            if reply.ok and isinstance(reply.body, list):
                observe_browser_slots(ctx, reply.body, key["key_id"])
            return reply

    # The pairing read can wait on the consent lock for as long as an arm request holds it, so it runs beside the
    # other reads and is awaited only when all of them succeed, and then only until its deadline; a refused or
    # failed read, or the deadline, cancels it instead.
    pairing_task = asyncio.ensure_future(read_pairing()) if key else None
    deadline = asyncio.get_running_loop().time() + tuning().PAIRING_COUNT_SECONDS
    health_generation = ctx.restart["generation"]
    try:
        reads = await concurrently(ctx.daemon.health(), *(
            (ctx.daemon.admin(key["token"]), ctx.daemon.admin(key["token"], "/hub-claim")) if key else ()))
        reply = settled(reads[0])
        answered = True
        if reply.ok:
            health = health_view(reply.body)
        else:
            error = f"the daemon's /health answered HTTP {reply.status}"
        if key:
            discovery, record = settled(reads[1]), settled(reads[2])
            if 401 in (discovery.status, record.status):
                error = await retained(ctx, key["token"]) or error
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
                        refusal = await retained(ctx, key["token"])
                        error, ownership = (refusal, ownership) if refusal else (error, None)
                    elif pairing is not None and pairing.ok:
                        attention = pairing_attention(pairing.body, time.time())
    except DaemonUnreachable:
        # As after a discovery refusal, ownership the daemon refused once is not shown when it cannot confirm it.
        ownership = None
        error = f"the daemon at {ctx.settings.daemon_url} did not answer"
    finally:
        if pairing_task:
            pairing_task.cancel()
            await concurrently(pairing_task)
    result = overview(ctx.claims, ctx.settings.daemon_url, health, ownership, error)
    if health:
        observe_recovery(ctx, health["health"]["recovery"])
    observe_restart(ctx, health["health"] if health else None, health_generation)
    result["answered"] = answered
    result["restarting"] = ctx.restart["active"]
    result["attention"] = attention
    return result


async def get_modules(request: Request):
    ctx = hub(request)
    key = ctx.claims.key()
    if key is None:
        return unclaimed("Claim this Hub on Overview before reading inventory.")
    apps_page = request.url.path == "/api/apps"
    result = await read_inventory(ctx, key, (lambda body: apps_view(body, key["key_id"], tuple(ctx.store.secrets())))
                                  if apps_page else module_view)
    if isinstance(result, JSONResponse):
        return result
    good, state, error, reachable = result
    if apps_page:
        shown = apps_view(good["inventory"], key["key_id"], tuple(ctx.store.secrets())) if good else {
            "apps": None, "entries": []}
    else:
        shown = {"modules": module_view(good["inventory"]) if good else None}
    return {"state": state, "error": error, "stale": state != "ready" and good is not None,
            "reachable": reachable, "fetched_at": good["fetched_at"] if good else None, **shown}


async def refused_spend(ctx: HubContext, token: str) -> JSONResponse:
    """A 401 during a Spend read; the refusal's own confirmation read may also go unanswered."""
    try:
        return await refused(ctx, token)
    except DaemonUnreachable:
        return error(503, "The daemon did not answer. Spend is unavailable until it does.")


async def get_spend(request: Request):
    """Read-only: each App's usage and session-cap use, and each backend's windows. It writes nothing, not even
    the inventory cache, so its poll adds no disk writes; a failed read falls back to the cached inventory."""
    ctx = hub(request)
    key = ctx.claims.key()
    if key is None:
        return unclaimed("Claim this Hub on Overview before reading spend.")
    secrets = tuple(ctx.store.secrets())
    good = ctx.inventory.get(key["key_id"])
    body, read_error, fetched_at = (good["inventory"], "", good["fetched_at"]) if good else (None, "", None)
    # Each read stands alone: one that fails or times out never hides what another one returned.
    reply, backends = await concurrently(ctx.daemon.inventory(key["token"]),
                                         ctx.daemon.settings(key["token"], "backends"))
    for result in (reply, backends):
        if isinstance(result, BaseException) and not isinstance(result, DaemonUnreachable):
            raise result
    if 401 in (getattr(reply, "status", None), getattr(backends, "status", None)):
        return await refused_spend(ctx, key["token"])
    if isinstance(reply, DaemonUnreachable):
        read_error = "The daemon did not answer."
    elif reply.ok and valid_inventory(reply.body):
        body, fetched_at = reply.body, time.time()
    else:
        read_error = "The daemon returned an invalid inventory." if reply.ok else settings_error(
            reply.body, reply.status)["detail"]
    live = [row["id"] for row in (body or {}).get("apps", []) if isinstance(row, dict)
            and row.get("kind") == "app" and row.get("revoked_at") is None and row.get("id") != key["key_id"]
            and isinstance(row.get("id"), str) and ID_SEGMENT.fullmatch(row["id"])]
    gate = asyncio.Semaphore(tuning().SPEND_CAP_READS)

    async def read_caps(app_id: str):
        async with gate:
            return await ctx.daemon.apps(key["token"], "limits", app_id)

    replies = await concurrently(*(read_caps(app_id) for app_id in live))
    for result in replies:
        if isinstance(result, BaseException) and not isinstance(result, DaemonUnreachable):
            raise result
    if any(getattr(result, "status", None) == 401 for result in replies):
        return await refused_spend(ctx, key["token"])
    limits = {app_id: result.body for app_id, result in zip(live, replies, strict=True)
              if getattr(result, "ok", False)}
    backend_rows = backends.body if getattr(backends, "ok", False) else None
    shown = spend_view(body, backend_rows, limits, key["key_id"], secrets)
    if body is None:
        shown["apps"] = None
    if leaks(json.dumps(shown).encode(), ctx.store.secrets()):
        return error(502, "Spend withheld: it contained a credential-like string.")
    return {"error": read_error, "stale": bool(read_error) and body is not None, "fetched_at": fetched_at,
            # Any read the daemon answered shows it is up; the shell's status reads this, not the error text.
            "reachable": not all(isinstance(result, DaemonUnreachable) for result in (reply, backends)),
            "backends_error": "" if backend_rows is not None else "The daemon's backend usage was unavailable.",
            **shown}


def add_routes(app: FastAPI) -> None:
    """Add the Overview, inventory and Spend routes to the app itself, in this order, as `session.add_routes` does
    (#190). `/api/modules` comes before `/api/apps`, as the stacked decorators registered them."""
    app.add_api_route("/api/overview", get_overview, methods=["GET"])
    app.add_api_route("/api/modules", get_modules, methods=["GET"])
    app.add_api_route("/api/apps", get_modules, methods=["GET"])
    app.add_api_route("/api/spend", get_spend, methods=["GET"])
