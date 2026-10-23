"""Signing a browser in to the Hub.

The Hub holds an admin-equivalent key, so nobody uses it without proving they are the owner. The proof is the one the
daemon uses for local callers: the owner can read a file that only the owner's account can read. On the daemon host,
`python -m harness_hub signin-link` writes a one-time code (stored only as a hash) into the Hub's owner-only state
directory and prints a sign-in link carrying the code in its fragment, which the browser never sends in a request
line or a Referer. The page posts the code once; the Hub answers with an HttpOnly, SameSite=Strict session cookie
and a separate origin binding that the browser keeps in sessionStorage and sends as a header.
Codes last ten minutes and work once. Sessions live in the server's memory and end when it restarts.

A browser has one cookie per origin, so a second tab that signs in with its own code while the cookie's session is still
live joins that session: it gets a binding of its own, the cookie and the expiry stay as they were, and every binding
works until the session expires or signs out (which ends them all). A binding authorizes only its own session's cookie.

The owner's tailnet identity is the other proof the daemon accepts: on a request that tailscaled forwarded
(tailnet.py), the page can ask for a session with `{"method": "tailnet"}`, and the server grants it only for a login
the daemon lists in `allowed_logins` (server.py). Its binding keeps that login, and the server checks it against the
daemon's list again on later requests: once the daemon stops listing it, `revoke` ends that binding alone, and
`confirm` records each daemon read that found it listed, so the server can bound how long an outage keeps the binding.
That bound runs on a monotonic clock, which a wall-clock step back cannot stretch; codes and session expiry use the wall
clock, since codes are on disk and shared with signin-link. A binding from a sign-in link has no login and is never
checked.
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
MAX_BINDINGS = 32  # tabs per session; one more drops the oldest tab's binding
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
    def __init__(self, state_dir: Path, clock=time.time, monotonic=time.monotonic):
        self.path = state_dir / SIGNIN_FILE
        self.clock = clock
        self.monotonic = monotonic
        # digest -> (expiry, [(binding digest, the tailnet login that opened it or None for a sign-in link, when the
        # daemon last listed that login on `monotonic`: the read behind the sign-in, or the last read that found it)])
        self._sessions: dict[str, tuple[float, list[tuple[str, str | None, float]]]] = {}
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

    def redeem_code(self, code: str | None, joining: str | None = None) -> tuple[str, str] | None:
        """Spend a sign-in code; returns a cookie id and an origin binding, or None. With the caller's live cookie id
        in `joining`, the binding joins that session and the same cookie id comes back.

        Waiting too long for another process's code update raises `SigninBusy` (an OSError), which spends no code and
        counts no failure. Only the file lock is held while it waits, so requests that check a session never wait."""
        now = self.clock()
        given = _digest(code) if isinstance(code, str) and 20 <= len(code) <= 128 else ""
        with signin_lock(self.path.parent):
            codes = _live_codes(self.path, now)
            match = next((c for c in codes if given and hmac.compare_digest(c["hash"], given)), None)
            if match is not None:
                codes.remove(match)
                write_private(self.path, json.dumps(codes) + "\n")
        with self._lock:
            if match is None:
                self._failures = [t for t in self._failures if t > now - 60][-FAILURES_PER_MINUTE:]
                self._failures.append(now)
                return None
            return self._start(now, joining)

    def create(self, joining: str | None = None, login: str | None = None,
               listed: float | None = None) -> tuple[str, str]:
        """A session for a caller proved some other way: a tailnet `login` the daemon lists as the owner's, which the
        new binding keeps so the server can check it again, and `listed`, when the daemon read that listed it ran (on
        `monotonic`; now if not given). `joining` works as in `redeem_code`."""
        with self._lock:
            return self._start(self.clock(), joining, login, listed)

    def _start(self, now: float, joining: str | None, login: str | None = None,
               listed: float | None = None) -> tuple[str, str]:
        binding = secrets.token_urlsafe(32)
        entry = (_digest(binding), login, self.monotonic() if listed is None else listed)
        record = self._sessions.get(_digest(joining)) if joining else None
        if record is not None and record[0] > now:
            record[1].append(entry)
            del record[1][:-MAX_BINDINGS]
            return joining, binding
        session = secrets.token_urlsafe(32)
        self._sessions[_digest(session)] = (now + SESSION_TTL_SECONDS, [entry])
        return session, binding

    def _find(self, session: str | None, binding: str | None) -> tuple[str, str | None, float] | None:
        """The live binding these name, as (digest, login, listed); call with the lock held."""
        if not session or not binding:
            return None
        key = _digest(session)
        record = self._sessions.get(key)
        if record is None or record[0] <= self.clock():
            self._sessions.pop(key, None)
            return None
        given = _digest(binding)
        matches = [entry for entry in record[1] if hmac.compare_digest(entry[0], given)]  # no early exit
        return matches[0] if matches else None

    def valid(self, session: str | None, binding: str | None = None) -> bool:
        with self._lock:
            return self._find(session, binding) is not None

    def binding(self, session: str | None, binding: str | None) -> tuple[str | None, float] | None:
        """A live binding as (its tailnet login or None for a link's, seconds since the daemon last listed that login);
        None when it is not live. One call, so a binding that ends between two reads is never taken for a link's."""
        with self._lock:
            entry = self._find(session, binding)
            return (entry[1], self.monotonic() - entry[2]) if entry else None

    def confirm(self, session: str | None, binding: str | None, listed: float | None = None) -> None:
        """Record that a daemon read at `listed` (on `monotonic`; now if not given) lists a live binding's login. An
        earlier read than the one recorded changes nothing, and a binding that ended stays ended."""
        with self._lock:
            entry = self._find(session, binding)
            if entry is not None:
                listed = self.monotonic() if listed is None else listed
                bindings = self._sessions[_digest(session)][1]
                bindings[bindings.index(entry)] = (entry[0], entry[1], max(entry[2], listed))

    def revoke(self, session: str | None, binding: str | None) -> None:
        """End one binding, for good; the session's other bindings and its expiry stay as they were."""
        with self._lock:
            entry = self._find(session, binding)
            if entry is None:
                return
            key = _digest(session)
            self._sessions[key][1].remove(entry)
            if not self._sessions[key][1]:
                del self._sessions[key]

    def end(self, session: str | None) -> None:
        if session:
            with self._lock:
                self._sessions.pop(_digest(session), None)
