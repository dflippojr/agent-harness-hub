"""The daemon endpoint's identity is verified before the Hub sends it anything (#264).

A loopback address limits who can reach the daemon's port, not who can listen on it. The Hub checks, on each new
connection to a loopback http:// daemon and before writing a byte, that the socket at the other end belongs to the
daemon's account (harness_hub/endpoint.py). These tests cover:

- reading the connection table as the kernel prints it, and finding the other end of a real connection;
- the check's verdict for each lookup result, and the error it reports;
- `--daemon-owner` and `--allow-insecure-http`, and the startup notice each gives;
- real sockets on this machine: an endpoint whose listener is not the expected account's is sent nothing, by every
  kind of call and by calls sent in parallel, while the right one works and the opt-in keeps the old behaviour.

A platform without a lookup (macOS, the BSDs) must refuse every loopback listener, so the real-socket tests expect
exactly that there rather than skip. The two cross-account tests run twice. In `another-expected-account` the listener
is this account's and the Hub is told the daemon runs as another; it runs everywhere. In `another-account` the
listener runs as a real other account, through `sudo -n`. That case is opt-in: set HUB_TEST_OTHER_ACCOUNT to the
account (`nobody` on a machine whose sudo allows it). Only its fixture reaches sudo, when the case runs, never when the
tests are imported. Without the opt-in the case skips and names it; with it, a machine that cannot start the listener
as that account fails it. A real socket and the platform's real lookup decide either way. The unmapped-uid test skips
where unprivileged user namespaces are unavailable.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import ctypes
import errno
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
OTHER_ACCOUNT_ENV = "HUB_TEST_OTHER_ACCOUNT"  # opt-in: the real other account the another-account listener runs as
SYSTEM_PYTHON = "/usr/bin/python3"
ROOT = Path(__file__).resolve().parents[1]
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


def sudo_problem(account: str) -> str:
    """Why this machine cannot start the listener as `account` through `sudo -n`, or ''."""
    if sys.platform != "linux" or not shutil.which("sudo") or not Path(SYSTEM_PYTHON).is_file():
        return f"a listener as another account needs Linux, sudo and {SYSTEM_PYTHON}"
    try:
        done = subprocess.run(["sudo", "-n", "-u", account, SYSTEM_PYTHON, "-I", "-c", "pass"],
                              capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"sudo -n -u {account} did not run: {exc}"
    return "" if done.returncode == 0 else f"sudo -n -u {account} exited {done.returncode}: {done.stderr.strip()}"


@pytest.fixture
def other_account() -> str:
    """The real other account the `another-account` listener runs as. Opt-in through HUB_TEST_OTHER_ACCOUNT, and
    reached through sudo only here, when a case that needs it runs: importing the tests never does."""
    account = os.environ.get(OTHER_ACCOUNT_ENV, "").strip()
    if not account:
        pytest.skip(f"opt-in: set {OTHER_ACCOUNT_ENV} to an account sudo -n can run {SYSTEM_PYTHON} as (such as "
                    f"{OTHER_ACCOUNT}) to run this listener as that account")
    problem = sudo_problem(account)
    assert not problem, f"{OTHER_ACCOUNT_ENV}={account}: {problem}"
    return account


def other_owner() -> str:
    """An account that is not the one running the tests, in the form --daemon-owner takes on this platform."""
    return "NOWHERE\\hub-test-other" if sys.platform == "win32" else str(os.geteuid() + 4242)


# Whether this platform has a listener lookup (Linux, Windows). Without one, every loopback listener is refused.
LOOKUP = endpoint.supported()


class Squatter:
    """A listener of another local account on a loopback port, recording what each connection sent it."""

    def __init__(self, account: str):
        self.received: list[bytes] = []
        self.proc = subprocess.Popen(["sudo", "-n", "-u", account, SYSTEM_PYTHON, "-I", "-c", SQUATTER],
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


@pytest.fixture(params=["another-account", "another-expected-account"])
def foreign(request):
    """A listener the Hub must not trust, made foreign as the test id says (see the module docstring)."""
    if request.param == "another-account":
        account = request.getfixturevalue("other_account")
        with Squatter(account) as squatter:
            yield Foreign(squatter.url, squatter.received, {}, account)
    else:
        with Listener() as listener:
            yield Foreign(listener.url, listener.heads, {"daemon_owner": other_owner()}, "")


# Runs pytest with an audit hook that records every process Python launches, and writes them to argv[1] as JSON.
LAUNCH_TRIPWIRE = r"""
import json, sys
launched = []

def hook(event, args):
    if event in ("subprocess.Popen", "os.system", "os.exec", "os.posix_spawn", "os.spawn", "os.startfile"):
        launched.append(repr(args))

sys.addaudithook(hook)
import pytest

code = pytest.main(sys.argv[2:])
with open(sys.argv[1], "w", encoding="utf-8") as out:
    json.dump(launched, out)
