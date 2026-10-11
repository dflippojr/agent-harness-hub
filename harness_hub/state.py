"""The Hub's private state: its daemon key, the pending claim's PKCE verifier and sign-in codes.

Everything here stays on the daemon host in the state directory, readable by the Hub's account only. No route sends
any of it to a browser: the server builds browser responses from allowlisted fields (`views.py`) and a response guard
withholds anything that still looks like a credential (`guard.py`).
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any

STATE_FILE = "hub-state.json"
SIGNIN_FILE = "signin-codes.json"
SERVER_FILE = "server.json"


def prepare_dir(path: Path) -> Path:
    """Create the state directory with owner-only permissions where the platform has them."""
    path.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        from .windows_acl import protect

        protect(path)
    else:
        path.chmod(0o700)
    return path


def write_private(path: Path, text: str) -> None:
    """Atomically replace `path` with `text`, readable by this account only."""
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            if os.name == "nt":
                from .windows_acl import protect

                protect(Path(tmp))
            else:
                os.fchmod(handle.fileno(), 0o600)
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def read_json(path: Path, default: Any) -> Any:
    try:
        if os.name == "nt" and path.exists():
            from .windows_acl import protect

            protect(path)
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


@contextlib.contextmanager
def signin_lock(state_dir: Path):
    """Serialize CLI/server code updates across processes, using a persistent lock inode."""
    path = prepare_dir(state_dir) / ".signin.lock"
    with path.open("a+b") as handle:
        if os.name == "nt":
            import msvcrt

            # Windows locks a byte range; ensure the byte exists without replacing the lock file.
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class StateStore:
    """`hub-state.json`: `{key, claim, outcome}`.

    - `key`: `{token, key_id, daemon_url, claimed_at}` once a claim is redeemed.
    - `claim`: `{request_id, verifier, match_code, expires_at, started_at, daemon_url}` while a claim waits.
    - `outcome`: `{state, at}` for the last claim that ended without a key (refused, expired, denied, released).
    """

    def __init__(self, state_dir: Path):
        self.dir = prepare_dir(state_dir)
        self.path = self.dir / STATE_FILE
        self._lock = threading.Lock()
        data = read_json(self.path, {})
        self._data: dict = data if isinstance(data, dict) else {}

    def get(self, name: str) -> dict | None:
        with self._lock:
            value = self._data.get(name)
            return dict(value) if isinstance(value, dict) else None

    def update(self, **values: dict | None) -> None:
        with self._lock:
            for name, value in values.items():
                if value is None:
                    self._data.pop(name, None)
                else:
                    self._data[name] = value
            write_private(self.path, json.dumps(self._data, indent=2) + "\n")

    def secrets(self) -> list[str]:
        """Every secret this store holds, for the response guard."""
        with self._lock:
            key = self._data.get("key") or {}
            claim = self._data.get("claim") or {}
            return [v for v in (key.get("token"), claim.get("verifier")) if isinstance(v, str) and v]
