"""The Overview router: Overview and the inventory listing (`/api/apps`, `/api/modules`), with the Hub's state on
`HubContext` (#190). Spend joins it next."""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from ..daemon import DaemonUnreachable
from ..views import apps_view, health_view, module_view, overview, ownership_view, pairing_attention
from .common import concurrently, hub, read_inventory, retained, settled, tuning, unclaimed
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


def add_routes(app: FastAPI) -> None:
    """Add the Overview and inventory routes to the app itself, in this order, as `session.add_routes` does
    (#190). `/api/modules` comes before `/api/apps`, as the stacked decorators registered them."""
    app.add_api_route("/api/overview", get_overview, methods=["GET"])
    app.add_api_route("/api/modules", get_modules, methods=["GET"])
    app.add_api_route("/api/apps", get_modules, methods=["GET"])