sys.exit(code)
"""


def test_importing_the_tests_never_reaches_sudo(tmp_path):
    """The real other account is opt-in (HUB_TEST_OTHER_ACCOUNT), and only a test that needs it reaches sudo, when
    it runs. Importing these tests, and running the another-account cases without the opt-in, launch no sudo: those
    cases skip and name the opt-in."""
    launched = tmp_path / "launched.json"
    env = {name: value for name, value in os.environ.items() if name != OTHER_ACCOUNT_ENV}
    done = subprocess.run([sys.executable, "-I", "-c", LAUNCH_TRIPWIRE, str(launched), "-q", "-rs", "-p",
                           "no:cacheprovider", f"--basetemp={tmp_path / 'inner'}", "-k", "another-account", __file__],
                          cwd=ROOT, env=env, capture_output=True, text=True, timeout=300, check=False)
    output = done.stdout + done.stderr
    sudo = [call for call in json.loads(launched.read_text(encoding="utf-8")) if "sudo" in call]
    assert sudo == [], sudo
    assert done.returncode == 0 and "2 skipped" in output and OTHER_ACCOUNT_ENV in output, output


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


def test_a_listener_of_another_account_gets_no_key_from_the_parallel_overview_reads(tmp_path, foreign):
    """The default plain-HTTP loopback URL, no opt-in, and a listener of another account on it: Overview's reads
    (the public health read and the owner API reads with the key, sent together) reach it with nothing, and Overview
    reports why."""
    listener = foreign
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


def test_a_listener_of_another_account_gets_no_claim_verifier(tmp_path, foreign):
    """A pending claim's redeem carries the PKCE verifier, which would let its holder take the Hub key: a listener of
    another account never receives it."""
    listener = foreign
    store_pending_claim(tmp_path / "state", listener.url)
    with hub_on(tmp_path, listener.url, **listener.settings):
        wait_until(lambda: len(listener.received) >= 1)
        time.sleep(0.3)  # the poller retries an unanswered redeem every poll_seconds
    sent = b"".join(listener.received)
    assert VERIFIER.encode() not in sent and b"code_verifier" not in sent
    assert all(data == b"" for data in listener.received), listener.received


# --- the connection table (Linux) -------------------------------------------------------------------------------------

HEADER = "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"
HEADER6 = ("  sl  local_address                         remote_address                        st tx_queue rx_queue tr "
           "tm->when retrnsmt   uid  timeout inode\n")  # tcp6 names its third column remote_address
TAIL = "00000000:00000000 00:00000000 00000000"


def row(n: int, local: str, remote: str, state: str, uid: int) -> str:
    return f"{n:4d}: {local} {remote} {state} {TAIL} {uid:5d}        0 {1000 + n} 1 0000000000000000 20 4 30 10 -1\n"


def time_wait_row(n: int, local: str, remote: str) -> str:
    """A time-wait socket as get_timewait*_sock prints it: 12 fields, timer code 3, uid 0."""
    return f"{n:4d}: {local} {remote} 06 00000000:00000000 03:00001770 00000000     0        0 0 3 0000000000000000\n"


def tables(tmp_path: Path, tcp: list[str], tcp6: list[str] | None) -> list[str]:
    (tmp_path / "tcp").write_text(HEADER + "".join(tcp), encoding="ascii")
    if tcp6 is not None:
        (tmp_path / "tcp6").write_text(HEADER6 + "".join(tcp6), encoding="ascii")
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
        time_wait_row(3, DAEMON, HUB),  # TIME_WAIT, in the short shape, lists uid 0
    ], [])
    assert proc_owners(*CONNECTION, tables=paths) == [1000]


def test_a_not_yet_accepted_or_dual_stack_far_end_is_found(tmp_path):
    paths = tables(tmp_path, [row(0, DAEMON, HUB, "03", 1001)],
                   [row(0, kernel("::ffff:127.0.0.1", 8100), kernel("::ffff:127.0.0.1", 40000), "01", 1002),
                    row(1, kernel("::1", 8100), kernel("::1", 40000), "01", 1003)])
    assert proc_owners(*CONNECTION, tables=paths) == [1001, 1002]
    assert proc_owners(("::1", 40000), ("::1", 8100), tables=paths) == [1003]
    assert proc_owners(("::ffff:127.0.0.1", 40000), ("127.0.0.1", 8100), tables=paths) == [1001, 1002]


def test_a_kernel_without_ipv6_is_read_and_one_without_the_table_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(endpoint, "ipv6_available", lambda: False, raising=False)  # no AF_INET6 sockets at all
    paths = tables(tmp_path, [], None)
    assert proc_owners(*CONNECTION, tables=paths) == []
    with pytest.raises(FileNotFoundError):
        proc_owners(*CONNECTION, tables=[str(tmp_path / "missing")])


def lookup(local, remote) -> list:
    """What this platform's own lookup finds at the other end of a connection: uids on Linux, accounts on Windows.
    A Windows socket it cannot tie to its process fails with the table's rows on that port, to show why."""
    if sys.platform == "linux":
        return proc_owners(local, remote)
    if sys.platform == "win32":
        try:
            return endpoint.windows_owners(local, remote)
        except endpoint.Unattributed as exc:
            raise AssertionError(f"{exc}; {windows_rows_on(int(remote[1]))}") from exc
    return []


def windows_rows_on(port: int) -> str:
    """Windows' TCP table on `port`, and when each of its processes started, for a failure message."""
    import psutil

    rows = [row for row in endpoint.windows_rows() if port in (row.local[1], row.remote[1])]
    started = {}
    for pid in {row.pid for row in rows}:
        try:
            started[pid] = psutil.Process(pid).create_time()
        except psutil.Error as exc:
            started[pid] = repr(exc)
    return f"rows on port {port}: {rows}; process starts: {started}"


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
    own: object = "TOWER\\owner"  # the Hub's own account, or the exception reading it raises
    alive: dict[int, object] = {}  # what is_running answers for a PID (True if not listed), or the exception it raises

    def __init__(self, pid: int | None = None):
        self.pid = pid

    def username(self) -> str:
        import psutil

        if self.pid is None:
            if isinstance(self.own, Exception):
                raise self.own
            return self.own
        if self.pid not in self.accounts:
            raise psutil.AccessDenied(self.pid)
        return self.accounts[self.pid]

    def create_time(self) -> float:
        import psutil

        if self.pid not in self.started:
            raise psutil.AccessDenied(self.pid)
        return self.started[self.pid]

    def is_running(self) -> bool:
        answer = False if self.pid in self.replaced else self.alive.get(self.pid, True)
        if isinstance(answer, Exception):
            raise answer
        return answer


# Windows' own TCP table (GetExtendedTcpTable, TCP_TABLE_OWNER_MODULE_ALL), faked with rows written as Windows writes
# them. psutil's table (TCP_TABLE_OWNER_PID_ALL, which has no creation times) is faked with the same rows, so a lookup
# that read it instead would fail these tests too.

FILETIME_UNIX_EPOCH = 116444736000000000  # 1970-01-01 as a FILETIME (100 ns units since 1601)
WINDOWS_STATES = {"LISTEN": 2, "SYN_RECV": 4, "ESTABLISHED": 5, "TIME_WAIT": 11,  # MIB_TCP_STATE
                  "UNDEFINED": 99}  # a state Windows does not define
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
    filetime: int | None = None  # the creation time as a raw FILETIME, in place of `created`


def windows_table(rows: list[WinRow], version: int, high_port_bytes: bytes = b"\0\0") -> bytes:
    """MIB_TCPTABLE_OWNER_MODULE (MIB_TCP6TABLE_OWNER_MODULE for IPv6) as GetExtendedTcpTable writes it: the row
    count, padding to the rows' 8-byte alignment, then the rows. A port is a DWORD in network byte order (the port in
    its low 16 bits), as an IPv4 address is, and the creation time a FILETIME."""

    def port(number: int) -> bytes:  # the upper 16 bits may be uninitialized (MIB_TCPROW_OWNER_PID)
        return struct.pack("!H", number) + high_port_bytes

    packed = []
    for row in rows:
        local, remote = ipaddress.ip_address(row.local[0]), ipaddress.ip_address(row.remote[0])
        if local.version != version:
            continue
        created = FILETIME_UNIX_EPOCH + round(row.created * 10_000_000) if row.created is not None else 0
        created = row.filetime if row.filetime is not None else created
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
        if isinstance(table, list):  # a table that changes between calls: the next one each time, then the last
            table = table.pop(0) if len(table) > 1 else table[0]
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
        FakeConn(row.local, () if row.state == "LISTEN" else row.remote, status.get(row.state, psutil.CONN_NONE),
                 row.pid) for row in rows])
    monkeypatch.setattr(psutil, "Process", FakeProcess)
    monkeypatch.setattr(FakeProcess, "accounts", accounts)
    monkeypatch.setattr(FakeProcess, "started", started)
    monkeypatch.setattr(FakeProcess, "replaced", replaced)
    return get


DAEMON_END, HUB_END, NO_PEER = CONNECTION[1], CONNECTION[0], ("0.0.0.0", 0)  # noqa: S104 - a listener's empty peer
OWNERS = {10: "TOWER\\owner", 11: "TOWER\\owner", 20: "TOWER\\hub"}


def daemon_rows(*, listener_pid: int = 10, end_pid: int = 10, listener_created: float | None = 1000.0,
                end_created: float | None = 2000.0) -> list[WinRow]:
    """The daemon's listener, the daemon's end of the Hub's connection, and the Hub's own end."""
    return [WinRow("LISTEN", DAEMON_END, NO_PEER, listener_pid, listener_created),
            WinRow("ESTABLISHED", DAEMON_END, HUB_END, end_pid, end_created),
            WinRow("ESTABLISHED", HUB_END, DAEMON_END, 20, 2000.0)]


