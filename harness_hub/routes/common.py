"""What every router shares: the Hub's state as one explicit `HubContext`, and the refusals a route answers with."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from types import ModuleType
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse

from ..auth import Sessions
from ..claim import ClaimManager
from ..config import Settings
from ..daemon import DaemonClient
from ..inventory import InventoryCache
from ..state import StateStore
from ..tailnet import PeerCheck


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
    except RecursionError as error:
        raise ValueError("JSON body is nested too deeply") from error


def error(status: int, detail: str, **extra: Any) -> JSONResponse:
    return JSONResponse({"detail": detail, **extra}, status_code=status)


# An error says whether the daemon answered, so the shell's status tells "the Hub could not reach the daemon" (a 503 of
# the Hub's own, reachable false) from "the daemon answered with an error" (forwarded, any status, reachable true).
def unreachable(detail: str, **extra: Any) -> JSONResponse:
    return error(503, detail, reachable=False, **extra)
