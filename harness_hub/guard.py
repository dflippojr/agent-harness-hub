"""Last line of defence: no Hub API response reaches a browser carrying a credential.

Browser responses are built from allowlisted fields (`views.py`), so nothing secret should get this far. This guard
checks every `/api/` response body anyway and withholds it (500) when it contains the Hub key or the pending claim's
verifier, or a string shaped like a full Agent Harness token. Only full-length shapes count: ordinary words such as
`ha-core` or `hp-laserjet` pass. Shorter key display prefixes (`ho-LW5TqUX`) are redacted field by field in `views.py`.
Withholding fails closed: a page shows an error, never a secret.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable, Iterable

from starlette.types import ASGIApp, Message, Receive, Scope, Send

log = logging.getLogger("harness_hub.guard")

# The daemon's secrets (agent-harness `harness/catalog_ids.py` TOKEN_PREFIXES, minted in `harness/db.py`): App, owner
# and other keys are the prefix plus `secrets.token_urlsafe(32)` (43 characters), pairing codes plus
# `token_urlsafe(18)` (24). A longer run counts whole, so a token followed by more token characters is not missed.
TOKEN_SHAPE = re.compile(r"(?<![A-Za-z0-9])(?:(?:ha|ho|hk)-[A-Za-z0-9_\-]{43,}|(?:hp|hrp)-[A-Za-z0-9_\-]{24,})")
# A key list's display prefix (`ho-LW5TqUX`, the token's first 10 characters) or anything shaped like one. Views redact
# it in fields where a key id or daemon diagnostic can appear; the guard does not, because ordinary words match it.
KEY_PREFIX_SHAPE = re.compile(r"(?<![A-Za-z0-9])(?:ha|ho|hk|hp|hrp)-[A-Za-z0-9_\-]{4,}")
WITHHELD = b'{"detail":"response withheld: it contained a credential-like string"}'


def _decoded(text: str) -> list[str]:
    """Every string in a JSON body, as the browser will read it. In the raw body a token after a newline follows the
    `n` of the JSON escape, and the shape's look-behind refuses it."""
    try:
        pending = [json.loads(text)]
    except (ValueError, RecursionError):
        return []
    out = []
    while pending:
        value = pending.pop()
        if isinstance(value, str):
            out.append(value)
        elif isinstance(value, dict):
            out.extend(value)
            pending.extend(value.values())
        elif isinstance(value, list):
            pending.extend(value)
    return out


def leaks(body: bytes, secrets: Iterable[str]) -> bool:
    text = body.decode("utf-8", errors="replace")
    known = [secret for secret in secrets if secret]
    return any(TOKEN_SHAPE.search(value) or any(secret in value for secret in known)
               for value in (text, *_decoded(text)))


class ResponseGuard:
    """ASGI middleware: buffer each `/api/` response and withhold it when it leaks."""

    def __init__(self, app: ASGIApp, secrets: Callable[[], list[str]]):
        self.app = app
        self.secrets = secrets

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope["path"].startswith("/api/"):
            await self.app(scope, receive, send)
            return
        start: Message | None = None
        chunks: list[bytes] = []

        async def capture(message: Message) -> None:
            nonlocal start
            if message["type"] == "http.response.start":
                start = message
            elif message["type"] == "http.response.body":
                chunks.append(message.get("body", b""))
                if not message.get("more_body", False):
                    await flush()

        async def flush() -> None:
            assert start is not None
            body = b"".join(chunks)
            if leaks(body, self.secrets()):
                log.error("withheld a %s response on %s: it contained a credential-like string",
                          start["status"], scope["path"])
                headers = [(k, v) for k, v in start["headers"] if k.lower() not in (b"content-length", b"content-type")]
                headers += [(b"content-type", b"application/json"), (b"content-length", str(len(WITHHELD)).encode())]
                await send({**start, "status": 500, "headers": headers})
                await send({"type": "http.response.body", "body": WITHHELD})
                return
            await send(start)
            await send({"type": "http.response.body", "body": body})

        await self.app(scope, receive, capture)