def test_the_windows_check_passes_a_listener_its_own_process_created(monkeypatch):
    windows_scenario(monkeypatch, daemon_rows(), accounts=OWNERS, started={10: 900.0, 20: 950.0})
    ListenerCheck(URL, Owner(account="TOWER\\owner"), platform="win32")(*CONNECTION)


def test_the_windows_lookup_reads_the_far_ends_process_account(monkeypatch):
    """Rows as Windows writes them: the far end of the Hub's connection, accepted or not yet, over IPv4 or a
    dual-stack IPv6 socket, names its process's account, and another user's process is `UNREADABLE`. The listeners,
    the Hub's own end and a closed connection are not that far end. Both tables read are the ones with creation
    times."""
    dual_end, dual_hub = ("::ffff:127.0.0.1", 8100), ("::ffff:127.0.0.1", 40000)
    rows = [WinRow("LISTEN", DAEMON_END, NO_PEER, 10, 1000.0),
            WinRow("LISTEN", ("::", 8100), ("::", 0), 30, 1000.0),
            WinRow("LISTEN", ("0.0.0.0", 8100), NO_PEER, 40, 1000.0),  # noqa: S104 - a row, not a bind
            WinRow("ESTABLISHED", DAEMON_END, HUB_END, 10, 2000.0),  # the daemon's end
            WinRow("ESTABLISHED", HUB_END, DAEMON_END, 20, 2000.0),  # the Hub's own end
            WinRow("TIME_WAIT", DAEMON_END, HUB_END, 0, None),
            WinRow("SYN_RECV", dual_end, dual_hub, 30, 2000.0),  # not accepted yet, on a dual-stack listener
            WinRow("ESTABLISHED", DAEMON_END, HUB_END, 40, 2000.0)]  # another user's: AccessDenied
    get = windows_scenario(monkeypatch, rows, accounts={10: "TOWER\\owner", 20: "TOWER\\hub", 30: "TOWER\\dual"},
                           started={10: 900.0, 30: 900.0, 40: 900.0})
    assert endpoint.windows_owners(HUB_END, DAEMON_END) == ["TOWER\\owner", UNREADABLE, "TOWER\\dual"]
    assert get.calls == [(2, 8), (2, 8), (23, 8), (23, 8)]  # each family sized, then filled: TCP_TABLE_OWNER_MODULE_ALL
    assert endpoint.windows_account() == "TOWER\\owner"
    assert resolve_owner("", "win32") == Owner(account="TOWER\\owner")


def test_the_windows_table_is_read_as_windows_writes_it(monkeypatch):
    """A table that grows between the call that sizes it and the one that fills it is read again. A machine without
    IPv6 has an empty IPv6 table. Any other failure, or a table cut short, raises, and the check refuses."""
    listener = daemon_rows()[0]
    table = windows_table(daemon_rows(), 4)
    assert len(table) == 8 + 3 * 160
    assert len(windows_table([WinRow("LISTEN", ("::1", 8100), ("::", 0), 1, 1.0)], 6)) == 8 + 192
    assert endpoint.parse_tcp_table(table, 4)[0] == endpoint.TcpRow(
        2, DAEMON_END, NO_PEER, 10, FILETIME_UNIX_EPOCH + 1000 * 10_000_000)
    assert endpoint.parse_tcp_table(bytes(4), 6) == []
    with pytest.raises(ValueError):
        endpoint.parse_tcp_table(table[:-1], 4)

    windows_scenario(monkeypatch, [], accounts={}, started={})
    monkeypatch.setattr(endpoint, "ipv6_available", lambda: False)  # a machine without IPv6: no AF_INET6 sockets
    get = FakeGetExtendedTcpTable({2: [windows_table([listener], 4), table], 23: 50})  # 50: ERROR_NOT_SUPPORTED
    monkeypatch.setattr(ctypes, "WinDLL", lambda name, **kw: SimpleNamespace(GetExtendedTcpTable=get), raising=False)
    assert [row.pid for row in endpoint.windows_rows()] == [10, 10, 20]
    assert get.calls == [(2, 8), (2, 8), (2, 8), (23, 8)]  # sized, too small once it grew, filled; no IPv6

    get.tables = {2: [windows_table([listener] * count, 4) for count in range(1, 12)], 23: 50}
    with pytest.raises(OSError, match="kept growing"):
        endpoint.windows_tcp_table(4)
    get.tables = {2: 50, 23: 50}  # an IPv4 table Windows does not give
    with pytest.raises(OSError, match="error 50"):
        endpoint.windows_tcp_table(4)
    get.tables = {2: 5, 23: 5}  # ERROR_ACCESS_DENIED
    assert "could not read the connection table" in refusal(ListenerCheck(URL, Owner(account="TOWER\\owner"),
                                                                          platform="win32"))


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


# When a socket the check relies on was created, against its process's start (900.0): after it, before it, or with no
# creation time.
CREATED = {"after": 1000.0, "before": 800.0, "none": None}
GRID = [(far_end, listener) for far_end in CREATED for listener in CREATED]
# What a refusal says about each faulty socket: the phrase, and the socket it names.
FAULTS = {("far end", "before"): ("started after", "the socket at the other end"),
          ("far end", "none"): ("no creation time", "the socket at the other end"),
          ("listener", "before"): ("started after", "the listening socket"),
          ("listener", "none"): ("no creation time", "the listening socket")}


@pytest.mark.parametrize(("far_end", "listener"), GRID,
                         ids=[f"far-end-{far_end}-listener-{listener}" for far_end, listener in GRID])
def test_the_windows_check_passes_only_when_its_process_predates_every_socket_it_relies_on(monkeypatch, far_end,
                                                                                            listener):
    """The grid {far end, listener} x {created after the process started, before, no creation time}. The far end
    counts as its process's only when that process started before every socket the check relies on was created: the
    far end itself and the listener with its PID. Any other cell is refused, naming a faulty socket."""
    rows = daemon_rows(listener_created=CREATED[listener], end_created=CREATED[far_end])
    windows_scenario(monkeypatch, rows, accounts=OWNERS, started={10: 900.0, 20: 950.0})
    check = ListenerCheck(URL, Owner(account="TOWER\\owner"), platform="win32")
    if far_end == listener == "after":
        check(*CONNECTION)
        return
    text = refusal(check)
    assert "not verified" in text and "sent it nothing" in text, text
    faults = [FAULTS[role, when] for role, when in (("far end", far_end), ("listener", listener))
              if when != "after"]
    assert any(phrase in text and name in text for phrase, name in faults), (faults, text)


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
    assert endpoint.uid_mapped(65534) is False
    uid_map.unlink()
    assert endpoint.uid_mapped(0) is None  # a map that cannot be read: the check refuses
    overflow.write_text("70000\n", encoding="ascii")
    assert endpoint.overflow_uid() == 70000
    overflow.write_text("not a number", encoding="ascii")
    assert endpoint.overflow_uid() is None  # unknown, not the default: the check refuses
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


# --- what the documentation claims ------------------------------------------------------------------------------------

