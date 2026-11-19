"""The daemon endpoint's identity is verified before the Hub sends it anything (#264).

A loopback address limits who can reach the daemon's port, not who can listen on it. The Hub checks, on each new
connection to a loopback http:// daemon and before writing a byte, that the socket at the other end belongs to the
daemon's account (harness_hub/endpoint.py). These tests cover:

- reading the connection table as the kernel prints it, and finding the other end of a real connection;
- the check's verdict for each lookup result, and the error it reports;
- `--daemon-owner` and `--allow-insecure-http`, and the startup notice each gives;
- real sockets on this machine: an endpoint whose listener is not the expected account's is sent nothing, by every
  kind of call and by calls sent in parallel, while the right one works and the opt-in keeps the old behaviour;
- a listener that really belongs to another account (`nobody`, through `sudo -n`): those tests need Linux and
  passwordless sudo (a GitHub-hosted runner has it) and are skipped elsewhere.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import functools
import json
import logging
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from harness_hub import __main__ as cli
from harness_hub import endpoint
from harness_hub.config import Settings
from harness_hub.daemon import DaemonClient, DaemonUnverified
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


# --- the connection table (Linux) -------------------------------------------------------------------------------------

LITTLE_ENDIAN = sys.byteorder == "little"
HEADER = "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"
TAIL = "00000000:00000000 00:00000000 00000000"


def row(n: int, local: str, remote: str, state: str, uid: int) -> str:
    return f"{n:4d}: {local} {remote} {state} {TAIL} {uid:5d}        0 {1000 + n} 1 0000000000000000 20 4 30 10 -1\n"


def tables(tmp_path: Path, tcp: list[str], tcp6: list[str] | None) -> list[str]:
    (tmp_path / "tcp").write_text(HEADER + "".join(tcp), encoding="ascii")
    if tcp6 is not None:
        (tmp_path / "tcp6").write_text(HEADER + "".join(tcp6), encoding="ascii")
    return [str(tmp_path / "tcp"), str(tmp_path / "tcp6")]


# How Linux prints 127.0.0.1, ::1 and ::ffff:127.0.0.1 on a little-endian machine, and ports 8100 and 40000.
V4_LOOPBACK, V6_LOOPBACK = "0100007F", "00000000000000000000000001000000"
MAPPED_LOOPBACK = "0000000000000000FFFF00000100007F"
DAEMON, CLIENT = "1FA4", "9C40"
CONNECTION = (("127.0.0.1", 40000), ("127.0.0.1", 8100))  # the Hub's address, the daemon's


@pytest.mark.skipif(not LITTLE_ENDIAN, reason="the table rows below are written as a little-endian kernel prints them")
def test_the_far_end_is_read_from_the_table_as_the_kernel_prints_it(tmp_path):
    paths = tables(tmp_path, [
        row(0, f"{V4_LOOPBACK}:{DAEMON}", "00000000:0000", "0A", 1000),  # the listener: not this connection
        row(1, f"{V4_LOOPBACK}:{DAEMON}", f"{V4_LOOPBACK}:{CLIENT}", "01", 1000),  # the daemon's end
        row(2, f"{V4_LOOPBACK}:{CLIENT}", f"{V4_LOOPBACK}:{DAEMON}", "01", 2000),  # the Hub's own end
        row(3, f"{V4_LOOPBACK}:{DAEMON}", f"{V4_LOOPBACK}:{CLIENT}", "06", 0),  # TIME_WAIT lists uid 0
    ], [])
    assert proc_owners(*CONNECTION, tables=paths) == [1000]


@pytest.mark.skipif(not LITTLE_ENDIAN, reason="the table rows below are written as a little-endian kernel prints them")
def test_a_not_yet_accepted_or_dual_stack_far_end_is_found(tmp_path):
    paths = tables(tmp_path, [row(0, f"{V4_LOOPBACK}:{DAEMON}", f"{V4_LOOPBACK}:{CLIENT}", "03", 1001)],
                   [row(0, f"{MAPPED_LOOPBACK}:{DAEMON}", f"{MAPPED_LOOPBACK}:{CLIENT}", "01", 1002),
                    row(1, f"{V6_LOOPBACK}:{DAEMON}", f"{V6_LOOPBACK}:{CLIENT}", "01", 1003)])
    assert proc_owners(*CONNECTION, tables=paths) == [1001, 1002]
    assert proc_owners(("::1", 40000), ("::1", 8100), tables=paths) == [1003]
    assert proc_owners(("::ffff:127.0.0.1", 40000), ("127.0.0.1", 8100), tables=paths) == [1001, 1002]


def test_a_kernel_without_ipv6_is_read_and_one_without_the_table_raises(tmp_path):
    paths = tables(tmp_path, [], None)
    assert proc_owners(*CONNECTION, tables=paths) == []
    with pytest.raises(FileNotFoundError):
        proc_owners(*CONNECTION, tables=[str(tmp_path / "missing")])


def connect(family: socket.AddressFamily, server_host: str, client_host: str):
    server = socket.socket(family, socket.SOCK_STREAM)
    if family == socket.AF_INET6:
        server.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
    server.bind((server_host, 0))
    server.listen()
    client = socket.create_connection((client_host, server.getsockname()[1]))
    return server, client


def ipv6_loopback() -> bool:
    with contextlib.suppress(OSError), socket.create_server(("::1", 0), family=socket.AF_INET6):
        return True
    return False


@pytest.mark.skipif(sys.platform != "linux", reason="reads Linux's /proc connection table")
@pytest.mark.parametrize(("family", "server_host", "client_host"), [
    (socket.AF_INET, "127.0.0.1", "127.0.0.1"),
    pytest.param(socket.AF_INET6, "::1", "::1", marks=pytest.mark.skipif(not ipv6_loopback(), reason="no ::1")),
    pytest.param(socket.AF_INET6, "::", "127.0.0.1", marks=pytest.mark.skipif(not ipv6_loopback(), reason="no ::1")),
])
def test_the_far_end_of_a_real_connection_is_found_before_and_after_accept(family, server_host, client_host):
    server, client = connect(family, server_host, client_host)
    accepted = None
    try:
        assert proc_owners(client.getsockname(), client.getpeername()) == [os.geteuid()]
        accepted, _ = server.accept()
        assert proc_owners(client.getsockname(), client.getpeername()) == [os.geteuid()]
    finally:
        for sock in (accepted, client, server):
            if sock is not None:
                sock.close()


# --- the check's verdict ----------------------------------------------------------------------------------------------

URL = "http://127.0.0.1:8100"


def linux_check(found, owner_uid: int = 1000) -> ListenerCheck:
    lookup = found if callable(found) else (lambda local, remote: list(found))
    return ListenerCheck(URL, Owner(uid=owner_uid), platform="linux", proc=lookup)


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


def test_which_urls_are_checked():
    assert isinstance(listener_check("http://127.0.0.1:8100", platform="linux"), ListenerCheck)
    assert isinstance(listener_check("http://[::1]:8100", platform="linux"), ListenerCheck)
    assert isinstance(listener_check("http://localhost:8100", platform="darwin"), ListenerCheck)
    assert listener_check("https://daemon.example.ts.net", platform="linux") is None
    assert listener_check("http://127.0.0.1:8100", allow_insecure_http=True, platform="linux") is None


# --- --daemon-owner and --allow-insecure-http -------------------------------------------------------------------------

def other_owner() -> str:
    """An account that is not the one running the tests, in the form --daemon-owner takes on this platform."""
    return "NOWHERE\\hub-test-other" if sys.platform == "win32" else str(os.geteuid() + 4242)


@pytest.mark.skipif(sys.platform != "linux", reason="Linux account names")
def test_a_linux_owner_is_a_user_name_or_a_uid():
    import pwd

    me = pwd.getpwuid(os.geteuid()).pw_name
    assert resolve_owner(me, "linux") == Owner(uid=os.geteuid()) == resolve_owner("", "linux")
    assert resolve_owner("4242", "linux") == Owner(uid=4242)
    with pytest.raises(ValueError, match="no account named"):
        resolve_owner("no-such-account-hub264", "linux")


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
    if endpoint.supported():
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
    assert level == logging.INFO and "nothing until the listener there is verified" in text


# --- real sockets on this machine -------------------------------------------------------------------------------------

needs_check = pytest.mark.skipif(not endpoint.supported(), reason="this platform has no listener check")


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


@needs_check
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


@needs_check
@pytest.mark.parametrize("parallel", [False, True])
def test_the_daemons_listener_is_verified_on_each_connection(parallel):
    """The same calls to a listener of the expected account (this one): each connection is checked, and each call
    reaches the listener (a stub that answers 502 and closes, so every call opens a new connection)."""
    with Listener() as listener:
        client = DaemonClient(listener.url)
        replies = run(calls(client, parallel=parallel))
        assert all(getattr(reply, "status", None) == 502 for reply in replies), replies
        wait_for_heads(listener, len(replies))
        assert client.check.checks == len(listener.heads) == len(replies)
        assert all(head.startswith((b"GET ", b"POST ", b"PUT ", b"PATCH ", b"DELETE ")) for head in listener.heads)


def test_the_opt_in_sends_without_a_check():
    with Listener() as listener:
        client = DaemonClient(listener.url, allow_insecure_http=True)
        assert client.check is None
        replies = run(calls(client, parallel=False))
        assert all(getattr(reply, "status", None) == 502 for reply in replies)
        wait_for_heads(listener, len(replies))
        assert len(listener.authorizations()) == 6  # every call with the Hub key went out, as before this change


@contextlib.contextmanager
def hub_on(tmp_path: Path, url: str, **settings):
    config = Settings(daemon_url=url, state_dir=tmp_path / "state", poll_seconds=0.02, **settings)
    with TestClient(create_app(config), base_url=f"http://{HUB_HOST}") as client:
        yield HubClient(client, config.state_dir)


@needs_check
def test_overview_reports_an_unverified_listener_and_sends_it_nothing(tmp_path):
    """Overview with a stored key, against a listener that is not the expected account's: its reads go out
    together, and none of them is written on a connection; Overview says why and the key is kept."""
    with Listener() as listener:
        store_key(tmp_path / "state", listener.url)
        with hub_on(tmp_path, listener.url, daemon_owner=other_owner()) as hub:
            hub.sign_in()
            body = hub.get("/api/overview").json()
            started = hub.post("/api/claim")
        assert "not verified" in body["error"] and "--daemon-owner" in body["error"], body["error"]
        assert body["answered"] is False and body["claim"]["state"] == "claimed"
        assert started.status_code == 409  # already claimed: no claim request either
        assert listener.heads and all(head == b"" for head in listener.heads), listener.heads
    assert StateStore(tmp_path / "state").get("key")["token"] == KEY
    assert_no_secrets(hub.bodies, KEY)


@needs_check
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


@needs_check
def test_the_default_verifies_and_works_end_to_end_with_a_daemon_of_this_account(tmp_path, caplog):
    daemon = FakeDaemon()
    with serving(daemon.app) as url:
        with caplog.at_level(logging.INFO), real_hub(tmp_path, url) as hub:
            hub.sign_in()
            started = hub.post("/api/claim").json()
            daemon.approve(started["request_id"])
            hub.wait_for("claimed")
            body = hub.get("/api/overview").json()
            check = hub.client.app.fastapi.state.hub.daemon.check
        assert body["error"] == "" and body["ownership"] is not None
        assert check is not None and check.checks >= 1 and check.owner == resolve_owner("")
    assert any("nothing until the listener there is verified" in r.getMessage() for r in caplog.records)
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
    if endpoint.supported():
        assert check_name(URL) == LISTENER_OWNER


@needs_check
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
