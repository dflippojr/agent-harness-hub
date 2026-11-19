"""The daemon endpoint's identity is verified before the Hub sends it anything (#264).

A loopback address limits who can reach the daemon's port, not who can listen on it. These tests put a listener of
another local account where the daemon should be and check that it receives nothing from the Hub: not the Hub key,
not the claim verifier, not a request.

The listener runs as another account (`nobody`) through `sudo -n`, so these tests need Linux and passwordless sudo
(a GitHub-hosted runner has it); elsewhere they are skipped.
"""

from __future__ import annotations

import base64
import contextlib
import functools
import pwd
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from harness_hub.state import StateStore

from .conftest import assert_no_secrets
from .test_env_hostility import KEY, VERIFIER, real_hub

OTHER_ACCOUNT = "nobody"
SYSTEM_PYTHON = "/usr/bin/python3"
KEY_ID = "k-synthetic01"

# A listener that records every connection it accepts (the bytes it received, base64, one line each) and answers 502.
# It prints its port first and exits when its stdin closes, so the test can stop it without signalling another
# account's process.
SQUATTER = r"""
import base64, os, select, socket, sys, threading
threading.Thread(target=lambda: (sys.stdin.read(), os._exit(0)), daemon=True).start()
server = socket.create_server(("127.0.0.1", 0))
print(server.getsockname()[1], flush=True)
while True:
    ready, _, _ = select.select([server], [], [], 120)
    if not ready:
        os._exit(0)
    conn, _ = server.accept()
    with conn:
        conn.settimeout(2)
        data = b""
        try:
            while b"\r\n\r\n" not in data and len(data) < 65536:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                data += chunk
            head, _, body = data.partition(b"\r\n\r\n")
            length = 0
            for line in head.split(b"\r\n")[1:]:
                name, _, value = line.partition(b":")
                if name.strip().lower() == b"content-length" and value.strip().isdigit():
                    length = int(value.strip())
            while len(body) < min(length, 65536):
                chunk = conn.recv(65536)
                if not chunk:
                    break
                body += chunk
                data += chunk
        except OSError:
            pass
        print(base64.b64encode(data).decode(), flush=True)
        try:
            conn.sendall(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
        except OSError:
            pass
"""


@functools.cache
def other_account() -> str:
    """The account the listener runs as, or '' when this machine cannot start a process as another account."""
    if sys.platform != "linux" or not shutil.which("sudo") or not Path(SYSTEM_PYTHON).is_file():
        return ""
    try:
        pwd.getpwnam(OTHER_ACCOUNT)
    except KeyError:
        return ""
    try:
        done = subprocess.run(["sudo", "-n", "-u", OTHER_ACCOUNT, SYSTEM_PYTHON, "-I", "-c", "pass"],
                              capture_output=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return OTHER_ACCOUNT if done.returncode == 0 else ""


needs_other_account = pytest.mark.skipif(not other_account(), reason="needs Linux and passwordless sudo to run a "
                                         f"listener as another account ({OTHER_ACCOUNT})")


class Squatter:
    """A listener of another local account on a loopback port, recording what each connection sent it."""

    def __init__(self):
        self.received: list[bytes] = []
        self.proc = subprocess.Popen(["sudo", "-n", "-u", other_account(), SYSTEM_PYTHON, "-I", "-c", SQUATTER],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        line = self.proc.stdout.readline().strip()
        assert line.isdigit(), f"the other account's listener did not start: {line!r}"
        self.port = int(line)
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def _read(self) -> None:
        for line in self.proc.stdout:
            self.received.append(base64.b64decode(line.strip()))

    def connections(self) -> int:
        return len(self.received)

    def __enter__(self) -> Squatter:
        return self

    def __exit__(self, *exc) -> None:
        with contextlib.suppress(OSError):
            self.proc.stdin.close()
        try:
            self.proc.wait(10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(10)
        self._reader.join(5)


def store_key(state_dir: Path, url: str) -> None:
    StateStore(state_dir).update(key={"token": KEY, "key_id": KEY_ID, "daemon_url": url, "claimed_at": time.time()})


def store_pending_claim(state_dir: Path, url: str) -> None:
    now = time.time()
    StateStore(state_dir).update(claim={"request_id": "pr-synthetic", "verifier": VERIFIER, "match_code": "123456",
                                        "started_at": now, "expires_at": now + 600, "daemon_url": url})


def wait_until(predicate, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.02)


@needs_other_account
def test_a_listener_of_another_account_gets_no_key_from_the_parallel_overview_reads(tmp_path):
    """The default plain-HTTP loopback URL, no opt-in, and a listener of another account on it: Overview's reads
    (the public health read and the owner API reads with the key, sent together) reach it with nothing, and Overview
    reports why."""
    with Squatter() as squatter:
        store_key(tmp_path / "state", squatter.url)
        with real_hub(tmp_path, squatter.url) as hub:
            hub.sign_in()
            overview = hub.get("/api/overview")
            assert overview.status_code == 200, overview.text
            body = overview.json()
            wait_until(lambda: squatter.connections() >= 1)
            time.sleep(0.2)  # any read still on its way
        sent = b"".join(squatter.received)
        assert KEY.encode() not in sent and b"authorization" not in sent.lower()
        assert squatter.received and all(data == b"" for data in squatter.received), squatter.received
        assert "not verified" in body["error"] and OTHER_ACCOUNT in body["error"], body["error"]
        assert body["answered"] is False
    assert StateStore(tmp_path / "state").get("key")["token"] == KEY  # an unverified endpoint never costs the key
    assert_no_secrets(hub.bodies, KEY)


@needs_other_account
def test_a_listener_of_another_account_gets_no_claim_verifier(tmp_path):
    """A pending claim's redeem carries the PKCE verifier, which would let its holder take the Hub key: a listener of
    another account never receives it."""
    with Squatter() as squatter:
        store_pending_claim(tmp_path / "state", squatter.url)
        with real_hub(tmp_path, squatter.url):
            wait_until(lambda: squatter.connections() >= 1)
            time.sleep(0.3)  # the poller retries an unanswered redeem every poll_seconds
        sent = b"".join(squatter.received)
        assert VERIFIER.encode() not in sent and b"code_verifier" not in sent
        assert all(data == b"" for data in squatter.received), squatter.received
