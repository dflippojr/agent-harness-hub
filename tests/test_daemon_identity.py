"""The daemon endpoint's identity is verified before the Hub sends it anything (#264).

A loopback address limits who can reach the daemon's port, not who can listen on it. The Hub checks, on each new
connection to a loopback http:// daemon and before writing a byte, that the socket at the other end belongs to the
daemon's account (harness_hub/endpoint.py). These tests cover:

- reading the connection table as the kernel prints it, and finding the other end of a real connection;
- the check's verdict for each lookup result, and the error it reports;
- `--daemon-owner` and `--allow-insecure-http`, and the startup notice each gives;
- real sockets on this machine: an endpoint whose listener is not the expected account's is sent nothing, by every
  kind of call and by calls sent in parallel, while the right one works and the opt-in keeps the old behaviour.

Nothing here is skipped. A platform without a lookup (macOS, the BSDs) must refuse every loopback listener, so the
real-socket tests expect exactly that there. The two cross-account tests run their listener as a real other account
(`nobody`, through `sudo -n`) where this machine allows it, as on CI's Linux runners, and their test id says so
(`another-account`). Elsewhere (`another-expected-account`) the listener is this account's and the Hub is told the
daemon runs as another; a real socket and the platform's real lookup decide either way.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import ctypes
import functools
import ipaddress
import json
import logging
import os
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from harness_hub import __main__ as cli
from harness_hub import endpoint
from harness_hub.config import Settings
from harness_hub.daemon import DaemonClient, DaemonUnreachable, DaemonUnverified, unanswered
from harness_hub.endpoint import (
    UNREADABLE,
    EndpointUnverified,
    ListenerCheck,
    Owner,
    identity_notice,
    listener_check,
    proc_owners,
    resolve_owner,
)
from harness_hub.server import create_app
from harness_hub.state import StateStore
from harness_hub.status import LISTENER_OWNER, STATUS_FILE, TLS, UNVERIFIED, StatusReporter, daemon_check

from .conftest import HUB_HOST, FakeDaemon, HubClient, assert_no_secrets
from .test_env_hostility import KEY, VERIFIER, Listener, every_call, real_hub, serving

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
    import pwd

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


def other_owner() -> str:
    """An account that is not the one running the tests, in the form --daemon-owner takes on this platform."""
    return "NOWHERE\\hub-test-other" if sys.platform == "win32" else str(os.geteuid() + 4242)


# Whether this platform has a listener lookup (Linux, Windows). Without one, every loopback listener is refused.
LOOKUP = endpoint.supported()
# How the cross-account tests make the listener foreign here (see the module docstring); shown in their test ids.
FOREIGN = "another-account" if other_account() else "another-expected-account"


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


@dataclass
class Foreign:
    """A loopback listener the Hub must not trust: where it is, what each connection sent it, the Hub settings that
    make it foreign, and the account a refusal names as its own ('' when that is not this test's to say)."""

    url: str
    received: list[bytes]
    settings: dict
    account: str


@contextlib.contextmanager
def foreign_listener(mode: str):
    if mode == "another-account":
        with Squatter() as squatter:
            yield Foreign(squatter.url, squatter.received, {}, OTHER_ACCOUNT)
    else:
        with Listener() as listener:
            yield Foreign(listener.url, listener.heads, {"daemon_owner": other_owner()}, "")


@contextlib.contextmanager
def hub_on(tmp_path: Path, url: str, **settings):
    config = Settings(daemon_url=url, state_dir=tmp_path / "state", poll_seconds=0.02, **settings)
    with TestClient(create_app(config), base_url=f"http://{HUB_HOST}") as client:
        yield HubClient(client, config.state_dir)


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


@pytest.mark.parametrize("mode", [FOREIGN])
def test_a_listener_of_another_account_gets_no_key_from_the_parallel_overview_reads(tmp_path, mode):
    """The default plain-HTTP loopback URL, no opt-in, and a listener of another account on it: Overview's reads
    (the public health read and the owner API reads with the key, sent together) reach it with nothing, and Overview
    reports why."""
    with foreign_listener(mode) as listener:
        store_key(tmp_path / "state", listener.url)
        with hub_on(tmp_path, listener.url, **listener.settings) as hub:
            hub.sign_in()
            overview = hub.get("/api/overview")
            assert overview.status_code == 200, overview.text
            body = overview.json()
            wait_until(lambda: len(listener.received) >= 1)
            time.sleep(0.2)  # any read still on its way
        sent = b"".join(listener.received)
        assert KEY.encode() not in sent and b"authorization" not in sent.lower()
        assert listener.received and all(data == b"" for data in listener.received), listener.received
        assert "not verified" in body["error"] and listener.account in body["error"], body["error"]
        assert body["answered"] is False
    assert StateStore(tmp_path / "state").get("key")["token"] == KEY  # an unverified endpoint never costs the key
    assert_no_secrets(hub.bodies, KEY)


@pytest.mark.parametrize("mode", [FOREIGN])
def test_a_listener_of_another_account_gets_no_claim_verifier(tmp_path, mode):
    """A pending claim's redeem carries the PKCE verifier, which would let its holder take the Hub key: a listener of
    another account never receives it."""
    with foreign_listener(mode) as listener:
        store_pending_claim(tmp_path / "state", listener.url)
        with hub_on(tmp_path, listener.url, **listener.settings):
            wait_until(lambda: len(listener.received) >= 1)
            time.sleep(0.3)  # the poller retries an unanswered redeem every poll_seconds
        sent = b"".join(listener.received)
        assert VERIFIER.encode() not in sent and b"code_verifier" not in sent
        assert all(data == b"" for data in listener.received), listener.received


# --- the connection table (Linux) -------------------------------------------------------------------------------------

HEADER = "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"
TAIL = "00000000:00000000 00:00000000 00000000"


def row(n: int, local: str, remote: str, state: str, uid: int) -> str:
    return f"{n:4d}: {local} {remote} {state} {TAIL} {uid:5d}        0 {1000 + n} 1 0000000000000000 20 4 30 10 -1\n"


def tables(tmp_path: Path, tcp: list[str], tcp6: list[str] | None) -> list[str]:
    (tmp_path / "tcp").write_text(HEADER + "".join(tcp), encoding="ascii")
    if tcp6 is not None:
        (tmp_path / "tcp6").write_text(HEADER + "".join(tcp6), encoding="ascii")
    return [str(tmp_path / "tcp"), str(tmp_path / "tcp6")]


def kernel(host: str, port: int) -> str:
    """An address and port as Linux prints them in /proc/net/tcp and tcp6: each 32-bit word of the address read as this
    machine's native integer, in upper-case hex, then the port."""
    packed = ipaddress.ip_address(host).packed
    words = (int.from_bytes(packed[i:i + 4], sys.byteorder) for i in range(0, len(packed), 4))
    return "".join(f"{word:08X}" for word in words) + f":{port:04X}"


CONNECTION = (("127.0.0.1", 40000), ("127.0.0.1", 8100))  # the Hub's address, the daemon's
DAEMON, HUB = kernel("127.0.0.1", 8100), kernel("127.0.0.1", 40000)


def test_rows_are_written_as_the_kernel_prints_them():
    expected = {"little": ("0100007F:1FA4", "0000000000000000FFFF00000100007F:1FA4"),
                "big": ("7F000001:1FA4", "00000000000000000000FFFF7F000001:1FA4")}[sys.byteorder]
    assert (DAEMON, kernel("::ffff:127.0.0.1", 8100)) == expected


def test_the_far_end_is_read_from_the_table_as_the_kernel_prints_it(tmp_path):
    paths = tables(tmp_path, [
        row(0, DAEMON, "00000000:0000", "0A", 1000),  # the listener: not this connection
        row(1, DAEMON, HUB, "01", 1000),  # the daemon's end
        row(2, HUB, DAEMON, "01", 2000),  # the Hub's own end
        row(3, DAEMON, HUB, "06", 0),  # TIME_WAIT lists uid 0
    ], [])
    assert proc_owners(*CONNECTION, tables=paths) == [1000]


def test_a_not_yet_accepted_or_dual_stack_far_end_is_found(tmp_path):
    paths = tables(tmp_path, [row(0, DAEMON, HUB, "03", 1001)],
                   [row(0, kernel("::ffff:127.0.0.1", 8100), kernel("::ffff:127.0.0.1", 40000), "01", 1002),
                    row(1, kernel("::1", 8100), kernel("::1", 40000), "01", 1003)])
    assert proc_owners(*CONNECTION, tables=paths) == [1001, 1002]
    assert proc_owners(("::1", 40000), ("::1", 8100), tables=paths) == [1003]
    assert proc_owners(("::ffff:127.0.0.1", 40000), ("127.0.0.1", 8100), tables=paths) == [1001, 1002]


def test_a_kernel_without_ipv6_is_read_and_one_without_the_table_raises(tmp_path):
    paths = tables(tmp_path, [], None)
    assert proc_owners(*CONNECTION, tables=paths) == []
    with pytest.raises(FileNotFoundError):
        proc_owners(*CONNECTION, tables=[str(tmp_path / "missing")])


def lookup(local, remote) -> list:
    """What this platform's own lookup finds at the other end of a connection: uids on Linux, accounts on Windows."""
    if sys.platform == "linux":
        return proc_owners(local, remote)
    if sys.platform == "win32":
        return endpoint.windows_owners(local, remote)
    return []


def me() -> object:
    """This account as this platform's lookup names it."""
    return os.geteuid() if sys.platform == "linux" else endpoint.windows_account() if sys.platform == "win32" else None


def test_the_far_end_of_a_real_connection_is_found_before_and_after_accept():
    """A real loopback connection, before and after the server accepts it: this platform's lookup names this account
    (a connection not accepted yet is listed with its listener's owner), and the check passes it. Without a lookup,
    the check refuses it."""
    server = socket.create_server(("127.0.0.1", 0))
    client = socket.create_connection(server.getsockname())
    check = listener_check(f"http://127.0.0.1:{server.getsockname()[1]}")
    accepted = None
    try:
        for _ in ("before accept", "after accept"):
            if LOOKUP:
                assert lookup(client.getsockname(), client.getpeername()) == [me()]
                check(client.getsockname(), client.getpeername())
            else:
                with pytest.raises(EndpointUnverified, match="on this platform"):
                    check(client.getsockname(), client.getpeername())
            accepted = accepted or server.accept()[0]
    finally:
        for sock in (accepted, client, server):
            if sock is not None:
                sock.close()


# --- the check's verdict ----------------------------------------------------------------------------------------------

URL = "http://127.0.0.1:8100"


def linux_check(found, owner_uid: int = 1000, *, me: int = 1000, unmapped: frozenset[int] = frozenset(),
                overflow: int = 65534) -> ListenerCheck:
    """A Linux check on any host: `found` is what the table lists, `me` the Hub's own uid, and every uid but those in
    `unmapped` has a mapping in the Hub's user namespace."""
    lookup = found if callable(found) else (lambda local, remote: list(found))
    return ListenerCheck(URL, Owner(uid=owner_uid), platform="linux", proc=lookup, me=lambda: me,
                         mapped=lambda uid: uid not in unmapped, overflow=lambda: overflow)


def windows_check(found, account: str = "TOWER\\owner") -> ListenerCheck:
    return ListenerCheck(URL, Owner(account=account), platform="win32", windows=lambda local, remote: list(found))


def refusal(check: ListenerCheck) -> str:
    with pytest.raises(EndpointUnverified) as caught:
        check(*CONNECTION)
    return str(caught.value)


def test_the_daemons_account_passes_and_any_other_is_refused_with_the_fix():
    linux_check([1000])(*CONNECTION)
    text = refusal(linux_check([1000, 4242]))
    assert "not verified" in text and "uid 4242" in text and "uid 1000" in text and "sent it nothing" in text
    assert "--daemon-owner" in text and "--allow-insecure-http" in text and "https" in text
    assert "not verified" in refusal(linux_check([]))
    assert "no socket at the other end" in refusal(linux_check([]))


def test_a_failed_lookup_refuses():
    def broken(local, remote):
        raise PermissionError("/proc/net/tcp")

    assert "could not read the connection table" in refusal(linux_check(broken))


def test_windows_accounts_compare_without_case_and_an_unreadable_one_is_refused():
    windows_check(["tower\\OWNER"])(*CONNECTION)
    assert windows_check([]).unidentifiable() == ""  # no uids on Windows: nothing to fail closed on
    assert "OTHER\\someone" in refusal(windows_check(["OTHER\\someone"]))
    assert UNREADABLE in refusal(windows_check([UNREADABLE]))
    assert "no socket at the other end" in refusal(windows_check([]))


def test_a_platform_without_a_check_refuses_and_says_what_to_use():
    text = refusal(ListenerCheck(URL, Owner(), platform="darwin"))
    assert "on this platform (darwin)" in text and "https" in text and "--allow-insecure-http" in text


def test_a_connection_without_addresses_is_refused():
    with pytest.raises(EndpointUnverified):
        linux_check([1000])(None, None)


def test_the_log_names_each_change_once(caplog):
    answers = [[4242], [4242], [1000], [1000], [4242]]
    check = linux_check(lambda local, remote: answers.pop(0))
    with caplog.at_level(logging.INFO, logger="harness_hub.endpoint"):
        for _ in range(5):
            with contextlib.suppress(EndpointUnverified):
                check(*CONNECTION)
    levels = [(r.levelno, "verified again" in r.getMessage()) for r in caplog.records]
    assert levels == [(logging.WARNING, False), (logging.INFO, True), (logging.WARNING, False)]
    assert check.checks == 5


def test_which_urls_are_checked(linux_accounts):
    assert isinstance(listener_check("http://127.0.0.1:8100", platform="linux"), ListenerCheck)
    assert isinstance(listener_check("http://[::1]:8100", platform="linux"), ListenerCheck)
    assert isinstance(listener_check("http://localhost:8100", platform="darwin"), ListenerCheck)
    assert listener_check("https://daemon.example.ts.net", platform="linux") is None
    assert listener_check("http://127.0.0.1:8100", allow_insecure_http=True, platform="linux") is None


# --- --daemon-owner and --allow-insecure-http -------------------------------------------------------------------------

class FakePwd:
    """The `pwd` module of a Linux machine with one account, `owner` (uid 1000)."""

    @staticmethod
    def getpwnam(name: str):
        if name != "owner":
            raise KeyError(name)
        return SimpleNamespace(pw_uid=1000)

    @staticmethod
    def getpwuid(uid: int):
        if uid != 1000:
            raise KeyError(uid)
        return SimpleNamespace(pw_name="owner")


@pytest.fixture
def linux_accounts(monkeypatch):
    """Linux's account lookups, on any platform: the faked `pwd` and an effective uid of 1000."""
    monkeypatch.setitem(sys.modules, "pwd", FakePwd)
    monkeypatch.setattr(os, "geteuid", lambda: 1000, raising=False)


def test_a_linux_owner_is_a_user_name_or_a_uid(linux_accounts):
    assert resolve_owner("owner", "linux") == Owner(uid=1000) == resolve_owner("", "linux")
    assert resolve_owner("4242", "linux") == Owner(uid=4242)
    with pytest.raises(ValueError, match="no account named"):
        resolve_owner("no-such-account-hub264", "linux")
    assert endpoint.uid_name(1000) == "owner (uid 1000)" and endpoint.uid_name(4242) == "uid 4242"


def test_a_windows_owner_names_its_domain():
    assert resolve_owner("TOWER\\owner", "win32") == Owner(account="TOWER\\owner")
    with pytest.raises(ValueError, match="DOMAIN"):
        resolve_owner("owner", "win32")


@pytest.mark.parametrize("extra", [{"daemon_owner": "4242"}, {"allow_insecure_http": True}])
def test_the_identity_settings_apply_only_to_a_plain_http_daemon(extra):
    with pytest.raises(ValueError, match="https daemon is verified by its certificate"):
        Settings(daemon_url="https://daemon.example.ts.net", **extra)


def test_the_owner_and_the_opt_in_cannot_both_be_set():
    with pytest.raises(ValueError, match="use one of them"):
        Settings(daemon_owner=other_owner(), allow_insecure_http=True)


@pytest.mark.parametrize("argv", [["--daemon-url", "https://daemon.example.ts.net", "--allow-insecure-http"],
                                  ["--daemon-owner", "4242", "--allow-insecure-http"]])
def test_conflicting_identity_arguments_exit(tmp_path, argv):
    with pytest.raises(SystemExit):
        cli.main([*argv, "--state-dir", str(tmp_path)])


def test_the_arguments_reach_the_settings(tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setattr("uvicorn.run", lambda app, **kw: seen.update(app=app))
    assert cli.main(["--allow-insecure-http", "--state-dir", str(tmp_path / "a")]) == 0
    assert seen["app"].fastapi.state.hub.settings.allow_insecure_http is True
    assert seen["app"].fastapi.state.hub.daemon.check is None
    assert cli.main(["--daemon-owner", other_owner(), "--state-dir", str(tmp_path / "b")]) == 0
    check = seen["app"].fastapi.state.hub.daemon.check
    assert check is not None and check.owner == resolve_owner(other_owner())


def test_the_startup_notice_says_how_the_daemon_is_verified():
    level, text = identity_notice(URL, None, allow_insecure_http=True)
    assert level == logging.WARNING and "--allow-insecure-http" in text and "without checking" in text
    assert identity_notice("https://daemon.example.ts.net", None, allow_insecure_http=False)[0] == logging.INFO
    level, text = identity_notice(URL, ListenerCheck(URL, Owner(), platform="darwin"), allow_insecure_http=False)
    assert level == logging.ERROR and "cannot be verified on this platform" in text
    level, text = identity_notice(URL, linux_check([1000]), allow_insecure_http=False)
    assert level == logging.INFO and "until it has verified that the listener belongs to" in text


# --- real sockets on this machine -------------------------------------------------------------------------------------

def run(coro):
    return asyncio.run(coro)


async def calls(client: DaemonClient, *, parallel: bool) -> list:
    try:
        if parallel:
            return await asyncio.gather(*every_call(client), *(client.admin(KEY) for _ in range(6)),
                                        return_exceptions=True)
        replies = []
        for call in every_call(client):
            try:
                replies.append(await call)
            except DaemonUnverified as exc:
                replies.append(exc)
        return replies
    finally:
        await client.aclose()


def wait_for_heads(listener: Listener, count: int) -> None:
    wait_until(lambda: len(listener.heads) >= count)


@pytest.mark.parametrize("parallel", [False, True])
def test_an_unverified_listener_gets_no_call_of_any_kind(parallel):
    """Every kind of call (public, with the Hub key, the claim's challenge and verifier), one after another or all
    at once: each opens a connection, the check refuses it, and the listener gets an empty connection."""
    with Listener() as listener:
        client = DaemonClient(listener.url, owner=other_owner())
        replies = run(calls(client, parallel=parallel))
        assert replies and all(isinstance(reply, DaemonUnverified) for reply in replies), replies
        assert all(reply.sent is False and "not verified" in str(reply) for reply in replies)
        wait_for_heads(listener, client.check.checks)
        assert listener.heads == [b""] * client.check.checks, listener.heads
        assert listener.authorizations() == []


@pytest.mark.parametrize("parallel", [False, True])
def test_the_daemons_listener_is_verified_on_each_connection(parallel):
    """The same calls to a listener of the expected account (this one): each connection is checked, and each call
    reaches the listener (a stub that answers 502 and closes, so every call opens a new connection). Without a
    lookup, each one is refused instead and the listener gets empty connections."""
    with Listener() as listener:
        client = DaemonClient(listener.url)
        replies = run(calls(client, parallel=parallel))
        wait_for_heads(listener, len(replies))
        assert client.check.checks == len(listener.heads) == len(replies)
        if LOOKUP:
            assert all(getattr(reply, "status", None) == 502 for reply in replies), replies
            assert all(head.startswith((b"GET ", b"POST ", b"PUT ", b"PATCH ", b"DELETE "))
                       for head in listener.heads)
        else:
            assert all(isinstance(reply, DaemonUnverified) for reply in replies), replies
            assert listener.heads == [b""] * len(replies)


def test_the_opt_in_sends_without_a_check():
    with Listener() as listener:
        client = DaemonClient(listener.url, allow_insecure_http=True)
        assert client.check is None
        replies = run(calls(client, parallel=False))
        assert all(getattr(reply, "status", None) == 502 for reply in replies)
        wait_for_heads(listener, len(replies))
        assert len(listener.authorizations()) == 6  # every call with the Hub key went out, as before this change


def test_overview_reports_an_unverified_listener_and_sends_it_nothing(tmp_path):
    """Overview with a stored key, against a listener that is not the expected account's: its reads go out
    together, and none of them is written on a connection; Overview says why and the key is kept."""
    with Listener() as listener:
        store_key(tmp_path / "state", listener.url)
        with hub_on(tmp_path, listener.url, daemon_owner=other_owner()) as hub:
            hub.sign_in()
            body = hub.get("/api/overview").json()
            started = hub.post("/api/claim")
        assert "not verified" in body["error"] and "--allow-insecure-http" in body["error"], body["error"]
        assert body["answered"] is False and body["claim"]["state"] == "claimed"
        assert started.status_code == 409  # already claimed: no claim request either
        assert listener.heads and all(head == b"" for head in listener.heads), listener.heads
    assert StateStore(tmp_path / "state").get("key")["token"] == KEY
    assert_no_secrets(hub.bodies, KEY)


def test_a_claim_is_not_started_on_an_unverified_listener(tmp_path):
    with Listener() as listener, hub_on(tmp_path, listener.url, daemon_owner=other_owner()) as hub:
        hub.sign_in()
        started = hub.post("/api/claim")
        assert started.status_code == 502 and "not verified" in started.json()["detail"], started.text
        assert listener.heads and all(head == b"" for head in listener.heads)


def test_a_platform_without_a_check_sends_a_plain_http_daemon_nothing(tmp_path, monkeypatch, caplog):
    """No opt-in, and a platform where the Hub cannot tell who listens: the startup log says so as an error, the
    stored key is never sent, and Overview reports the configuration error."""
    monkeypatch.setattr(endpoint, "PLATFORM", "darwin")
    with Listener() as listener:
        store_key(tmp_path / "state", listener.url)
        with caplog.at_level(logging.INFO), hub_on(tmp_path, listener.url) as hub:
            hub.sign_in()
            body = hub.get("/api/overview").json()
        assert "on this platform (darwin)" in body["error"] and "--allow-insecure-http" in body["error"]
        assert listener.heads and all(head == b"" for head in listener.heads)
    assert any(r.levelno == logging.ERROR and "cannot be verified on this platform" in r.getMessage()
               for r in caplog.records)


def test_the_opt_in_keeps_the_old_behaviour_and_warns_at_startup(tmp_path, caplog):
    daemon = FakeDaemon()
    with serving(daemon.app) as url:
        with caplog.at_level(logging.INFO), hub_on(tmp_path, url, allow_insecure_http=True) as hub:
            hub.sign_in()
            started = hub.post("/api/claim").json()
            daemon.approve(started["request_id"])
            hub.wait_for("claimed")
            body = hub.get("/api/overview").json()
            assert hub.client.app.fastapi.state.hub.daemon.check is None
        assert body["error"] == "" and body["ownership"] is not None
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("--allow-insecure-http" in text and url in text for text in warnings), warnings


def test_the_default_verifies_and_works_end_to_end_with_a_daemon_of_this_account(tmp_path, caplog):
    """The default settings against a daemon of this account on a real socket: claim and Overview work through the
    check. Without a lookup, the claim is refused with the platform's reason and the daemon gets no request."""
    daemon = FakeDaemon()
    with serving(daemon.app) as url:
        with caplog.at_level(logging.INFO), real_hub(tmp_path, url) as hub:
            hub.sign_in()
            started = hub.post("/api/claim")
            check = hub.client.app.fastapi.state.hub.daemon.check
            if LOOKUP:
                daemon.approve(started.json()["request_id"])
                hub.wait_for("claimed")
                body = hub.get("/api/overview").json()
                assert body["error"] == "" and body["ownership"] is not None
            else:
                assert started.status_code == 502 and "on this platform" in started.json()["detail"]
                assert daemon.requests == {}
        assert check is not None and check.checks >= 1 and check.owner == resolve_owner("")
    notice = "until it has verified that the listener belongs to" if LOOKUP else "cannot be verified on this platform"
    assert any(notice in r.getMessage() for r in caplog.records)
    assert_no_secrets(hub.bodies, daemon.hub_token)


# --- status.json ------------------------------------------------------------------------------------------------------

def check_name(url: str, **kw) -> str:
    async def name() -> str:
        client = DaemonClient(url, **kw)
        try:
            return daemon_check(client, allow_insecure_http=kw.get("allow_insecure_http", False))
        finally:
            await client.aclose()

    return run(name())


def test_status_json_names_how_the_daemon_is_verified():
    assert check_name("https://daemon.example.ts.net") == TLS
    assert check_name(URL, allow_insecure_http=True) == UNVERIFIED
    assert check_name(URL) == LISTENER_OWNER


def test_status_json_records_the_refusal_of_an_unverified_daemon(tmp_path):
    async def beat(url: str) -> dict:
        client = DaemonClient(url, owner=other_owner())
        reporter = StatusReporter(tmp_path, client, lambda: "claimed", interval=60, build="0" * 40,
                                  check=daemon_check(client, allow_insecure_http=False))
        try:
            return await reporter.beat()
        finally:
            await client.aclose()

    with Listener() as listener:
        status = run(beat(listener.url))
    assert status["daemon_check"] == LISTENER_OWNER and status["daemon_ok"] is False
    assert "not verified" in status["daemon_unverified"] and KEY not in json.dumps(status)
    assert json.loads((tmp_path / STATUS_FILE).read_text(encoding="utf-8")) == status


# --- the Windows lookup, against a faked psutil (the real one runs in CI's Windows jobs) ------------------------------

class FakeConn:
    def __init__(self, laddr, raddr, status, pid):
        self.laddr, self.raddr, self.status, self.pid = laddr, raddr, status, pid


class FakeProcess:
    accounts: dict[int, str] = {}
    started: dict[int, float] = {}  # when each process started, in seconds since the epoch
    replaced: frozenset[int] = frozenset()  # PIDs whose process changes while the Hub reads it

    def __init__(self, pid: int | None = None):
        self.pid = pid

    def username(self) -> str:
        import psutil

        if self.pid is None:
            return "TOWER\\owner"
        if self.pid not in self.accounts:
            raise psutil.AccessDenied(self.pid)
        return self.accounts[self.pid]

    def create_time(self) -> float:
        import psutil

        if self.pid not in self.started:
            raise psutil.AccessDenied(self.pid)
        return self.started[self.pid]

    def is_running(self) -> bool:
        return self.pid not in self.replaced


def test_the_windows_lookup_reads_the_far_ends_process_account(monkeypatch):
    import psutil  # a dev dependency on every platform (requirements-dev.lock)

    daemon, hub = ("127.0.0.1", 8100), ("127.0.0.1", 40000)
    table = [
        FakeConn(daemon, (), psutil.CONN_LISTEN, 10),  # the listener: not this connection
        FakeConn(daemon, hub, psutil.CONN_ESTABLISHED, 10),  # the daemon's end
        FakeConn(hub, daemon, psutil.CONN_ESTABLISHED, 20),  # the Hub's own end
        FakeConn(daemon, hub, psutil.CONN_TIME_WAIT, 0),
        FakeConn(("::ffff:127.0.0.1", 8100), ("::ffff:127.0.0.1", 40000), psutil.CONN_SYN_RECV, 30),  # dual stack
        FakeConn(daemon, hub, psutil.CONN_ESTABLISHED, 0),  # no process to read
        FakeConn(daemon, hub, psutil.CONN_ESTABLISHED, 40),  # another user's: AccessDenied
    ]
    monkeypatch.setattr(psutil, "net_connections", lambda kind: table)
    monkeypatch.setattr(psutil, "Process", FakeProcess)
    monkeypatch.setattr(FakeProcess, "accounts", {10: "TOWER\\owner", 20: "TOWER\\hub", 30: "TOWER\\dual"})
    assert endpoint.windows_owners(hub, daemon) == ["TOWER\\owner", "TOWER\\dual", UNREADABLE, UNREADABLE]
    assert endpoint.windows_account() == "TOWER\\owner"
    assert resolve_owner("", "win32") == Owner(account="TOWER\\owner")


# Windows' own TCP table (GetExtendedTcpTable, TCP_TABLE_OWNER_MODULE_ALL), faked with rows written as Windows writes
# them. psutil's table (TCP_TABLE_OWNER_PID_ALL, which has no creation times) is faked with the same rows, so a lookup
# that read it instead would fail these tests too.

FILETIME_UNIX_EPOCH = 116444736000000000  # 1970-01-01 as a FILETIME (100 ns units since 1601)
WINDOWS_STATES = {"LISTEN": 2, "SYN_RECV": 4, "ESTABLISHED": 5, "TIME_WAIT": 11}  # MIB_TCP_STATE
WINDOWS_FAMILIES = {4: 2, 6: 23}  # AF_INET and AF_INET6 as Windows numbers them


@dataclass
class WinRow:
    """A socket in Windows' TCP table: its state, its own and its peer's address, the PID that owns it, and when it
    was created (seconds since the epoch; None where Windows gives no time)."""

    state: str
    local: tuple[str, int]
    remote: tuple[str, int]
    pid: int
    created: float | None


def windows_table(rows: list[WinRow], version: int) -> bytes:
    """MIB_TCPTABLE_OWNER_MODULE (MIB_TCP6TABLE_OWNER_MODULE for IPv6) as GetExtendedTcpTable writes it: the row
    count, padding to the rows' 8-byte alignment, then the rows. A port is a DWORD in network byte order (the port in
    its low 16 bits), as an IPv4 address is, and the creation time a FILETIME."""

    def port(number: int) -> bytes:
        return struct.pack("!H", number) + b"\0\0"

    packed = []
    for row in rows:
        local, remote = ipaddress.ip_address(row.local[0]), ipaddress.ip_address(row.remote[0])
        if local.version != version:
            continue
        created = FILETIME_UNIX_EPOCH + round(row.created * 10_000_000) if row.created is not None else 0
        state = WINDOWS_STATES[row.state]
        if version == 4:
            packed.append(struct.pack("=I4s4s4s4sIq128x", state, local.packed, port(row.local[1]), remote.packed,
                                      port(row.remote[1]), row.pid, created))
        else:
            packed.append(struct.pack("=16sI4s16sI4sIIq128x", local.packed, 0, port(row.local[1]), remote.packed, 0,
                                      port(row.remote[1]), state, row.pid, created))
    return struct.pack("=I4x", len(packed)) + b"".join(packed)


class FakeGetExtendedTcpTable:
    """iphlpapi's GetExtendedTcpTable over faked tables: it says how large a buffer the table needs, then fills it."""

    def __init__(self, tables: dict[int, bytes | int]):
        self.tables = tables  # by Windows address family: the table, or the error code the call returns
        self.calls: list[tuple[int, int]] = []

    def __call__(self, buffer, size, order, family, table_class, reserved) -> int:
        self.calls.append((family, table_class))
        table = self.tables[family]
        if isinstance(table, int):
            return table
        if buffer is None or size._obj.value < len(table):
            size._obj.value = len(table)
            return 122  # ERROR_INSUFFICIENT_BUFFER
        ctypes.memmove(buffer, table, len(table))
        return 0


def windows_scenario(monkeypatch, rows: list[WinRow], *, accounts: dict[int, str], started: dict[int, float],
                     replaced: frozenset[int] = frozenset()) -> FakeGetExtendedTcpTable:
    """This machine's TCP table, and its processes' accounts and start times, as Windows gives them to the Hub."""
    import psutil

    get = FakeGetExtendedTcpTable({WINDOWS_FAMILIES[version]: windows_table(rows, version) for version in (4, 6)})
    monkeypatch.setattr(ctypes, "WinDLL", lambda name, **kw: SimpleNamespace(GetExtendedTcpTable=get), raising=False)
    status = {"LISTEN": psutil.CONN_LISTEN, "SYN_RECV": psutil.CONN_SYN_RECV, "ESTABLISHED": psutil.CONN_ESTABLISHED,
              "TIME_WAIT": psutil.CONN_TIME_WAIT}
    monkeypatch.setattr(psutil, "net_connections", lambda kind: [
        FakeConn(row.local, () if row.state == "LISTEN" else row.remote, status[row.state], row.pid) for row in rows])
    monkeypatch.setattr(psutil, "Process", FakeProcess)
    monkeypatch.setattr(FakeProcess, "accounts", accounts)
    monkeypatch.setattr(FakeProcess, "started", started)
    monkeypatch.setattr(FakeProcess, "replaced", replaced)
    return get


DAEMON_END, HUB_END, NO_PEER = CONNECTION[1], CONNECTION[0], ("0.0.0.0", 0)  # noqa: S104 - a listener's empty peer
OWNERS = {10: "TOWER\\owner", 11: "TOWER\\owner", 20: "TOWER\\hub"}


def daemon_rows(*, listener_pid: int = 10, end_pid: int = 10, listener_created: float | None = 1000.0) -> list[WinRow]:
    """The daemon's listener, the daemon's end of the Hub's connection, and the Hub's own end."""
    return [WinRow("LISTEN", DAEMON_END, NO_PEER, listener_pid, listener_created),
            WinRow("ESTABLISHED", DAEMON_END, HUB_END, end_pid, 2000.0),
            WinRow("ESTABLISHED", HUB_END, DAEMON_END, 20, 2000.0)]


def test_the_windows_check_passes_a_listener_its_own_process_created(monkeypatch):
    windows_scenario(monkeypatch, daemon_rows(), accounts=OWNERS, started={10: 900.0, 20: 950.0})
    ListenerCheck(URL, Owner(account="TOWER\\owner"), platform="win32")(*CONNECTION)


# Each way the far end of the Hub's connection fails to be tied to the process the table names: the rows, the
# processes' start times, the PIDs whose process changes while read, and a phrase of the refusal that says which.
UNTIED = {
    "a-process-younger-than-its-listener": ({}, {10: 1500.0}, frozenset(), "started after"),
    "an-end-whose-process-holds-no-listener": ({"listener_pid": 11}, {10: 900.0, 11: 900.0}, frozenset(),
                                               "no listening socket"),
    "a-listener-without-a-creation-time": ({"listener_created": None}, {10: 900.0}, frozenset(), "no creation time"),
    "a-process-that-changes-while-read": ({}, {10: 900.0}, frozenset({10}), "changed while"),
    "a-process-whose-start-cannot-be-read": ({}, {}, frozenset(), "when process 10 started"),
}


@pytest.mark.parametrize("case", sorted(UNTIED))
def test_the_windows_check_refuses_a_far_end_it_cannot_tie_to_its_process(monkeypatch, case):
    """Windows' TCP table names a socket's process by PID, and Windows reuses PIDs. The far end of the Hub's
    connection counts as its process's only when that process also holds the listening socket on the daemon's port
    and started before that socket was created. Without a creation time the Hub cannot tell, and refuses."""
    rows, started, replaced, phrase = UNTIED[case]
    windows_scenario(monkeypatch, daemon_rows(**rows), accounts=OWNERS, started=started, replaced=replaced)
    text = refusal(ListenerCheck(URL, Owner(account="TOWER\\owner"), platform="win32"))
    assert "not verified" in text and phrase in text and "sent it nothing" in text, text


def test_addresses_compare_across_spellings():
    assert endpoint._same(("::ffff:127.0.0.1", 8100), ("127.0.0.1", 8100))
    assert endpoint._same(("fe80::1%3", 8100), ("fe80::1", 8100))
    assert not endpoint._same(("127.0.0.1", 8100), ("127.0.0.1", 8101))
    assert not endpoint._same(("not an address", 8100), ("127.0.0.1", 8100))
    assert not endpoint._same((), ("127.0.0.1", 8100))


def test_the_checked_backend_has_no_socket_path_and_sleeps_as_httpcore_does():
    backend = endpoint.CheckedBackend(lambda local, remote: None)
    with pytest.raises(EndpointUnverified):
        run(backend.connect_unix_socket("/run/daemon.sock"))
    run(backend.sleep(0))


def test_without_httpxs_pool_the_checked_transport_refuses_to_exist(monkeypatch):
    """The check is installed in httpx's connection pool (`_pool`). An httpx that keeps it elsewhere must stop the
    Hub at construction, never run it without the check."""
    import httpx

    built = httpx.AsyncHTTPTransport.__init__

    def without_pool(self, *args, **kw):
        built(self, *args, **kw)
        del self._pool

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "__init__", without_pool)
    with pytest.raises(RuntimeError, match="without the check"):
        endpoint.DaemonTransport(httpx.create_ssl_context(), check=lambda local, remote: None)
    with pytest.raises(RuntimeError, match="without the check"):
        DaemonClient(URL)
    endpoint.DaemonTransport(httpx.create_ssl_context())  # no check to install: https or the opt-in


def test_a_refusal_is_said_as_it_is_and_anything_else_with_the_callers_words():
    assert unanswered(DaemonUnverified("the daemon URL is not verified: synthetic"), "fallback") == (
        "the daemon URL is not verified: synthetic")
    assert unanswered(DaemonUnreachable("ConnectError", sent=False), "fallback") == "fallback"


# --- the owner check fails closed on an unmapped or overflow uid ------------------------------------------------------

def overflow_uid() -> int:
    """The kernel's overflow uid (its default where the setting cannot be read)."""
    try:
        return int(Path("/proc/sys/kernel/overflowuid").read_text(encoding="ascii"))
    except (OSError, ValueError):
        return 65534


@functools.cache
def unmapped_user_namespace() -> bool:
    """Whether this machine lets an unprivileged process run in a new user namespace with no uid mapping."""
    if sys.platform != "linux" or not shutil.which("unshare"):
        return False
    try:
        done = subprocess.run(["unshare", "--user", "cat", "/proc/self/uid_map"], capture_output=True, text=True,
                              timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return done.returncode == 0 and done.stdout.strip() == ""


# One owner API read with the Hub's own client and its default owner, printed as JSON: whether it was sent, and the
# refusal when it was not.
UNMAPPED_PROBE = r"""
import asyncio, json, os, sys
from harness_hub.daemon import DaemonClient, DaemonUnreachable

async def read(url, key):
    client = DaemonClient(url)
    try:
        reply = await client.admin(key)
        return {"sent": True, "status": reply.status}
    except DaemonUnreachable as exc:
        return {"sent": exc.sent, "refusal": str(exc)}
    finally:
        await client.aclose()

print(json.dumps({"euid": os.geteuid(), **asyncio.run(read(sys.argv[1], sys.argv[2]))}))
"""


def test_owner_check_fails_closed_on_an_unmapped_uid():
    """A Hub whose own uid has no mapping in its user namespace sends its daemon nothing, and says why."""
    if not unmapped_user_namespace():
        pytest.skip("unprivileged user namespaces are unavailable here")
    with Listener() as listener:
        done = subprocess.run(["unshare", "--user", sys.executable, "-I", "-c", UNMAPPED_PROBE, listener.url, KEY],
                              capture_output=True, text=True, timeout=120, check=False)
        assert done.returncode == 0, done.stderr
        result = json.loads(done.stdout)
        wait_for_heads(listener, 1)
    assert result["euid"] == overflow_uid(), result  # the namespace really has no mapping for this account
    assert listener.heads == [b""] * len(listener.heads) and listener.authorizations() == [], listener.heads
    assert result["sent"] is False and "not verified" in result["refusal"], result
    assert "unmapped" in result["refusal"] or "overflow" in result["refusal"], result


def test_owner_check_fails_closed_on_the_overflow_uid():
    """An expected owner that is the overflow uid matches no socket, even one the table lists with that uid."""
    overflow = overflow_uid()
    check = ListenerCheck(URL, Owner(uid=overflow), platform="linux", proc=lambda local, remote: [overflow])
    with pytest.raises(EndpointUnverified) as caught:
        check(*CONNECTION)
    assert "overflow" in str(caught.value)


@pytest.mark.parametrize("owner", ["nobody", str(overflow_uid())])
def test_daemon_owner_refuses_nobody_and_the_overflow_uid(tmp_path, monkeypatch, owner):
    monkeypatch.setattr("uvicorn.run", lambda app, **kw: None)  # a Hub that starts must not block this test
    with pytest.raises(ValueError, match="overflow uid|DOMAIN"):
        Settings(daemon_owner=owner)
    with pytest.raises(SystemExit):
        cli.main(["--daemon-owner", owner, "--state-dir", str(tmp_path)])


def test_the_owner_check_refuses_each_unmapped_or_overflow_case_before_reading_the_table():
    def table(local, remote):
        raise AssertionError("the table is not read when the owner cannot be singled out")

    own = refusal(linux_check(table, me=65534, unmapped=frozenset({65534})))
    assert "own uid is unmapped" in own and "overflow uid is 65534" in own and "sends it nothing" in own
    expected = refusal(linux_check(table, owner_uid=2000, unmapped=frozenset({2000})))
    assert "uid 2000, is unmapped" in expected and "--daemon-owner" in expected
    overflow = refusal(linux_check(table, owner_uid=70000, overflow=70000))
    assert "uid 70000, the kernel's overflow uid" in overflow
    assert refusal(ListenerCheck(URL, Owner(), platform="linux", proc=table, me=lambda: 1000,
                                 mapped=lambda uid: True)).startswith("the daemon URL")  # no expected uid at all
    linux_check([1000])(*CONNECTION)  # a mapped Hub expecting a mapped owner still passes


def test_the_namespace_readers_parse_the_kernels_files(tmp_path, monkeypatch):
    uid_map, overflow = tmp_path / "uid_map", tmp_path / "overflowuid"
    monkeypatch.setattr(endpoint, "UID_MAP_FILE", str(uid_map))
    monkeypatch.setattr(endpoint, "OVERFLOW_UID_FILE", str(overflow))
    uid_map.write_text("         0          0 4294967295\n", encoding="ascii")  # the initial namespace
    assert endpoint.uid_mapped(0) and endpoint.uid_mapped(1000) and endpoint.uid_mapped(4294967294)
    uid_map.write_text("0 1000 1\n1 100000 65536\n", encoding="ascii")  # a rootless container
    assert endpoint.uid_mapped(0) and endpoint.uid_mapped(65536) and not endpoint.uid_mapped(65537)
    uid_map.write_text("", encoding="ascii")  # a namespace whose map was never written
    assert not endpoint.uid_mapped(65534)
    uid_map.unlink()
    assert not endpoint.uid_mapped(0)  # an unreadable map counts as no mapping
    overflow.write_text("70000\n", encoding="ascii")
    assert endpoint.overflow_uid() == 70000
    overflow.write_text("not a number", encoding="ascii")
    assert endpoint.overflow_uid() == endpoint.DEFAULT_OVERFLOW_UID
    monkeypatch.delattr(os, "geteuid", raising=False)
    assert endpoint.own_uid() == -1


def test_daemon_owner_refuses_an_account_whose_uid_is_the_overflow_uid(linux_accounts, monkeypatch):
    monkeypatch.setattr(FakePwd, "getpwnam", staticmethod(
        lambda name: SimpleNamespace(pw_uid={"owner": 1000, "nfsnobody": 65534}[name])))
    monkeypatch.setattr(endpoint, "overflow_uid", lambda: 65534)
    with pytest.raises(ValueError, match="overflow uid"):
        resolve_owner("nfsnobody", "linux")
    assert resolve_owner("owner", "linux") == Owner(uid=1000)


def test_the_startup_notice_is_an_error_when_the_owner_cannot_be_singled_out():
    level, text = identity_notice(URL, linux_check([1000], me=65534, unmapped=frozenset({65534})),
                                  allow_insecure_http=False)
    assert level == logging.ERROR and "own uid is unmapped" in text
