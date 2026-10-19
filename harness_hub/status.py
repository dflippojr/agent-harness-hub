"""The Hub's heartbeat: a small owner-only file a read-only sweep can fetch (docs/production-signals.md).

With `--status-interval`, the Hub asks its daemon's public `/health` every interval and rewrites `status.json` in the
state directory with typed values only: version, build, start time, claim state and when the daemon last answered.
Nothing here is a credential, and nothing is served over HTTP, so a tailnet peer sees none of it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from datetime import UTC, datetime
from pathlib import Path

from . import __version__
from .daemon import DaemonClient, DaemonUnreachable
from .state import write_private

STATUS_FILE = "status.json"
UNKNOWN_BUILD = "unknown"
log = logging.getLogger("harness_hub.status")


def build_commit(value: str | None) -> str:
    """The commit the deploy script recorded in HUB_BUILD_COMMIT, or `unknown` for anything that is not a SHA."""
    value = (value or "").strip().lower()
    return value if re.fullmatch(r"[0-9a-f]{40}", value) else UNKNOWN_BUILD


def _stamp(moment: float) -> str:
    return datetime.fromtimestamp(moment, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class StatusReporter:
    def __init__(self, state_dir: Path, daemon: DaemonClient, claim_state, *, interval: float, build: str,
                 clock=time.time):
        self.path = state_dir / STATUS_FILE
        self.daemon = daemon
        self.claim_state = claim_state
        self.interval = interval
        self.build = build
        self.clock = clock
        self.started_at = clock()
        self.daemon_last_ok: float | None = None
        self._task: asyncio.Task | None = None

    async def beat(self) -> dict:
        """Poll the daemon once and rewrite the file. A daemon that does not answer is recorded, never raised."""
        try:
            reply = await self.daemon.health()
            answered = reply.ok and isinstance(reply.body, dict) and reply.body.get("ok") is True
        except DaemonUnreachable:
            answered = False
        now = self.clock()
        if answered:
            self.daemon_last_ok = now
        status = {
            "version": __version__,
            "build": self.build,
            "started_at": _stamp(self.started_at),
            "written_at": _stamp(now),
            "claim": self.claim_state(),
            "daemon_ok": answered,
            "daemon_last_ok_at": _stamp(self.daemon_last_ok) if self.daemon_last_ok is not None else None,
        }
        write_private(self.path, json.dumps(status) + "\n")
        return status

    async def _run(self) -> None:
        while True:
            try:
                await self.beat()
            except Exception:  # never let the heartbeat die silently; a stale written_at is the sweep's signal
                log.exception("Hub status write failed")
            await asyncio.sleep(self.interval)

    def start(self) -> None:
        if self.interval > 0 and self._task is None:
            self._task = asyncio.get_running_loop().create_task(self._run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