def test_the_docs_list_same_account_forwarders_as_not_covered_and_claim_no_more_than_the_check():
    """The check proves which account holds the socket at the other end, not which program it is or where it sends
    what it gets: README lists same-account forwarders under what it does not cover, and neither README nor the
    module says a connection's other end cannot change after the check."""
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    section = readme.split("\n## Daemon connection\n", 1)[1].split("\n## ", 1)[0]
    not_covered = section.split("**What it does not cover**", 1)[1].split("\n\n", 1)[0]
    assert "same-account forwarders" in not_covered.lower() and "ssh -L" in not_covered, not_covered
    rules = readme.split("\n## Security rules\n", 1)[1].split("\n## ", 1)[0]
    for text in (section, rules, endpoint.__doc__):
        assert "cannot change" not in text and "can't change" not in text and "never the key" not in text


# --- every input of the owner check fails closed ----------------------------------------------------------------------

def linux_inputs(tmp_path: Path, monkeypatch) -> dict[str, Path]:
    """Every input the Linux owner check reads, as this test's files and fakes: the daemon (uid 1000) at the other end
    of CONNECTION in tcp, an empty tcp6 on a kernel with IPv6, the overflow uid 65534, a uid map that maps every uid,
    and a Hub of uid 1000. The check passes them as they are."""
    paths = {name: tmp_path / name for name in ("tcp", "tcp6", "overflowuid", "uid_map")}
    paths["tcp"].write_text(HEADER + row(0, DAEMON, HUB, "01", 1000), encoding="ascii")
    paths["tcp6"].write_text(HEADER6, encoding="ascii")
    paths["overflowuid"].write_text("65534\n", encoding="ascii")
    paths["uid_map"].write_text("         0          0 4294967295\n", encoding="ascii")
    monkeypatch.setattr(endpoint, "OVERFLOW_UID_FILE", str(paths["overflowuid"]))
    monkeypatch.setattr(endpoint, "UID_MAP_FILE", str(paths["uid_map"]))
    monkeypatch.setattr(endpoint, "ipv6_available", lambda: True, raising=False)
    monkeypatch.setattr(os, "geteuid", lambda: 1000, raising=False)
    return paths


def linux_input_check(paths: dict[str, Path]) -> ListenerCheck:
    """The Linux check for the daemon's account (uid 1000), with its own readers on those inputs."""
    tables = [str(paths["tcp"]), str(paths["tcp6"])]
    return ListenerCheck(URL, Owner(uid=1000), platform="linux",
                         proc=lambda local, remote: proc_owners(local, remote, tables=tables))


def windows_inputs(monkeypatch, rows: list[WinRow] | None = None,
                   started: dict[int, float] | None = None) -> FakeGetExtendedTcpTable:
    """Every input the Windows owner check reads, faked: the daemon's process (10, TOWER\\owner, started before its
    sockets) at the other end of CONNECTION and on its listener, on a machine with IPv6. The check passes them."""
    monkeypatch.setattr(endpoint, "ipv6_available", lambda: True, raising=False)
    return windows_scenario(monkeypatch, daemon_rows() if rows is None else rows, accounts=dict(OWNERS),
                            started={10: 900.0, 20: 950.0} if started is None else started)


def windows_input_check() -> ListenerCheck:
    return ListenerCheck(URL, Owner(account="TOWER\\owner"), platform="win32")


def directory(path: Path) -> None:
    """Replace a file with a directory: it is there, and it cannot be read."""
    path.unlink()
    path.mkdir()


def on_linux(change):
    """A cell on Linux: the passing inputs, with one changed by `change(paths, monkeypatch)`."""
    def cell(tmp_path, monkeypatch):
        paths = linux_inputs(tmp_path, monkeypatch)
        change(paths, monkeypatch)
        check = linux_input_check(paths)
        return (lambda: check(*CONNECTION)), EndpointUnverified, "not verified"
    return cell


def on_windows(change=lambda get, monkeypatch: None, *, rows=None, started=None, phrase="not verified"):
    """A cell on Windows: the passing inputs, with one changed by `change(get, monkeypatch)`, `rows` or `started`."""
    def cell(tmp_path, monkeypatch):
        change(windows_inputs(monkeypatch, rows, started), monkeypatch)
        check = windows_input_check()
        return (lambda: check(*CONNECTION)), EndpointUnverified, phrase
    return cell


def at_startup(own):
    """A cell for the Hub's own Windows account, read when the Hub builds its check: the Hub refuses to build it."""
    def cell(tmp_path, monkeypatch):
        windows_inputs(monkeypatch)
        monkeypatch.setattr(FakeProcess, "own", own)
        return (lambda: listener_check(URL, platform="win32")), ValueError, "own account"
    return cell


def addresses(platform: str, local, remote):
    """A cell for the connection's own two addresses, on one platform."""
    def cell(tmp_path, monkeypatch):
        paths = linux_inputs(tmp_path, monkeypatch)
        windows_inputs(monkeypatch)
        check = linux_input_check(paths) if platform == "linux" else windows_input_check()
        return (lambda: check(local, remote)), EndpointUnverified, "not verified"
    return cell


def access_denied(pid: int | None = None) -> Exception:
    import psutil

    return psutil.AccessDenied(pid)


