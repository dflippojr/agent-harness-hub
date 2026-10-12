"""Signing a browser in to the Hub.

The Hub holds an admin-equivalent key, so nobody uses it without proving they are the owner. The proof is the one the
daemon uses for local callers: the owner can read a file that only the owner's account can read. On the daemon host,
`python -m harness_hub signin-link` writes a one-time code (stored only as a hash) into the Hub's owner-only state
directory and prints a sign-in link carrying the code in its fragment, which the browser never sends in a request
line or a Referer. The page posts the code once; the Hub answers with an HttpOnly, SameSite=Strict session cookie
and a separate origin binding that the browser keeps in sessionStorage and sends as a header.
Codes last ten minutes and work once. Sessions live in the server's memory and end when it restarts.

The owner's tailnet identity is the other proof the daemon accepts: on a request that tailscaled forwarded
(tailnet.py), the page can ask for a session with `{"method": "tailnet"}`, and the server grants it only for a login
the daemon lists in `allowed_logins` (server.py).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import threading
import time
from pathlib import Path

from .state import SIGNIN_FILE, prepare_dir, read_json, signin_lock, write_private

CODE_TTL_SECONDS = 600
SESSION_TTL_SECONDS = 12 * 3600
MAX_CODES = 5
FAILURES_PER_MINUTE = 10
TAILNET_ATTEMPTS_PER_MINUTE = 5  # per login, apart from the link throttle so no one else can lock the owner out
TAILNET_LOGINS_TRACKED = 256


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _live_codes(path: Path, now: float) -> list[dict]:
    codes = read_json(path, [])
    if not isinstance(codes, list):
        return []
    return [c for c in codes if isinstance(c, dict) and isinstance(c.get("hash"), str)
            and isinstance(c.get("expires_at"), int | float) and c["expires_at"] > now]


def create_signin_code(state_dir: Path, *, now: float | None = None) -> str:
    """Make a one-time sign-in code and record its hash. Run on the daemon host as the Hub's account."""
    now = time.time() if now is None else now
    path = prepare_dir(state_dir) / SIGNIN_FILE
    code = secrets.token_urlsafe(32)
    with signin_lock(state_dir):
        codes = _live_codes(path, now)[-(MAX_CODES - 1):]
        codes.append({"hash": _digest(code), "expires_at": now + CODE_TTL_SECONDS})
        write_private(path, json.dumps(codes) + "\n")
    return code


class Sessions:
    def __init__(self, state_dir: Path, clock=time.time):
        self.path = state_dir / SIGNIN_FILE
        self.clock = clock
        self._sessions: dict[str, tuple[float, str]] = {}
        self._failures: list[float] = []
        self._tailnet: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def throttled(self) -> bool:
        now = self.clock()
        with self._lock:
            self._failures = [t for t in self._failures if t > now - 60]
            return len(self._failures) >= FAILURES_PER_MINUTE

    def tailnet_attempt(self, login: str) -> bool:
        """Count a tailnet sign-in attempt for a verified login; False once it has had its share this minute."""
        now = self.clock()
        with self._lock:
            recent = [t for t in self._tailnet.pop(login, []) if t > now - 60]
            allowed = len(recent) < TAILNET_ATTEMPTS_PER_MINUTE
            if allowed:
                recent.append(now)
            self._tailnet[login] = recent  # most recent last, so the oldest login is dropped first
            while len(self._tailnet) > TAILNET_LOGINS_TRACKED:
                del self._tailnet[next(iter(self._tailnet))]
            return allowed

    def redeem_code(self, code: str | None) -> tuple[str, str] | None:
        """Spend a sign-in code; returns a cookie id and an origin binding, or None."""
        now = self.clock()
        with self._lock, signin_lock(self.path.parent):
            codes = _live_codes(self.path, now)
            given = _digest(code) if isinstance(code, str) and 20 <= len(code) <= 128 else ""
            match = next((c for c in codes if given and hmac.compare_digest(c["hash"], given)), None)
            if match is None:
                self._failures = [t for t in self._failures if t > now - 60][-FAILURES_PER_MINUTE:]
                self._failures.append(now)
                return None
            codes.remove(match)
            write_private(self.path, json.dumps(codes) + "\n")
            return self._start(now)

    def create(self) -> tuple[str, str]:
        """A session for a caller proved some other way (a tailnet login the daemon lists as the owner's)."""
        with self._lock:
            return self._start(self.clock())

    def _start(self, now: float) -> tuple[str, str]:
        session = secrets.token_urlsafe(32)
        binding = secrets.token_urlsafe(32)
        self._sessions[_digest(session)] = (now + SESSION_TTL_SECONDS, _digest(binding))
        return session, binding

    def valid(self, session: str | None, binding: str | None = None) -> bool:
        if not session or not binding:
            return False
        now = self.clock()
        with self._lock:
            key = _digest(session)
            record = self._sessions.get(key)
            if record is None or record[0] <= now:
                self._sessions.pop(key, None)
                return False
            return hmac.compare_digest(record[1], _digest(binding))

    def end(self, session: str | None) -> None:
        if session:
            with self._lock:
                self._sessions.pop(_digest(session), None)
