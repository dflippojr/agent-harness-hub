"""The Hub server's calls to its daemon. Only this module and `claim.py` talk to the daemon.

The Hub calls the public `/health`, the Hub-claim pairing routes for its *own* claim, and the owner API with its
`hub` key. It never claims or redeems an App's pairing request: App tokens go from the daemon straight to the App
(agent-harness #519), so the only token response the Hub ever reads is its own Hub key's.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

ADMIN = "/api/admin/v1"
CLAIM_REQUESTS = "/api/v1/pair/requests"
TIMEOUT = httpx.Timeout(10.0, connect=5.0)


class DaemonUnreachable(Exception):
    """The daemon did not answer (stopped, restarting, or the URL is wrong)."""


@dataclass
class Reply:
    status: int
    body: Any

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    @property
    def code(self) -> str:
        """The daemon's machine-readable error code (`error.code`), or ''."""
        if isinstance(self.body, dict) and isinstance(self.body.get("error"), dict):
            return str(self.body["error"].get("code") or "")
        return ""

    @property
    def detail(self) -> str:
        if isinstance(self.body, dict) and isinstance(self.body.get("detail"), str):
            return self.body["detail"]
        return ""


class DaemonClient:
    def __init__(self, base_url: str, transport: httpx.AsyncBaseTransport | None = None):
        self.base_url = base_url
        # No Origin header is ever sent: the Hub claims as a native client, so its key is not bound to a browser.
        self._client = httpx.AsyncClient(base_url=base_url, timeout=TIMEOUT, transport=transport,
                                         follow_redirects=False, headers={"Accept": "application/json"})

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _call(self, method: str, path: str, *, token: str = "", json: Any = None) -> Reply:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        try:
            response = await self._client.request(method, path, json=json, headers=headers)
        except httpx.HTTPError as exc:
            raise DaemonUnreachable(type(exc).__name__) from None
        try:
            body = response.json()
        except ValueError:
            body = None
        return Reply(response.status_code, body)

    async def health(self) -> Reply:
        return await self._call("GET", "/health")

    async def start_hub_claim(self, name: str, challenge: str) -> Reply:
        return await self._call("POST", CLAIM_REQUESTS, json={
            "name": name, "kind": "hub", "code_challenge": challenge, "code_challenge_method": "S256"})

    async def redeem_hub_claim(self, request_id: str, verifier: str) -> Reply:
        """Redeem this Hub's own claim. The caller (`claim.py`) checks that the key it gets has role `hub`."""
        return await self._call("POST", f"{CLAIM_REQUESTS}/{request_id}/token", json={"code_verifier": verifier})

    async def admin(self, token: str, path: str = "") -> Reply:
        """A read on the owner API with the Hub key."""
        return await self._call("GET", ADMIN + path, token=token)