GARBAGE_ROW = "   1: not a row the kernel writes\n"
# Each input the owner check reads, unreadable or unparseable. Every cell starts from inputs the check passes
# (test_the_owner_check_passes_inputs_that_read_and_parse) and changes that one input.
FAIL_CLOSED = {
    "linux-proc-net-tcp-unreadable": on_linux(lambda paths, mp: directory(paths["tcp"])),
    "linux-proc-net-tcp-unparseable": on_linux(lambda paths, mp: paths["tcp"].write_text(
        HEADER + row(0, DAEMON, HUB, "01", 1000) + GARBAGE_ROW, encoding="ascii")),
    "linux-proc-net-tcp6-unreadable": on_linux(lambda paths, mp: paths["tcp6"].unlink()),  # with IPv6 there
    "linux-proc-net-tcp6-unparseable": on_linux(lambda paths, mp: paths["tcp6"].write_text(
        HEADER6 + GARBAGE_ROW, encoding="ascii")),
    "linux-overflowuid-unreadable": on_linux(lambda paths, mp: directory(paths["overflowuid"])),
    "linux-overflowuid-unparseable": on_linux(lambda paths, mp: paths["overflowuid"].write_text(
        "not a number\n", encoding="ascii")),
    "linux-uid-map-unreadable": on_linux(lambda paths, mp: directory(paths["uid_map"])),
    "linux-uid-map-unparseable": on_linux(lambda paths, mp: paths["uid_map"].write_text(
        "0 0 4294967295\nnot a row\n", encoding="ascii")),
    "linux-hub-uid-unreadable": on_linux(lambda paths, mp: mp.delattr(os, "geteuid", raising=False)),
    "linux-hub-uid-unparseable": on_linux(lambda paths, mp: mp.setattr(os, "geteuid", lambda: "not a uid",
                                                                       raising=False)),
    "windows-tcp-table-ipv4-unreadable": on_windows(lambda get, mp: get.tables.update({2: 5})),  # ACCESS_DENIED
    "windows-tcp-table-ipv4-unparseable": on_windows(
        rows=[*daemon_rows(), WinRow("UNDEFINED", ("127.0.0.1", 9), NO_PEER, 30, 1000.0)]),
    "windows-tcp-table-ipv6-unreadable": on_windows(lambda get, mp: get.tables.update({23: 50})),  # NOT_SUPPORTED
    "windows-tcp-table-ipv6-unparseable": on_windows(lambda get, mp: get.tables.update({23: struct.pack("=I4x", 1)})),
    "windows-socket-creation-time-unreadable": on_windows(rows=daemon_rows(end_created=None)),
    "windows-socket-creation-time-unparseable": on_windows(rows=daemon_rows(end_created=-2e10)),  # before 1601
    "windows-process-start-unreadable": on_windows(started={20: 950.0}),
    "windows-process-start-unparseable": on_windows(started={10: float("nan"), 20: 950.0}),
    "windows-process-account-unreadable": on_windows(
        lambda get, mp: mp.setattr(FakeProcess, "accounts", {20: "TOWER\\hub"}), phrase=UNREADABLE),
    "windows-process-account-unparseable": on_windows(
        lambda get, mp: mp.setattr(FakeProcess, "accounts", {10: "owner"}), phrase=UNREADABLE),
    "windows-process-recheck-unreadable": on_windows(lambda get, mp: mp.setattr(FakeProcess, "alive",
                                                                                {10: access_denied(10)})),
    "windows-process-recheck-unparseable": on_windows(lambda get, mp: mp.setattr(FakeProcess, "alive", {10: "yes"})),
    "windows-hub-account-unreadable": at_startup(access_denied()),
    "windows-hub-account-unparseable": at_startup("owner"),
    "windows-psutil-unimportable": on_windows(lambda get, mp: mp.setitem(sys.modules, "psutil", None)),
    "linux-connection-addresses-unreadable": addresses("linux", None, None),
    "linux-connection-addresses-unparseable": addresses("linux", ("not an address", 40000), CONNECTION[1]),
    "windows-connection-addresses-unreadable": addresses("win32", None, None),
    "windows-connection-addresses-unparseable": addresses("win32", ("not an address", 40000), CONNECTION[1]),
}


def test_the_owner_check_passes_inputs_that_read_and_parse(tmp_path, monkeypatch):
    """The inputs every FAIL_CLOSED cell starts from: the check passes them, on Linux and on Windows."""
    linux_input_check(linux_inputs(tmp_path, monkeypatch))(*CONNECTION)
    windows_inputs(monkeypatch)
    windows_input_check()(*CONNECTION)
    assert listener_check(URL, platform="win32").owner == Owner(account="TOWER\\owner")


@pytest.mark.parametrize("cell", list(FAIL_CLOSED))
def test_every_input_of_the_owner_check_fails_closed(tmp_path, monkeypatch, cell):
    """Each input the owner check reads, on Linux and on Windows, unreadable or unparseable: the check refuses the
    connection and sends nothing, or, for the Hub's own account, the Hub refuses to build the check."""
    attempt, refused, phrase = FAIL_CLOSED[cell](tmp_path, monkeypatch)
    with pytest.raises(refused) as caught:
        attempt()
    assert phrase in str(caught.value), str(caught.value)


def test_a_missing_ipv6_table_is_empty_only_where_the_machine_has_no_ipv6(monkeypatch):
    """Only an address family the system does not support means no IPv6 (so no IPv6 or dual-stack socket to list).
    Any other failure of the probe, or a Python that cannot tell, counts as IPv6 being there."""
    class Opened:  # an AF_INET6 socket the system creates
        def __init__(self, *args, **kwargs):
            pass

        def close(self) -> None:
            pass

    monkeypatch.setattr(socket, "socket", Opened)
    assert endpoint.ipv6_available() is True

    def refused(code: int):
        def make(*args, **kwargs):
            raise OSError(code, os.strerror(code))
        return make

    monkeypatch.setattr(socket, "socket", refused(errno.EAFNOSUPPORT))
    assert endpoint.ipv6_available() is False
    monkeypatch.setattr(socket, "socket", refused(10047))  # WSAEAFNOSUPPORT, as Windows reports it
    assert endpoint.ipv6_available() is False
    monkeypatch.setattr(socket, "socket", refused(errno.EMFILE))  # out of file descriptors: cannot tell
    assert endpoint.ipv6_available() is True
    monkeypatch.delattr(socket, "AF_INET6")
    assert endpoint.ipv6_available() is True


def test_a_table_without_the_kernels_header_is_refused(tmp_path):
    for text in ("", "not the header\n", HEADER6):  # tcp with nothing, another header, or tcp6's header
        (tmp_path / "tcp").write_text(text, encoding="ascii")
        with pytest.raises(ValueError, match="header"):
            proc_owners(*CONNECTION, tables=[str(tmp_path / "tcp")])


def test_the_hubs_own_account_and_overflow_uid_are_refused_at_startup_when_they_cannot_be_read(monkeypatch):
    windows_inputs(monkeypatch)
    monkeypatch.setitem(sys.modules, "psutil", None)
    with pytest.raises(ValueError, match="cannot read its own account"):
        endpoint.windows_account()
    monkeypatch.setattr(endpoint, "overflow_uid", lambda: None)
    with pytest.raises(ValueError, match="overflow uid \\(unreadable here\\)"):
        resolve_owner("nobody", "linux")
    assert not endpoint.is_account("TOWER\\owner\\extra") and not endpoint.is_account(None)


# --- every parsed OS record is validated in full ----------------------------------------------------------------------

# The fields of a /proc/net/tcp (or tcp6) row, as the kernel prints them: a request or time-wait socket prints the first
# 12 (get_openreq*, get_timewait*), a full socket all 17 (get_tcp4_sock, get_tcp6_sock).
PROC_FIELDS = ["slot", "local-address", "remote-address", "state", "queues", "timer", "retransmits", "uid", "timeout",
               "inode", "refcount", "pointer", "rto", "ato", "quick-pingpong", "cwnd", "ssthresh"]


def proc_row_fields(table: str, shape: int, *, local: str = "", remote: str = "", state: str = "", uid: str = "1000",
                    slot: str = "0:") -> list[str]:
    """A row of `table` ("tcp" or "tcp6") in one of the kernel's two shapes: 17 fields for a full socket, 12 for a
    request or time-wait socket. By default the daemon's end of CONNECTION (dual-stack in tcp6), owned by uid 1000."""
    host = "::ffff:127.0.0.1" if table == "tcp6" else "127.0.0.1"
    local, remote = local or kernel(host, 8100), remote or kernel(host, 40000)
    if shape == 17:
        return [slot, local, remote, state or "01", "00000000:00000000", "00:00000000", "00000000", uid, "0", "54321",
                "1", "0000000000000000", "20", "4", "0", "10", "-1"]
    state = state or "03"
    if state == "03":  # a request socket: timer code 1, refcount 0
        return [slot, local, remote, state, "00000000:00000000", "01:00000063", "00000000", uid, "0", "0", "0",
                "0000000000000000"]
    return [slot, local, remote, state, "00000000:00000000", "03:00001770", "00000000", uid, "0", "0", "3",
            "0000000000000000"]  # a time-wait socket: timer code 3


