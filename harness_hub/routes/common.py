"""What every router shares: the Hub's state as one explicit `HubContext`, and the refusals a route answers with."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from types import ModuleType
from typing import Any

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from ..auth import Sessions
from ..claim import ClaimManager
from ..config import Settings
from ..daemon import DaemonClient, DaemonUnreachable
from ..guard import leaks
from ..inventory import InventoryCache, valid_inventory
from ..state import StateStore
from ..tailnet import PeerCheck
from ..views import apps_view, entries_view, module_view, settings_error


@dataclass
class HubContext:
    """The state `create_app` builds once and its routes share, on `app.state.hub`, in place of closure variables."""

    settings: Settings
    store: StateStore
    daemon: DaemonClient
    claims: ClaimManager
    sessions: Sessions
    inventory: InventoryCache
    peer_check: PeerCheck
    # The daemon's last allowed_logins answer, kept briefly: {"at", "until", "answer"}, on the sessions' monotonic
    # clock so the outage grace counts from the read that said yes, not from a request that reused it (#177).
    owner_logins: dict[str, Any] = field(default_factory=dict)
    owner_logins_read: asyncio.Lock = field(default_factory=asyncio.Lock)  # one read at a time; waiters take its answer
    # Listings must not publish a new slot while its disclosures are being checked or it is being withdrawn.
    pairing_consent_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # A bounded, process-local backstop for an observed browser claim that skipped the confirmation gate.
    # Share observations across pages/tabs; record successful Hub confirmations under the same lock as polls.
    browser_slot_states: dict[tuple[str, str], tuple[str, bool]] = field(default_factory=dict)
    # Bound to the Hub key and the entry's displayed identity, with a bounded ten-minute lifetime.
    consent_copies: dict[tuple[str, str], tuple[str, float, list[dict], bool]] = field(default_factory=dict)


def hub(request: Request) -> HubContext:
    """The `HubContext` of the app serving this request: how a router's handlers reach the Hub's state."""
    return request.app.state.hub


def tuning() -> ModuleType:
    """`harness_hub.server`, home of the timing constants: read per use, so a value tests set there applies."""
    from .. import server

    return server


def json_body(data: bytes | bytearray) -> Any:
    """A request body parsed as JSON. Raises ValueError for any body that does not parse, deeply nested arrays included
    (json raises RecursionError for those, which a route's `except ValueError` would miss and answer with a 500)."""
    try:
        return json.loads(data)
    except RecursionError as exc:
        raise ValueError("JSON body is nested too deeply") from exc


def error(status: int, detail: str, **extra: Any) -> JSONResponse:
    return JSONResponse({"detail": detail, **extra}, status_code=status)


async def read_json(request: Request, limit: int, label: str, origin: str, not_json: str = "") -> Any:
    """A write's JSON body of at most `limit` bytes, {} when empty. Raises the refusal as an HTTPException, answered
    as {"detail": ...} just as `error` answers: 403 with no Origin, 413 over the limit, 400 when it does not parse."""
    if not request.headers.get("origin"):
        raise HTTPException(403, origin)
    data = bytearray()
    async for chunk in request.stream():
        if len(data) + len(chunk) > limit:
            raise HTTPException(413, f"{label} body is too large")
        data.extend(chunk)
    try:
        return json_body(data or b"{}")
    except ValueError:
        raise HTTPException(400, not_json or f"{label} body must be JSON") from None


# An error says whether the daemon answered, so the shell's status tells "the Hub could not reach the daemon" (a 503 of
# the Hub's own, reachable false) from "the daemon answered with an error" (forwarded, any status, reachable true).
def unreachable(detail: str, **extra: Any) -> JSONResponse:
    return error(503, detail, reachable=False, **extra)


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


# A read or write the Hub refuses because it has not claimed the daemon. claimed false lets a page show its Claim this
# Hub first state, with a link to Overview, instead of the refusal's sentence.
def unclaimed(detail: str) -> JSONResponse:
    return error(409, detail, claimed=False)


async def retained(ctx: HubContext, token: str) -> str:
    """The daemon answered 401: drop the key only if a second read confirms it, else say it was kept.

    A transient rejection must not destroy the owner's persistent key."""
    confirmation = await ctx.daemon.admin(token)
    if confirmation.status == 401:
        await ctx.claims.released(token)
        return ""
    return "the owner API rejected the key once; retained it pending confirmation"


async def refused(ctx: HubContext, token: str) -> JSONResponse:
    """The daemon answered 401: drop the key only if a second read confirms it, then point to Overview."""
    await retained(ctx, token)
    return error(409, "The daemon refused this Hub's key. Check Overview before trying again.")


def cacheable(ctx: HubContext, body: Any, key_id: str) -> bool:
    """Whether no page that reads the shared cache would show a credential-like string from this snapshot."""
    secrets = tuple(ctx.store.secrets())
    views = (module_view(body), apps_view(body, key_id, secrets), entries_view(body, secrets))
    return not leaks(json.dumps(views).encode(), ctx.store.secrets())


async def read_inventory(ctx: HubContext, key: dict, shown) -> tuple[dict | None, str, str, bool] | JSONResponse:
    """The last good inventory (refreshed when the daemon answers with a valid one), its state, any error, and
    whether the daemon answered at all (the shell's status reads that instead of the error text).

    `shown` projects an inventory into the asking page's browser view; a snapshot whose view would leak is not
    shown. Modules, Apps and Marketplace share the cache, so a snapshot that would leak on any of them is never
    cached either: the page whose own view is clean shows it once, and the earlier last-good stays."""
    good = ctx.inventory.get(key["key_id"])
    state, detail, reachable = "ready", "", True
    try:
        reply = await ctx.daemon.inventory(key["token"])
        if reply.status == 401:
            return await refused(ctx, key["token"])
        if reply.ok:
            if not valid_inventory(reply.body):
                state, detail = "error", "The daemon returned an invalid inventory."
            elif leaks(json.dumps(shown(reply.body)).encode(), ctx.store.secrets()):
                state, detail = "error", "Inventory withheld: it contained a credential-like string."
            elif cacheable(ctx, reply.body, key["key_id"]):
                good = ctx.inventory.save(reply.body, key["key_id"])
            else:
                good = {"inventory": reply.body, "fetched_at": time.time()}
        elif reply.status == 404:
            state, detail = "absent", "The Hub inventory module is not installed."
        elif reply.status == 400 and "disabled" in reply.detail.lower():
            state, detail = "disabled", "Hub inventory is disabled. Switch it on below."
        else:
            # A stale or unreadable entries file is a named 400 (`hub.entries: invalid JSON`, `entries[0]...`).
            state, detail = "error", settings_error(reply.body, reply.status)["detail"]
    except DaemonUnreachable:
        state, detail, reachable = "error", "The daemon did not answer.", False
    except (OSError, ValueError):
        state, detail = "error", "The inventory could not be saved in the private state directory."
    return good, state, detail, reachable
