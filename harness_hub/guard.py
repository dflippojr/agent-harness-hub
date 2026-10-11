"""Last line of defence: no Hub API response reaches a browser carrying a credential.

Browser responses are built from allowlisted fields (`views.py`), so nothing secret should get this far. This guard
checks every `/api/` response body anyway and withholds it (500) when it contains the Hub key or the pending claim's
verifier, or a string shaped like an Agent Harness token (`ha-`, `ho-` or `hp-` followed by token characters).
Withholding fails closed: a page shows an error, never a secret.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterable

from starlette.types import ASGIApp, Message, Receive, Scope, Send

log = logging.getLogger("harness_hub.guard")

# The daemon's App, owner and pairing-code secrets and the key list's display prefixes (e.g. `ho-LW5TqUX`).
TOKEN_SHAPE = re.compile(r"(?<![A-Za-z0-9])(?:ha|ho|hp)-[A-Za-z0-9_\-]{4,}")
WITHHELD = b'{"detail":"response withheld: it contained a credential-like string"}'


def leaks(body: bytes, secrets: Iterable[str]) -> bool:
    text = body.decode("utf-8", errors="replace")
    if TOKEN_SHAPE.search(text):
        return True
    return any(secret and secret in text for secret in secrets)


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