def other_row(table: str, shape: int) -> list[str]:
    """A row of the same table and shape that is not the daemon's end of CONNECTION: the daemon's listener (a full
    socket), or a time-wait socket on another of the Hub's ports."""
    six = table == "tcp6"
    host = "::ffff:127.0.0.1" if six else "127.0.0.1"
    if shape == 17:
        return proc_row_fields(table, 17, remote=kernel("::" if six else "0.0.0.0", 0), state="0A", slot="1:")  # noqa: S104
    return proc_row_fields(table, 12, remote=kernel(host, 40001), state="06", uid="0", slot="1:")


def write_table(paths: dict[str, Path], table: str, rows: list[list[str]]) -> None:
    """`rows` as `table`, after its header, with the other table empty."""
    for name, header in (("tcp", HEADER), ("tcp6", HEADER6)):
        body = "".join(" ".join(row) + "\n" for row in rows) if name == table else ""
        paths[name].write_text(header + body, encoding="ascii")


PROC_FAULTS = [(table, shape, field, fault) for table in ("tcp", "tcp6") for shape in (17, 12)
               for field in range(shape) for fault in ("missing", "malformed")]


@pytest.mark.parametrize(("table", "shape"), [(table, shape) for table in ("tcp", "tcp6") for shape in (17, 12)])
def test_both_row_shapes_the_kernel_prints_pass(tmp_path, monkeypatch, table, shape):
    """The far end and another row, in either shape the kernel prints, in either table: the check passes them."""
    paths = linux_inputs(tmp_path, monkeypatch)
    write_table(paths, table, [proc_row_fields(table, shape), other_row(table, shape)])
    linux_input_check(paths)(*CONNECTION)


@pytest.mark.parametrize(("table", "shape", "field", "fault"), PROC_FAULTS,
                         ids=[f"{t}-{shape}-{PROC_FIELDS[field]}-{fault}" for t, shape, field, fault in PROC_FAULTS])
def test_each_field_of_a_proc_net_tcp_row_is_validated(tmp_path, monkeypatch, table, shape, field, fault):
    """Each field of both shapes, missing or malformed, in a row other than the far end's: the table is not in the
    kernel's printed form, so the check refuses even though the far end's own row is valid."""
    bad = other_row(table, shape)
    if fault == "missing":
        del bad[field]
    else:
        bad[field] = "zz"
    paths = linux_inputs(tmp_path, monkeypatch)
    write_table(paths, table, [proc_row_fields(table, shape), bad])
    with pytest.raises(EndpointUnverified, match="not verified"):
        linux_input_check(paths)(*CONNECTION)


def changed(shape: int, field: int, value: str) -> list[str]:
    row = other_row("tcp", shape)
    row[field] = value
    return row


# Rows the kernel never prints, each beside a valid far end in tcp.
PROC_MALFORMED = {
    "undefined-state-FF": changed(17, 3, "FF"),
    "undefined-state-00": changed(17, 3, "00"),
    "full-socket-in-a-request-state-0C": changed(17, 3, "0C"),  # TCP_NEW_SYN_RECV: printed in the 12-field shape
    "short-row-in-an-established-state-01": changed(12, 3, "01"),  # an established socket prints all 17 fields
    "lowercase-hex-address": changed(17, 1, kernel("127.0.0.1", 8100).lower()),
    "uid-with-a-leading-zero": changed(17, 7, "01000"),
    "long-timer-with-a-leading-zero": changed(17, 5, "00:012345678"),
    "pointer-of-12-digits": changed(17, 11, "000000000000"),
    "pointer-of-two-zeros": changed(17, 11, "00"),  # only the literal 0 of upstream 7c0ec6288b49, not any zeros
    "uppercase-pointer": changed(17, 11, "FFFF888012345678"),
    "negative-inode": changed(17, 9, "-1"),
    "queue-of-7-digits": changed(17, 4, "0000000:00000000"),
    "cut-short-after-the-uid": other_row("tcp", 17)[:8],
    "an-extra-field": [*other_row("tcp", 17), "0"],
    "a-blank-row": [],
}


@pytest.mark.parametrize("case", list(PROC_MALFORMED))
def test_a_proc_net_tcp_row_the_kernel_never_prints_is_refused(tmp_path, monkeypatch, case):
    paths = linux_inputs(tmp_path, monkeypatch)
    write_table(paths, "tcp", [proc_row_fields("tcp", 17), PROC_MALFORMED[case]])
    with pytest.raises(EndpointUnverified, match="not verified"):
        linux_input_check(paths)(*CONNECTION)


def map_line(first: int, outside: int, count: int) -> str:
    return f"{first:10d} {outside:10d} {count:10d}\n"  # uid_m_show: "%10u %10u %10u\n"


BASE_MAP = map_line(0, 0, 2000)  # maps the Hub's uid and the daemon's (1000)
# uid maps the kernel never writes (kernel/user_namespace.c, map_write), each after a valid row.
UID_MAP_MALFORMED = {
    "a-value-past-32-bits": BASE_MAP + "      5000       5000 4294967296\n",
    "a-leading-zero": BASE_MAP + "     05000       5000         10\n",
    "an-empty-range": BASE_MAP + map_line(5000, 5000, 0),
    "a-range-past-the-32-bit-space": BASE_MAP + map_line(4294967290, 5000, 10),
    "an-outside-range-past-the-32-bit-space": BASE_MAP + map_line(5000, 4294967290, 10),
    "overlapping-ranges-inside": BASE_MAP + map_line(1500, 9000, 10),
    "overlapping-ranges-outside": BASE_MAP + map_line(5000, 1500, 10),
    "more-than-340-rows": BASE_MAP + "".join(map_line(10000 + 10 * n, 10000 + 10 * n, 1) for n in range(340)),
    "two-fields": BASE_MAP + "      5000       5000\n",
    "four-fields": BASE_MAP + map_line(5000, 5000, 10).rstrip("\n") + "          1\n",
    "a-negative-number": BASE_MAP + "        -1       5000         10\n",
}


def test_a_uid_map_in_the_kernels_form_passes(tmp_path, monkeypatch):
    """The kernel's own maps: the initial namespace's, a rootless container's, and the most rows it allows (340)."""
    for text in (map_line(0, 0, 4294967295), map_line(0, 1000, 1) + map_line(1, 100000, 65536),
                 BASE_MAP + "".join(map_line(10000 + 10 * n, 10000 + 10 * n, 1) for n in range(339))):
        paths = linux_inputs(tmp_path, monkeypatch)
        paths["uid_map"].write_text(text, encoding="ascii")
        linux_input_check(paths)(*CONNECTION)


@pytest.mark.parametrize("case", list(UID_MAP_MALFORMED))
def test_a_uid_map_the_kernel_never_writes_is_refused(tmp_path, monkeypatch, case):
    paths = linux_inputs(tmp_path, monkeypatch)
    paths["uid_map"].write_text(UID_MAP_MALFORMED[case], encoding="ascii")
    with pytest.raises(EndpointUnverified, match="not verified"):
        linux_input_check(paths)(*CONNECTION)


