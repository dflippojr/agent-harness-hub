"""The Hub's private state: its daemon key, the pending claim's PKCE verifier and sign-in codes.

Everything here stays on the daemon host in the state directory, readable by the Hub's account only. No route sends
any of it to a browser: the server builds browser responses from allowlisted fields (`views.py`) and a response guard
withholds anything that still looks like a credential (`guard.py`).
"""

from __future__ import annotations

import contextlib
import errno
import json
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

STATE_FILE = "hub-state.json"
SIGNIN_FILE = "signin-codes.json"
SERVER_FILE = "server.json"
SIGNIN_LOCK_SECONDS = 5.0  # how long a sign-in or signin-link waits for another process's code update
_LOCK_HELD = {errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK, getattr(errno, "EDEADLOCK", errno.EDEADLK)}


def prepare_dir(path: Path) -> Path:
    """Create the state directory with owner-only permissions where the platform has them."""
    path.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        from .windows_acl import protect

        protect(path)
    else:
        path.chmod(0o700)
    return path


def _replace_private(source: str, path: Path) -> None:
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        move = ctypes.WinDLL("kernel32", use_last_error=True).MoveFileExW
        move.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
        move.restype = wintypes.BOOL
        # REPLACE_EXISTING | WRITE_THROUGH; the file itself was already fsynced.
        if not move(source, str(path), 0x1 | 0x8):
            raise ctypes.WinError(ctypes.get_last_error())
    else:
        os.replace(source, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            try:
                os.fsync(directory)
            except OSError as exc:
                if exc.errno not in (errno.EINVAL, errno.ENOTSUP):
                    raise
        finally:
            os.close(directory)


def write_private(path: Path, text: str) -> None:
    """Replace private state after fsyncing the file and, where supported, its directory."""
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
        _replace_private(tmp, path)
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


class SigninBusy(TimeoutError):
    """Another process held the sign-in lock for longer than `SIGNIN_LOCK_SECONDS`."""


@contextlib.contextmanager
def signin_lock(state_dir: Path):
    """Serialize CLI/server code updates across processes, using a persistent lock inode.

    The wait is bounded on every platform: a holder that stalls (a suspended terminal, a slow or scanned disk) makes
    this raise `SigninBusy` after `SIGNIN_LOCK_SECONDS` instead of blocking forever (POSIX) or failing with an
    unexplained OSError (Windows `LK_LOCK` gives up after about ten seconds)."""
    path = prepare_dir(state_dir) / ".signin.lock"
    with path.open("a+b") as handle:
        if os.name == "nt":
            import msvcrt

            # Windows locks a byte range; ensure the byte exists without replacing the lock file.
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
        else:
            import fcntl
        deadline = time.monotonic() + SIGNIN_LOCK_SECONDS
        while True:
            try:
                if os.name == "nt":
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as error:
                if error.errno not in _LOCK_HELD:
                    raise
                if time.monotonic() >= deadline:
                    raise SigninBusy("another process is updating sign-in codes") from error
                time.sleep(0.05)
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
    - `claim`: `{request_id, verifier, match_code, expires_at, started_at, daemon_url}` while a claim waits, plus
      `redeem_unanswered: true` once a redeem may have reached the daemon without its reply arriving.
    - `outcome`: `{state, at}` for the last claim that ended without a key (refused, expired, denied, released,
      rate_limited, failed, lost).
    """

    def __init__(self, state_dir: Path):
        self.dir = prepare_dir(state_dir)
        self.path = self.dir / STATE_FILE
        self._lock = threading.Lock()
        self._write_lock = threading.Lock()
        data = read_json(self.path, {})
        self._data: dict = data if isinstance(data, dict) else {}

    def get(self, name: str) -> dict | None:
        with self._lock:
            value = self._data.get(name)
            return dict(value) if isinstance(value, dict) else None

    def update(self, **values: dict | None) -> None:
        # Disk I/O serializes writers, but never blocks readers of the last saved snapshot.
        with self._write_lock:
            with self._lock:
                candidate = dict(self._data)
            for name, value in values.items():
                if value is None:
                    candidate.pop(name, None)
                else:
                    candidate[name] = dict(value)
            write_private(self.path, json.dumps(candidate, indent=2) + "\n")
            with self._lock:
                self._data = candidate

    def secrets(self) -> list[str]:
        """Every secret this store holds, for the response guard."""
        with self._lock:
            key = self._data.get("key") or {}
            claim = self._data.get("claim") or {}
            return [v for v in (key.get("token"), claim.get("verifier")) if isinstance(v, str) and v]
