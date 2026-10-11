"""The Hub's exclusive claim on its daemon (agent-harness #543), held entirely on the Hub's server.

1. Start: make a PKCE pair, `POST /api/v1/pair/requests` with `kind: "hub"`. The daemon answers a request id and a
   six-digit match code; a daemon that already has a Hub answers 409 `hub_claimed`.
2. The owner approves on the daemon host: `harness hub approve <id> --match <code>`. The Hub shows that command.
3. The Hub polls the redeem route with its verifier; once approved it receives its `hub` key, exactly once.

The verifier and the key are written only to the owner-only state file. With `--claim-file` (the daemon installer)
the Hub also publishes `{"id", "match_code"}` there, atomically, and nothing else.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import logging
import re
import secrets
import time
from pathlib import Path

from .daemon import DaemonClient, DaemonUnreachable
from .state import StateStore, write_private

log = logging.getLogger("harness_hub.claim")

REQUEST_ID = re.compile(r"pr-[A-Za-z0-9_-]{1,64}")
MATCH_CODE = re.compile(r"[0-9]{6}")
APPROVE_COMMAND = "harness hub approve {id} --match {match}"
RELEASE_COMMAND = "harness hub release --confirm"

# What the UI is told for each state; the server adds the commands.
UNCLAIMED, PENDING, CLAIMED = "unclaimed", "pending", "claimed"
PERSISTING = "persisting"
REFUSED, EXPIRED, DENIED, RELEASED, RATE_LIMITED, FAILED = (
    "refused", "expired", "denied", "released", "rate_limited", "failed")
OUTCOMES = (REFUSED, EXPIRED, DENIED, RELEASED, RATE_LIMITED, FAILED)


class ClaimError(Exception):
    """A claim could not start; `state` describes the refusal or the existing claim."""

    def __init__(self, state: str, message: str):
        super().__init__(message)
        self.state = state


def pkce_pair() -> tuple[str, str]:
    """(verifier, S256 challenge), RFC 7636."""
    verifier = secrets.token_urlsafe(64)[:96]
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


class ClaimManager:
    def __init__(self, store: StateStore, daemon: DaemonClient, *, name: str = "Hub",
                 claim_file: Path | None = None, poll_seconds: float = 2.0, clock=time.time):
        self.store = store
        self.daemon = daemon
        self.name = name
        self.claim_file = claim_file
        self.poll_seconds = poll_seconds
        self.clock = clock
        self._lock = asyncio.Lock()
        self._poller: asyncio.Task | None = None
        self._redeemed_key: dict | None = None
        self._stopping = asyncio.Event()
        self._wake = asyncio.Event()

    # -- state ------------------------------------------------------------------------------------------------

    def key(self) -> dict | None:
        key = self.store.get("key")
        if key and key.get("daemon_url") == self.daemon.base_url and key.get("token"):
            return key
        return None

    def token(self) -> str:
        key = self.key()
        return key["token"] if key else ""

    def pending(self) -> dict | None:
        claim = self.store.get("claim")
        if claim and claim.get("daemon_url") == self.daemon.base_url:
            return claim
        return None

    def state(self) -> str:
        if self._redeemed_key is not None:
            return PERSISTING
        if self.key():
            return CLAIMED
        if self.pending():
            return PENDING
        outcome = self.store.get("outcome")
        if outcome and outcome.get("state") in OUTCOMES:
            return outcome["state"]
        return UNCLAIMED

    def secrets(self) -> list[str]:
        """Include the private candidate in the response guard until it is durable."""
        values = self.store.secrets()
        if self._redeemed_key is not None:
            values.append(self._redeemed_key["token"])
        return values

    def _finish(self, state: str) -> None:
        self.store.update(claim=None, outcome={"state": state, "at": self.clock()})
        self._remove_claim_file()

    # -- start ------------------------------------------------------------------------------------------------

    async def start(self) -> dict:
        """Start a claim, or return the one already waiting. Raises ClaimError when the daemon refuses."""
        async with self._lock:
            if self._redeemed_key is not None:
                self.ensure_polling()
                raise ClaimError(PERSISTING, "the redeemed Hub key is waiting for private state storage to recover")
            if self.key():
                raise ClaimError(CLAIMED, "this Hub already holds the daemon's Hub key")
            pending = self.pending()
            if pending and pending["expires_at"] > self.clock():
                self.ensure_polling()
                return pending
            verifier, challenge = pkce_pair()
            try:
                reply = await self.daemon.start_hub_claim(self.name, challenge)
            except DaemonUnreachable:
                self._finish(FAILED)
                raise ClaimError(FAILED, "the daemon did not answer; check that it is running") from None
            if reply.status == 409 and reply.code == "hub_claimed":
                self._finish(REFUSED)
                raise ClaimError(REFUSED, "this daemon already has a Hub")
            if reply.status == 429:
                self._finish(RATE_LIMITED)
                raise ClaimError(RATE_LIMITED, "the daemon has too many pairing requests waiting; try again later")
            body = reply.body if isinstance(reply.body, dict) else {}
            rid, match = str(body.get("id", "")), str(body.get("match_code", ""))
            if reply.status != 201 or not REQUEST_ID.fullmatch(rid) or not MATCH_CODE.fullmatch(match):
                self._finish(FAILED)
                raise ClaimError(FAILED, f"the daemon refused the claim (HTTP {reply.status})")
            now = self.clock()
            expires = body.get("expires_at")
            claim = {"request_id": rid, "verifier": verifier, "match_code": match, "started_at": now,
                     "expires_at": float(expires) if isinstance(expires, int | float) else now + 600,
                     "daemon_url": self.daemon.base_url}
            self.store.update(claim=claim, outcome=None)
            self._publish_claim_file(rid, match)
            log.info("Hub claim %s started; approve it on the daemon host", rid)
            self.ensure_polling()
            return claim

    async def forget(self) -> None:
        """Order cancellation after an in-flight redemption, preserving any minted key."""
        async with self._lock:
            if self._redeemed_key is not None:
                self.ensure_polling()
                return
            if self.key():
                return
            self.store.update(claim=None, outcome=None)
            self._remove_claim_file()
            self._wake.set()

    def released(self) -> None:
        """The daemon no longer accepts this Hub's key (`harness hub release` on the host)."""
        self.store.update(key=None, claim=None, outcome={"state": RELEASED, "at": self.clock()})

    # -- redeem -----------------------------------------------------------------------------------------------

    def ensure_polling(self) -> None:
        if not self._stopping.is_set():
            if self._poller is None or self._poller.done():
                self._poller = asyncio.get_running_loop().create_task(self._poll())
            else:
                self._wake.set()

    async def stop(self) -> None:
        # Do not cancel a request that may already have minted a one-time credential.
        # DaemonClient bounds the network wait; wake idle pollers immediately.
        self._stopping.set()
        self._wake.set()
        if self._poller is not None:
            await self._poller
        async with self._lock:
            if self._redeemed_key is not None and not self._persist_key():
                log.error("Hub key could not be saved before shutdown; release the Hub on the daemon host "
                          "and claim again after repairing private state storage")

    async def _poll(self) -> None:
        while not self._stopping.is_set():
            self._wake.clear()
            try:
                done = await self.poll_once()
            except Exception:  # never let the poller die silently
                log.exception("Hub claim poll failed")
                done = False
            if done:
                return
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=self.poll_seconds)

    async def poll_once(self) -> bool:
        """One redeem attempt. True when the claim is finished (key stored, or refused, expired or denied)."""
        async with self._lock:
            if self._redeemed_key is not None:
                return self._persist_key()
            return await self._redeem()

    async def _redeem(self) -> bool:
        """Hold the claim lock across the request and every response transition."""
        claim = self.pending()
        if claim is None:
            return True
        if not REQUEST_ID.fullmatch(claim["request_id"]):
            self._finish(FAILED)
            return True
        try:
            reply = await self.daemon.redeem_hub_claim(claim["request_id"], claim["verifier"])
        except DaemonUnreachable:
            if self.clock() > claim["expires_at"] + 300:  # the approve and redeem windows have both passed
                self._finish(EXPIRED)
                return True
            return False
        if reply.status == 409 and reply.code == "pairing_pending":
            if self.clock() > claim["expires_at"] + 300:
                self._finish(EXPIRED)
                return True
            return False
        if reply.status == 201:
            return self._store_key(reply.body, claim)
        if reply.status == 409 and reply.code == "hub_claimed":
            self._finish(REFUSED)
        elif reply.status == 403 and "denied" in reply.detail:
            self._finish(DENIED)
        elif reply.status in (400, 404) and ("expired" in reply.detail or reply.status == 404):
            self._finish(EXPIRED)
        else:
            log.warning("Hub claim %s ended: HTTP %s", claim["request_id"], reply.status)
            self._finish(FAILED)
        return True

    def _store_key(self, body, claim: dict) -> bool:
        token = body.get("token") if isinstance(body, dict) else None
        app = body.get("app") if isinstance(body, dict) else None
        if (not isinstance(token, str) or not token.startswith("ho-") or not isinstance(app, dict)
                or app.get("role") != "hub" or not isinstance(app.get("id"), str)):
            # Not a Hub key: never keep or show it.
            log.error("Hub claim %s redeemed something that is not a Hub key; discarded", claim["request_id"])
            self._finish(FAILED)
            return True
        self._redeemed_key = {"token": token, "key_id": app["id"], "daemon_url": self.daemon.base_url,
                              "claimed_at": self.clock(), "request_id": claim["request_id"]}
        return self._persist_key()

    def _persist_key(self) -> bool:
        """Retry only the local write: the daemon returns this credential exactly once."""
        key = self._redeemed_key
        try:
            self.store.update(key=key, claim=None, outcome=None)
        except OSError:
            # Exception text can contain secrets supplied by storage or the daemon.
            log.error("Redeemed Hub key is waiting for private state storage; saving will be retried. "
                      "Repair storage before stopping the Hub")
            return False
        self._redeemed_key = None
        self._remove_claim_file()
        log.info("Hub claim saved: this Hub is the daemon's Hub")
        return True

    # -- installer claim file ---------------------------------------------------------------------------------

    def _publish_claim_file(self, rid: str, match: str) -> None:
        if self.claim_file is not None:
            write_private(self.claim_file, json.dumps({"id": rid, "match_code": match}))

    def _remove_claim_file(self) -> None:
        if self.claim_file is not None:
            with contextlib.suppress(OSError):
                self.claim_file.unlink()

    def approve_command(self, claim: dict) -> str:
        return APPROVE_COMMAND.format(id=claim["request_id"], match=claim["match_code"])