def test_windows_rows_in_every_state_windows_defines_pass(monkeypatch):
    """A table with a row in each MIB_TCP_STATE (1 to 12), and an unrelated row without a creation time (Windows gives
    none for some): the check passes the far end."""
    for state in range(1, 13):
        monkeypatch.setitem(WINDOWS_STATES, f"STATE-{state}", state)
    extra = [WinRow(f"STATE-{state}", ("127.0.0.1", 9000 + state), NO_PEER, 30, 1000.0) for state in range(1, 13)]
    extra.append(WinRow("LISTEN", ("127.0.0.1", 9100), NO_PEER, 4, None))  # not a socket the check relies on
    windows_inputs(monkeypatch, rows=[*daemon_rows(), *extra])
    windows_input_check()(*CONNECTION)


# Windows TCP rows Windows never writes, each beside a valid far end: the IPv4 or IPv6 table.
WINDOWS_MALFORMED = {
    "ipv4-state-0": [WinRow("STATE-0", ("127.0.0.1", 9000), NO_PEER, 30, 1000.0)],
    "ipv4-state-13": [WinRow("STATE-13", ("127.0.0.1", 9000), NO_PEER, 30, 1000.0)],
    "ipv4-state-4294967295": [WinRow("STATE-MAX", ("127.0.0.1", 9000), NO_PEER, 30, 1000.0)],
    "ipv6-state-13": [WinRow("STATE-13", ("::1", 9000), ("::", 0), 30, 1000.0)],
    "ipv4-negative-creation-time": [WinRow("LISTEN", ("127.0.0.1", 9000), NO_PEER, 30, -2e10)],
    "ipv6-negative-creation-time": [WinRow("LISTEN", ("::1", 9000), ("::", 0), 30, -2e10)],
}


@pytest.mark.parametrize("case", list(WINDOWS_MALFORMED))
def test_a_windows_tcp_row_windows_never_writes_is_refused(monkeypatch, case):
    monkeypatch.setitem(WINDOWS_STATES, "STATE-0", 0)
    monkeypatch.setitem(WINDOWS_STATES, "STATE-13", 13)
    monkeypatch.setitem(WINDOWS_STATES, "STATE-MAX", 4294967295)
    windows_inputs(monkeypatch, rows=[*daemon_rows(), *WINDOWS_MALFORMED[case]])
    with pytest.raises(EndpointUnverified, match="not verified"):
        windows_input_check()(*CONNECTION)


# --- the last pass: every parsed field within its kernel or Win32 type ------------------------------------------------

U8, U32, U64 = 2 ** 8 - 1, 2 ** 32 - 1, 2 ** 64 - 1
INT_MIN, INT_MAX, LONG_MAX = -(2 ** 31), 2 ** 31 - 1, 2 ** 63 - 1


def kind_row(kind: str) -> list[str]:
    """A tcp row other than the far end: a full socket (17 fields), or a request or time-wait socket (12)."""
    if kind == "full":
        return other_row("tcp", 17)
    if kind == "request":
        return proc_row_fields("tcp", 12, remote=kernel("127.0.0.1", 40001), state="03", slot="1:")
    return other_row("tcp", 12)


# (kind, field, value, whether the check passes): each decimal field at the edges of its kernel type, the timer codes
# each kind prints, and the constants the short shapes print. Field numbers follow PROC_FIELDS.
PROC_BOUNDARIES = [
    ("full", 0, f"{INT_MAX}:", True), ("full", 0, f"{INT_MAX + 1}:", False),  # slot: int
    *[("full", 5, f"0{code}:00000000", code in "0124") for code in "012345"],  # the timer codes a full socket prints
    ("full", 5, "00:7FFFFFFFFFFFFFFF", True), ("full", 5, "00:8000000000000000", False),  # when: clock_t, not negative
    ("full", 3, "05", True), ("full", 3, "06", False),  # TIME_WAIT is printed only in the short shape (#266)
    ("full", 4, "00000000:7FFFFFFF", True), ("full", 4, "00000000:80000000", False),  # receive queue: int, not negative
    ("full", 4, "00000000:FFFFFFFF", False),
    ("full", 4, "7FFFFFFF:00000000", True), ("full", 4, "80000000:00000000", False),  # send queue: within the send
    ("full", 4, "FFFFFFFF:00000000", False),  # buffer, an int
    ("full", 6, "000000FF", True), ("full", 6, "00000100", False),  # retransmits: u8
    ("full", 7, str(U32), True), ("full", 7, str(U32 + 1), False),  # uid: uid_t
    ("full", 8, str(U8), True), ("full", 8, str(U8 + 1), False), ("full", 8, "-1", False),  # timeout: u8 probes
    ("full", 9, str(U64), True), ("full", 9, str(U64 + 1), False),  # inode: unsigned long
    ("full", 10, str(INT_MIN), True), ("full", 10, str(INT_MAX), True),  # refcount: printed as int
    ("full", 10, str(INT_MIN - 1), False), ("full", 10, str(INT_MAX + 1), False),
    ("full", 12, str(LONG_MAX), True), ("full", 12, str(LONG_MAX + 1), False),  # rto: clock_t
    ("full", 13, str(LONG_MAX), True), ("full", 13, str(LONG_MAX + 1), False),  # ato: clock_t
    ("full", 14, "511", True), ("full", 14, "512", False),  # (u8 quick << 1) | pingpong
    ("full", 15, str(U32), True), ("full", 15, str(U32 + 1), False),  # cwnd: u32
    ("full", 16, str(INT_MIN), True), ("full", 16, str(INT_MAX), True),  # ssthresh: printed as int
    ("full", 16, str(INT_MIN - 1), False), ("full", 16, str(INT_MAX + 1), False),
    ("request", 4, "00000001:00000000", False),  # a request socket prints empty queues
    ("request", 5, "00:00000063", False), ("request", 5, "03:00000063", False),  # and timer code 1
    ("request", 5, "01:8000000000000000", False),
    ("request", 6, "000000FF", True), ("request", 6, "00000100", False),  # retransmits: u8 timeouts
    ("request", 7, str(U32), True), ("request", 7, str(U32 + 1), False),  # uid: the listener's
    ("request", 8, "1", False), ("request", 9, "1", False), ("request", 10, "1", False),  # timeout, inode, refcount: 0
    ("time-wait", 3, "05", True), ("time-wait", 3, "06", True),  # FIN_WAIT2 and TIME_WAIT
    ("time-wait", 4, "00000001:00000000", False),  # a time-wait socket prints empty queues
    ("time-wait", 5, "01:00001770", False), ("time-wait", 5, "03:8000000000000000", False),  # and timer code 3
    ("time-wait", 6, "00000001", False), ("time-wait", 7, "1", False),  # no retransmits, uid 0
    ("time-wait", 8, "1", False), ("time-wait", 9, "1", False),  # timeout, inode: 0
    ("time-wait", 10, str(INT_MIN), True), ("time-wait", 10, str(INT_MAX), True),  # refcount: int
    ("time-wait", 10, str(INT_MAX + 1), False),
]


@pytest.mark.parametrize(("kind", "field", "value", "passes"), PROC_BOUNDARIES,
                         ids=[f"{kind}-{PROC_FIELDS[field]}-{value}-{'passes' if passes else 'refused'}"
                              for kind, field, value, passes in PROC_BOUNDARIES])
def test_each_proc_net_tcp_field_is_held_to_its_kernel_type(tmp_path, monkeypatch, kind, field, value, passes):
    """Each field at the edges of the kernel type it is printed from, and each value the kernel prints in a field it
    prints as a constant, in a row beside a valid far end: inside the type the check passes, outside it refuses."""
    row = kind_row(kind)
    row[field] = value
    paths = linux_inputs(tmp_path, monkeypatch)
    write_table(paths, "tcp", [proc_row_fields("tcp", 17), row])
    check = linux_input_check(paths)
    if passes:
        check(*CONNECTION)
    else:
        with pytest.raises(EndpointUnverified, match="not verified"):
            check(*CONNECTION)


# (uid map row beside BASE_MAP, whether the check passes): each number of a uid_map row at the edges of its u32 range,
# with the ranges map_write accepts.
UID_MAP_BOUNDARIES = {
    "first-at-its-largest": (map_line(4294967294, 5000, 1), True),
    "first-one-past": (map_line(4294967295, 5000, 1), False),
    "outside-at-its-largest": (map_line(5000, 4294967294, 1), True),
    "outside-one-past": (map_line(5000, 4294967295, 1), False),
    "count-at-its-smallest": (map_line(5000, 5000, 1), True),
    "count-one-below": (map_line(5000, 5000, 0), False),
    "count-reaching-the-end": (map_line(5000, 5000, 4294967295 - 5000), True),
    "count-one-past-the-end": (map_line(5000, 5000, 4294967296 - 5000), False),
}


@pytest.mark.parametrize("case", list(UID_MAP_BOUNDARIES))
def test_each_uid_map_number_is_held_to_its_type(tmp_path, monkeypatch, case):
    row, passes = UID_MAP_BOUNDARIES[case]
    paths = linux_inputs(tmp_path, monkeypatch)
    paths["uid_map"].write_text(BASE_MAP + row, encoding="ascii")
    check = linux_input_check(paths)
    if passes:
        check(*CONNECTION)
    else:
        with pytest.raises(EndpointUnverified, match="not verified"):
            check(*CONNECTION)


# (extra row, whether the check passes): each Windows TCP row field at the edges of its Win32 type.
WINDOWS_BOUNDARIES = {
    "state-1": ([WinRow("STATE-1", ("127.0.0.1", 9000), NO_PEER, 30, 1000.0)], True),
    "state-12": ([WinRow("STATE-12", ("127.0.0.1", 9000), NO_PEER, 30, 1000.0)], True),
    "state-0": ([WinRow("STATE-0", ("127.0.0.1", 9000), NO_PEER, 30, 1000.0)], False),
    "state-13": ([WinRow("STATE-13", ("127.0.0.1", 9000), NO_PEER, 30, 1000.0)], False),
    "creation-time-at-its-largest": ([WinRow("LISTEN", ("127.0.0.1", 9000), NO_PEER, 30, None, 2 ** 63 - 1)], True),
    "creation-time-zero": ([WinRow("LISTEN", ("127.0.0.1", 9000), NO_PEER, 30, None, 0)], True),
    "creation-time-one-below-zero": ([WinRow("LISTEN", ("127.0.0.1", 9000), NO_PEER, 30, None, -1)], False),
    "pid-at-its-largest": ([WinRow("LISTEN", ("127.0.0.1", 9000), NO_PEER, U32, 1000.0)], True),
}


@pytest.mark.parametrize("case", list(WINDOWS_BOUNDARIES))
def test_each_windows_tcp_row_field_is_held_to_its_win32_type(monkeypatch, case):
    rows, passes = WINDOWS_BOUNDARIES[case]
    for name, state in (("STATE-0", 0), ("STATE-1", 1), ("STATE-12", 12), ("STATE-13", 13)):
        monkeypatch.setitem(WINDOWS_STATES, name, state)
    windows_inputs(monkeypatch, rows=[*daemon_rows(), *rows])
    check = windows_input_check()
    if passes:
        check(*CONNECTION)
    else:
        with pytest.raises(EndpointUnverified, match="not verified"):
            check(*CONNECTION)


def test_a_windows_port_is_read_from_its_low_16_bits_only(monkeypatch):
    """MIB_TCPROW_OWNER_PID: a port's upper 16 bits may be uninitialized. Set, they change nothing."""
    windows_inputs(monkeypatch)
    get = FakeGetExtendedTcpTable({2: windows_table(daemon_rows(), 4, high_port_bytes=b"\xff\xff"),
                                   23: windows_table(daemon_rows(), 6, high_port_bytes=b"\xff\xff")})
    monkeypatch.setattr(ctypes, "WinDLL", lambda name, **kw: SimpleNamespace(GetExtendedTcpTable=get), raising=False)
    windows_input_check()(*CONNECTION)


@pytest.mark.parametrize("table", ["tcp", "tcp6"])
def test_a_kernel_that_prints_the_pointer_as_0_is_read(tmp_path, monkeypatch, table):
    """Kernels with upstream commit 7c0ec6288b49 ("net: Replace %pK output with 0") print every row's pointer column
    as a literal 0. Each kind of row (a full socket, a time-wait socket, a request socket) still passes with it, in
    either table, and the check verifies the daemon's end."""
    host = "::ffff:127.0.0.1" if table == "tcp6" else "127.0.0.1"
    rows = [proc_row_fields(table, 17), other_row(table, 17), other_row(table, 12),
            proc_row_fields(table, 12, remote=kernel(host, 40002), state="03", slot="3:")]
    for row in rows:
        row[11] = "0"
    paths = linux_inputs(tmp_path, monkeypatch)
    write_table(paths, table, rows)
    linux_input_check(paths)(*CONNECTION)


def kind_row_in(table: str, kind: str) -> list[str]:
    """A row of `table` other than the far end: a full socket, a time-wait socket or a request socket."""
    if kind == "full":
        return other_row(table, 17)
    if kind == "time-wait":
        return other_row(table, 12)
    host = "::ffff:127.0.0.1" if table == "tcp6" else "127.0.0.1"
    return proc_row_fields(table, 12, remote=kernel(host, 40001), state="03", slot="1:")


# Every column of every kind of row that the kernel prints in a fixed-width hex form, or (the slot) with its colon. A
# literal 0 is accepted in the pointer column alone (upstream 7c0ec6288b49); decimal columns print zero as 0 (%u, %d),
# and always have.
ZERO_ELSEWHERE = [(table, kind, field) for table in ("tcp", "tcp6") for kind in ("full", "time-wait", "request")
                  for field in range(7)]


@pytest.mark.parametrize(("table", "kind", "field"), ZERO_ELSEWHERE,
                         ids=[f"{table}-{kind}-{PROC_FIELDS[field]}" for table, kind, field in ZERO_ELSEWHERE])
def test_a_literal_0_is_refused_in_every_fixed_width_column(tmp_path, monkeypatch, table, kind, field):
    """The pointer column's literal 0 loosens no other column: a bare 0 in the slot, an address, the state, the
    queues, the timer or the retransmits of any kind of row, in either table, is refused beside a valid far end."""
    row = kind_row_in(table, kind)
    row[field] = "0"
    paths = linux_inputs(tmp_path, monkeypatch)
    write_table(paths, table, [proc_row_fields(table, 17), row])
    with pytest.raises(EndpointUnverified, match="not verified"):
        linux_input_check(paths)(*CONNECTION)
