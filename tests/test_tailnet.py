"""Sign-in with the owner's tailnet identity: whose `Tailscale-*` headers count, and which logins get a session."""

from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from harness_hub import __main__ as cli
from harness_hub import auth
from harness_hub import server as hub_server
from harness_hub.daemon import DaemonClient
from harness_hub.tailnet import (
    CACHE_SECONDS,
    PeerCheck,
    TailnetIdentity,
    connection_owner,
    install_dirs,
    is_loopback,
    is_tailscaled,
    serve_config,
    tcp_forwards,
)

from .conftest import HUB_HOST
from .test_token_words import FULL_TOKENS

OWNER = "owner@example.test"
REMOTE = "100.64.0.7"  # another node on the tailnet
OWN_ADDRESSES = {"100.100.1.1", "fd7a:115c:a1e0::1", "127.0.0.1", "::1", "192.168.1.20"}  # this machine's
INSTALL = Path(os.path.abspath("C:/Program Files/Tailscale"))
TAILSCALED = str(INSTALL / "tailscaled.exe")
HUB_PORT = 8120  # the port the test client's requests arrive on (conftest.HUB_HOST)


class FakePeers:
    """The OS connection table and process paths, as a PeerCheck sees them."""

    def __init__(self, exe: str = TAILSCALED, *, windows: bool = True):
        self.exe_path = exe
        self.windows = windows
        self.pid: int | None = 4242
        self.service_pid: int | None = 4242  # the Tailscale service's process
        self.serve_config: object = {"TCP": {"443": {"HTTPS": True}},  # serve's HTTP proxy to the Hub
                                     "Web": {"node.example.ts.net:443": {"Handlers": {"/": {
                                         "Proxy": f"http://127.0.0.1:{HUB_PORT}"}}}}}
        self.asked: list[tuple[tuple, tuple]] = []
        self.addresses = set(OWN_ADDRESSES)
        self.address_lookups = 0
        self.now = 0.0

    def check(self) -> PeerCheck:
        return PeerCheck(owner=self.owner, exe=lambda pid: self.exe_path, dirs=lambda: [INSTALL],
                         service=lambda: self.service_pid, platform_ok=lambda: self.windows,
                         own_addresses=self.own_addresses, serve=self.serve, clock=lambda: self.now)

    def own_addresses(self):
        self.address_lookups += 1
        return set(self.addresses)

    def serve(self):
        if isinstance(self.serve_config, Exception):
            raise self.serve_config
        return self.serve_config

    def owner(self, client, server):
        self.asked.append((client, server))
        return self.pid


def identity(login: str = OWNER, **extra: str) -> dict:
    """What tailscale serve adds for a request from another node: the login and the tailnet source address."""
    return {"Tailscale-User-Login": login, "Tailscale-User-Name": "Owner", "X-Forwarded-For": REMOTE, **extra}


def tailnet_sign_in(hub, login: str = OWNER, **headers: str):
    return hub.post("/api/session", json={"method": "tailnet"}, headers={**identity(login), **headers})


@pytest.fixture
def peers():
    return FakePeers()


@pytest.fixture
def tailnet_hub(make_hub, fake_daemon, peers):
    """A claimed Hub whose peer check reports tailscaled on the other end of every connection."""
    hub = make_hub(peer_check=peers.check())
    hub.sign_in()
    pending = hub.post("/api/claim").json()
    fake_daemon.approve(pending["request_id"])
    hub.wait_for("claimed")
    hub.delete("/api/session")
    hub.client.cookies.clear()
    hub.client.headers.pop("X-Hub-Session", None)
    return hub


def test_tailscaled_must_run_from_the_install_directory():
    assert is_tailscaled(TAILSCALED, [INSTALL])
    assert not is_tailscaled(str(Path(os.path.abspath("C:/Users/evil/tailscaled.exe"))), [INSTALL])
    assert not is_tailscaled(str(INSTALL / "tailscale.exe"), [INSTALL])
    assert not is_tailscaled(str(INSTALL / "sub" / "tailscaled.exe"), [INSTALL])
    assert not is_tailscaled("", [INSTALL])


def test_tailscaled_must_be_the_tailscale_service():
    def check(service):
        return PeerCheck(owner=lambda *a: 7, exe=lambda pid: TAILSCALED, dirs=lambda: [INSTALL], service=service,
                         platform_ok=lambda: True)

    def missing():
        raise OSError("service 'Tailscale' does not exist")

    peer, server = ("127.0.0.1", 50000), ("127.0.0.1", 8120)
    assert check(lambda: 7).verify(peer, server) is True
    assert check(lambda: 8).verify(peer, server) is False  # the right binary, run by someone else
    assert check(lambda: None).verify(peer, server) is False  # service stopped
    assert check(missing).verify(peer, server) is False  # missing, or the query was refused


def test_peer_verdicts_are_cached_briefly_and_failures_are_not():
    now = [0.0]
    calls = []

    def owner(client, server):
        calls.append(client)
        if len(calls) == 1:
            raise OSError("access denied")
        return 7

    check = PeerCheck(owner=owner, exe=lambda pid: TAILSCALED, dirs=lambda: [INSTALL], service=lambda: 7,
                      platform_ok=lambda: True, clock=lambda: now[0])
    peer, server = ("127.0.0.1", 50000), ("127.0.0.1", 8120)
    assert check.verify(peer, server) is False  # the failed lookup counts as not tailscaled, uncached
    assert check.verify(peer, server) is True
    assert check.verify(peer, server) is True and len(calls) == 2
    now[0] += 6
    assert check.verify(peer, server) is True and len(calls) == 3
    assert check.verify(None, server) is False


def test_sign_in_never_uses_a_cached_verdict():
    calls = []
    check = PeerCheck(owner=lambda client, server: calls.append(client) or 7, exe=lambda pid: TAILSCALED,
                      dirs=lambda: [INSTALL], service=lambda: 7, platform_ok=lambda: True, clock=lambda: 0.0)
    peer, server = ("127.0.0.1", 50000), ("127.0.0.1", 8120)
    assert check.verify(peer, server) is True and check.verify(peer, server) is True and len(calls) == 1
    assert check.verify(peer, server, fresh=True) is True and len(calls) == 2


def test_sign_in_looks_the_peer_up_again(tailnet_hub, peers):
    assert tailnet_hub.get("/api/session", headers=identity()).json()["tailnet_login"] == OWNER
    asked = len(peers.asked)
    assert tailnet_hub.get("/api/session", headers=identity()).json()["tailnet_login"] == OWNER
    assert len(peers.asked) == asked  # keep-alive reads reuse the verdict
    # The port has since gone to another process: sign-in asks the connection table again and refuses.
    peers.exe_path = str(Path(os.path.abspath("C:/Users/evil/tailscaled.exe")))
    response = tailnet_sign_in(tailnet_hub)
    assert len(peers.asked) == asked + 1
    assert response.status_code == 401 and "set-cookie" not in response.headers


@pytest.mark.parametrize("forwarded", [
    [("X-Forwarded-For", "100.100.1.1")],  # this node's own Tailscale IPv4
    [("X-Forwarded-For", "fd7a:115c:a1e0::1")],  # and IPv6
    [("X-Forwarded-For", "192.168.1.20")],  # any other address of this machine
    [("X-Forwarded-For", "127.0.0.1")],
    [("X-Forwarded-For", "::ffff:100.100.1.1")],
    [],  # missing
    [("X-Forwarded-For", f"{REMOTE}, 100.64.0.8")],  # more than one address
    [("X-Forwarded-For", REMOTE), ("X-Forwarded-For", "100.64.0.8")],
    [("X-Forwarded-For", "not-an-address")],
])
def test_tailscaled_requests_from_this_machine_get_no_identity(tailnet_hub, forwarded):
    headers = [("Tailscale-User-Login", OWNER), *forwarded]
    assert tailnet_hub.client.get("/api/session", headers=headers).json()["tailnet_login"] is None
    response = tailnet_hub.client.post("/api/session", json={"method": "tailnet"},
                                       headers=[("X-Hub-Request", "1"), *headers])
    assert response.status_code == 401 and "set-cookie" not in response.headers
    assert tailnet_hub.get("/api/overview").status_code == 401


STATIC = Path(__file__).resolve().parents[1] / "harness_hub" / "static"


def test_only_the_session_route_looks_the_identity_up(tailnet_hub, peers):
    """serve adds the headers to every request; a page load must not list interfaces once per asset (#54)."""
    tailnet_hub.sign_in()
    paths = ["/", "/api/overview", *(f"/static/{f.relative_to(STATIC).as_posix()}" for f in STATIC.rglob("*")
                                     if f.is_file())]
    assert len(paths) > 10
    for path in paths:
        assert tailnet_hub.get(path, headers=identity()).status_code == 200, path
    assert not peers.asked and peers.address_lookups == 0


@pytest.mark.parametrize("windows", [True, False])
@pytest.mark.parametrize(("kind", "method", "path"), [
    ("http", "GET", "/"), ("http", "GET", "/static/app.mjs"), ("http", "GET", "/api/overview"),
    ("http", "POST", "/api/claim"), ("http", "DELETE", "/api/session"), ("http", "HEAD", "/api/session"),
    ("http", "GET", "/api/session/"), ("websocket", "GET", "/api/session"),
])
def test_other_requests_lose_the_identity_headers_unchecked(windows, kind, method, path):
    def refuse(*args, **kwargs):
        raise AssertionError("looked up a request that does not read the identity")

    seen = []

    async def app(scope, receive, send):
        seen.append(scope)

    check = PeerCheck(owner=refuse, exe=refuse, dirs=refuse, service=refuse, platform_ok=lambda: windows,
                      own_addresses=refuse, serve=refuse)
    middleware = TailnetIdentity(app, check=check, trust_unverified=True)
    headers = [(b"tailscale-user-login", OWNER.encode()), (b"x-forwarded-for", REMOTE.encode()), (b"accept", b"*/*")]
    scope = {"type": kind, "method": method, "path": path, "headers": headers, "client": ("127.0.0.1", 50000),
             "server": ("127.0.0.1", HUB_PORT)}
    asyncio.run(middleware(scope, None, None))
    assert seen[0]["headers"] == [(b"x-forwarded-for", REMOTE.encode()), (b"accept", b"*/*")]


def test_session_reads_reuse_the_address_list_briefly(tailnet_hub, peers):
    assert tailnet_hub.get("/api/session", headers=identity()).json()["tailnet_login"] == OWNER
    assert tailnet_hub.get("/api/session", headers=identity()).json()["tailnet_login"] == OWNER
    assert peers.address_lookups == 1
    peers.now += CACHE_SECONDS
    assert tailnet_hub.get("/api/session", headers=identity()).json()["tailnet_login"] == OWNER
    assert peers.address_lookups == 2


def test_sign_in_never_uses_a_cached_address_list(tailnet_hub, peers):
    assert tailnet_hub.get("/api/session", headers=identity()).json()["tailnet_login"] == OWNER
    asked, listed = len(peers.asked), peers.address_lookups
    assert tailnet_sign_in(tailnet_hub).status_code == 204
    assert len(peers.asked) == asked + 1 and peers.address_lookups == listed + 1
    assert tailnet_sign_in(tailnet_hub).status_code == 204
    assert len(peers.asked) == asked + 2 and peers.address_lookups == listed + 2
    # This machine has since gained the source's address: the cached list still clears it for display, not sign-in.
    assert tailnet_hub.get("/api/session", headers=identity()).json()["tailnet_login"] == OWNER
    peers.addresses.add(REMOTE)
    response = tailnet_sign_in(tailnet_hub)
    assert response.status_code == 401 and peers.address_lookups == listed + 3


def test_unknown_own_addresses_admit_no_source(make_hub):
    peers = FakePeers()
    check = peers.check()
    check.own_addresses = lambda: (_ for _ in ()).throw(OSError("no interfaces"))
    hub = make_hub(peer_check=check)
    assert hub.get("/api/session", headers=identity()).json()["tailnet_login"] is None



def test_a_verified_tailscaled_on_a_non_loopback_server_address_is_refused():
    """With a subnet route, tailscaled ends a routed connection and opens its own to the Hub's LAN or wildcard
    address, carrying the device's own headers (#37). Only the loopback listener serve proxies to counts."""
    assert PeerCheck().server_ok is is_loopback
    calls = []
    check = PeerCheck(owner=lambda client, server: calls.append(server) or 7, exe=lambda pid: TAILSCALED,
                      dirs=lambda: [INSTALL], service=lambda: 7, platform_ok=lambda: True)
    for server in (("192.168.1.20", 8120), ("100.100.1.1", 8120), ("0.0.0.0", 8120), ("::", 8120),  # noqa: S104
                   ("fe80::1%3", 8120), ("fd7a:115c:a1e0::1", 8120), ("localhost", 8120), ("", 8120),
                   (None, 8120), ("/run/hub.sock", None)):
        assert check.verify(("127.0.0.1", 50000), server) is False
        assert check.verify(("127.0.0.1", 50000), server, fresh=True) is False
    assert check.verify(("127.0.0.1", 50000), None) is False
    assert not calls  # refused before the connection table is read, and never cached
    for port, server in enumerate((("127.0.0.1", 8120), ("::1", 8120), ("127.0.0.2", 8120),
                                   ("::ffff:127.0.0.1", 8120)), 51000):
        assert check.verify(("127.0.0.1", port), server) is True
    assert len(calls) == 4


def test_a_subnet_routed_connection_signs_no_one_in(tailnet_hub, peers):
    # The same app, reached on a LAN address: the device writes its own bytes, so it can name the loopback Host.
    routed = TestClient(tailnet_hub.client.app, base_url=f"http://192.168.1.20:{HUB_PORT}")
    loopback_host = {"Host": f"127.0.0.1:{HUB_PORT}", "X-Hub-Request": "1"}
    asked = len(peers.asked)
    assert routed.get("/api/session", headers={**identity(), **loopback_host}).json()["tailnet_login"] is None
    response = routed.post("/api/session", json={"method": "tailnet"}, headers={**identity(), **loopback_host})
    assert response.status_code == 401 and "set-cookie" not in response.headers
    assert len(peers.asked) == asked  # tailscaled would have owned the socket; it was never asked
    assert tailnet_sign_in(tailnet_hub).status_code == 204  # serve's own loopback connection still works

def test_check_runs_nowhere_but_windows():
    check = PeerCheck(owner=lambda *a: 7, exe=lambda pid: TAILSCALED, dirs=lambda: [INSTALL],
                      platform_ok=lambda: False)
    assert check.verify(("127.0.0.1", 50000), ("127.0.0.1", 8120)) is False


def test_listed_login_gets_a_session(tailnet_hub, peers):
    assert tailnet_hub.get("/api/session", headers=identity()).json()["tailnet_login"] == OWNER
    response = tailnet_sign_in(tailnet_hub)
    assert response.status_code == 204, response.text
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie
    tailnet_hub.client.headers["X-Hub-Session"] = response.headers["x-hub-session"]
    assert tailnet_hub.get("/api/overview").status_code == 200
    assert tailnet_hub.get("/api/session").json()["signed_in"] is True
    # The lookup used the TCP peer the server saw, never a forwarded address.
    assert peers.asked and all(client[0] == "testclient" for client, _ in peers.asked)


def test_unlisted_login_is_refused(tailnet_hub, fake_daemon):
    fake_daemon.allowed_logins = [OWNER, "partner@example.test"]
    response = tailnet_sign_in(tailnet_hub, "guest@example.test")
    assert response.status_code == 403 and "set-cookie" not in response.headers
    assert tailnet_hub.get("/api/overview").status_code == 401
    assert tailnet_sign_in(tailnet_hub, OWNER.upper()).status_code == 403  # exact match, like the daemon
    assert tailnet_sign_in(tailnet_hub, "partner@example.test").status_code == 204


def test_refused_logins_do_not_throttle_link_sign_in(tailnet_hub):
    for _ in range(auth.TAILNET_ATTEMPTS_PER_MINUTE):
        assert tailnet_sign_in(tailnet_hub, "guest@example.test").status_code == 403
    tailnet_hub.sign_in()


def test_tailnet_attempts_are_throttled_per_login(tailnet_hub, fake_daemon):
    for _ in range(auth.TAILNET_ATTEMPTS_PER_MINUTE):
        assert tailnet_sign_in(tailnet_hub, "guest@example.test").status_code == 403
    asked = len(fake_daemon.sent)
    assert tailnet_sign_in(tailnet_hub, "guest@example.test").status_code == 429
    assert len(fake_daemon.sent) == asked  # a throttled attempt costs no owner API call
    # Another tailnet member cannot lock the owner out, and links keep working.
    assert tailnet_sign_in(tailnet_hub).status_code == 204
    tailnet_hub.sign_in()


def test_allowed_logins_answer_is_reused_briefly(tailnet_hub, fake_daemon):
    def discovery_calls():
        return sum(url.rstrip("/").endswith("/api/admin/v1") for url in fake_daemon.sent)

    before = discovery_calls()
    assert tailnet_sign_in(tailnet_hub, "guest@example.test").status_code == 403
    assert tailnet_sign_in(tailnet_hub).status_code == 204
    assert discovery_calls() == before + 1


def test_no_owner_allowlist_admits_no_login(tailnet_hub, fake_daemon):
    fake_daemon.allowed_logins = []
    response = tailnet_sign_in(tailnet_hub)
    unlisted = tailnet_sign_in(tailnet_hub, "guest@example.test")
    # Open-owner mode reads the same as an unlisted login, so tailnet callers learn nothing about the daemon.
    assert response.status_code == unlisted.status_code == 403
    assert response.json() == unlisted.json()


@pytest.mark.parametrize("listed", [None, "owner@example.test", [OWNER, 7]])
def test_a_daemon_without_a_usable_list_admits_no_login(tailnet_hub, fake_daemon, listed):
    fake_daemon.allowed_logins = listed
    response = tailnet_sign_in(tailnet_hub)
    assert response.status_code == 502 and "1.26" in response.json()["detail"]


def test_unclaimed_daemon_admits_no_login(make_hub, peers):
    hub = make_hub(peer_check=peers.check())
    response = tailnet_sign_in(hub)
    assert response.status_code == 403 and "claim" not in response.json()["detail"]


def test_unreachable_or_refusing_daemon_admits_no_login(tailnet_hub, fake_daemon):
    fake_daemon.down = True
    assert tailnet_sign_in(tailnet_hub).status_code == 503
    fake_daemon.down = False
    fake_daemon.live_tokens.clear()
    assert tailnet_sign_in(tailnet_hub).status_code == 502


@pytest.mark.skipif(sys.platform != "win32", reason="the tailscaled peer check runs only on Windows")
def test_real_connection_table_names_this_process_and_it_is_not_tailscaled():
    with socket.create_server(("127.0.0.1", 0)) as listener:
        server = listener.getsockname()
        with socket.create_connection(server) as client:
            accepted, _ = listener.accept()
            with accepted:
                peer = client.getsockname()
                assert connection_owner(peer, server) == os.getpid()
                assert PeerCheck().verify(peer, server) is False


def test_forged_headers_from_another_local_process_are_ignored(make_hub, fake_daemon):
    impostor = FakePeers(str(Path(os.path.abspath("C:/Users/evil/tailscaled.exe"))))
    hub = make_hub(peer_check=impostor.check())
    assert hub.get("/api/session", headers=identity()).json()["tailnet_login"] is None
    hub.sign_in()
    pending = hub.post("/api/claim").json()
    fake_daemon.approve(pending["request_id"])
    hub.wait_for("claimed")
    hub.delete("/api/session")
    hub.client.cookies.clear()
    hub.client.headers.pop("X-Hub-Session", None)
    response = tailnet_sign_in(hub)
    assert response.status_code == 401 and "tailscaled" in response.json()["detail"]
    assert hub.get("/api/overview").status_code == 401


def test_a_tailscaled_that_is_not_the_service_signs_no_one_in(tailnet_hub, peers):
    # Any local account can run the installed tailscaled.exe as a userspace proxy and forge headers through it.
    peers.service_pid = 5150
    assert tailnet_hub.get("/api/session", headers=identity()).json()["tailnet_login"] is None
    response = tailnet_sign_in(tailnet_hub)
    assert response.status_code == 401 and "set-cookie" not in response.headers
    assert tailnet_hub.get("/api/overview").status_code == 401


def test_forwarded_headers_never_make_a_peer_trusted(make_hub):
    peers = FakePeers()
    peers.pid = None  # no process owns the socket the server saw
    hub = make_hub(peer_check=peers.check())
    forwarded = {**identity(), "X-Forwarded-For": "100.64.0.1", "X-Forwarded-Proto": "https",
                 "Forwarded": "for=100.64.0.1"}
    assert hub.get("/api/session", headers=forwarded).json()["tailnet_login"] is None


def test_off_windows_headers_are_stripped_unless_explicitly_trusted(make_hub, tmp_path):
    peers = FakePeers(windows=False)
    hub = make_hub(peer_check=peers.check())
    assert hub.get("/api/session", headers=identity()).json()["tailnet_login"] is None
    assert not peers.asked
    unsafe = make_hub(peer_check=peers.check(), trust_unverified_identity_headers=True,
                      state_dir=tmp_path / "unsafe")
    assert unsafe.get("/api/session", headers=identity()).json()["tailnet_login"] == OWNER


def test_ambiguous_or_missing_logins_count_as_none(tailnet_hub):
    assert tailnet_hub.get("/api/session").json()["tailnet_login"] is None
    duplicated = [("Tailscale-User-Login", OWNER), ("Tailscale-User-Login", "other@example.test")]
    response = tailnet_hub.client.get("/api/session", headers=duplicated)
    assert response.json()["tailnet_login"] is None
    response = tailnet_hub.client.post("/api/session", json={"method": "tailnet"},
                                       headers=[("X-Hub-Request", "1"), *duplicated])
    assert response.status_code == 401
    assert tailnet_hub.post("/api/session", json={"method": "tailnet"}).status_code == 401


def test_tailnet_sign_in_needs_the_hub_header_and_same_origin(tailnet_hub):
    plain = tailnet_hub.client.post("/api/session", json={"method": "tailnet"}, headers=identity())
    assert plain.status_code == 403
    cross = tailnet_sign_in(tailnet_hub, Origin="https://evil.example")
    assert cross.status_code == 403


def test_link_sign_in_still_works(tailnet_hub):
    tailnet_hub.sign_in()
    assert tailnet_hub.get("/api/overview").status_code == 200


def test_unsafe_trust_option_parses():
    args = cli.serve_parser().parse_args(["--trust-unverified-identity-headers"])
    assert args.trust_unverified_identity_headers is True
    assert cli.serve_parser().parse_args([]).trust_unverified_identity_headers is False


@pytest.mark.parametrize("config", [
    {"TCP": {"4443": {"TCPForward": f"127.0.0.1:{HUB_PORT}"}}},  # tailscale serve --tcp
    {"TCP": {"4443": {"TCPForward": f"localhost:{HUB_PORT}", "TerminateTLS": "node.example.ts.net"}}},
    {"Foreground": {"session": {"TCP": {"4443": {"TCPForward": f"[::1]:{HUB_PORT}"}}}}},  # a foreground serve
    {"Services": {"svc:hub": {"TCP": {"4443": {"TCPForward": f"192.168.1.20:{HUB_PORT}"}}}}},
    {"TCP": {"4443": {"TCPForward": "unparsable"}}},  # a target whose port is unclear could be the Hub
    OSError("Access is denied"),  # a config that cannot be read
])
def test_a_raw_tcp_forward_to_the_hub_signs_no_one_in(tailnet_hub, peers, fake_daemon, config):
    # tailscaled passes a raw TCP client's own Tailscale-* and X-Forwarded-For headers through untouched.
    peers.serve_config = config
    asked = len(fake_daemon.sent)
    response = tailnet_sign_in(tailnet_hub)
    assert response.status_code == 403 and "set-cookie" not in response.headers
    assert len(fake_daemon.sent) == asked
    assert tailnet_hub.get("/api/overview").status_code == 401
    peers.serve_config = None  # no serve config at all
    assert tailnet_sign_in(tailnet_hub).status_code == 204


def test_raw_tcp_forwards_elsewhere_do_not_block_sign_in(tailnet_hub, peers):
    peers.serve_config["TCP"]["22"] = {"TCPForward": "127.0.0.1:22"}
    peers.serve_config["TCP"]["8443"] = {"TCPForward": f"127.0.0.1:{HUB_PORT}0"}
    assert tailnet_sign_in(tailnet_hub).status_code == 204


def test_tcp_forwards_are_found_anywhere_in_the_config():
    config = {"TCP": {"1": {"TCPForward": "a:1"}}, "Foreground": {"s": {"TCP": {"2": {"TCPForward": "b:2"}}}},
              "Services": {"svc:x": {"TCP": {"3": {"TCPForward": "c:3"}}}}, "Web": {"h:443": {"Handlers": {}}}}
    assert sorted(tcp_forwards(config)) == ["a:1", "b:2", "c:3"]
    assert tcp_forwards(None) == []



def test_serve_config_is_decoded_as_utf8(monkeypatch, tmp_path):
    (tmp_path / "Tailscale").mkdir()
    (tmp_path / "Tailscale" / "tailscale.exe").touch()
    monkeypatch.setenv("ProgramFiles", str(tmp_path))
    monkeypatch.delenv("ProgramW6432", raising=False)
    seen = {}

    def run(args, **kw):
        seen.update(kw)
        return subprocess.CompletedProcess(args, 0, stdout='{"Web": {"nöde.example.ts.net:443": {}}}')

    monkeypatch.setattr(subprocess, "run", run)
    assert serve_config() == {"Web": {"nöde.example.ts.net:443": {}}}
    assert seen["encoding"] == "utf-8"  # Go prints UTF-8 whatever the console code page is

@pytest.mark.skipif(sys.platform != "win32" or not any((d / "tailscale.exe").is_file() for d in install_dirs()),
                    reason="needs the Tailscale CLI on Windows")
def test_real_serve_config_is_readable():
    config = serve_config()
    assert config is None or isinstance(config, dict)


# #84: the page reads /api/session first on every load, so a login's text must never make the guard withhold it.
WORD_LOGIN = "ha-jung.kim@example.test"


def test_tailnet_login_with_a_prefix_word_can_read_its_session(tailnet_hub):
    reply = tailnet_hub.get("/api/session", headers=identity(WORD_LOGIN))
    assert reply.status_code == 200 and reply.json()["tailnet_login"] == WORD_LOGIN
    tailnet_hub.sign_in()
    signed_in = tailnet_hub.get("/api/session", headers=identity(WORD_LOGIN))
    assert signed_in.status_code == 200 and signed_in.json()["signed_in"] is True


@pytest.mark.parametrize("token", FULL_TOKENS)
def test_a_token_shaped_login_is_redacted_not_withheld(tailnet_hub, token):
    reply = tailnet_hub.get("/api/session", headers=identity(token + "@example.test"))
    assert reply.status_code == 200
    assert reply.json()["tailnet_login"] == "[withheld: token-shaped]@example.test"


# #169: a tailnet session lasts only while the daemon still lists the login that opened it.
def owner_tab(hub, login: str = OWNER) -> str:
    """Sign this client's tab in with its tailnet identity; returns the tab's binding."""
    response = tailnet_sign_in(hub, login)
    assert response.status_code == 204, response.text
    hub.client.headers["X-Hub-Session"] = response.headers["x-hub-session"]
    return response.headers["x-hub-session"]


def discovery_reads(fake_daemon) -> int:
    return sum(url.rstrip("/").endswith("/api/admin/v1") for url in fake_daemon.sent)


def removal(hub, fake_daemon, how: str):
    """Take a signed-in login's ownership away in one of the ways the daemon can say no; returns an undo."""
    claims = hub.client.app.fastapi.state.claims
    tokens = set(fake_daemon.live_tokens)
    if how == "unlisted":
        fake_daemon.allowed_logins = ["partner@example.test"]
    elif how == "empty":
        fake_daemon.allowed_logins = []
    elif how == "no list":
        fake_daemon.allowed_logins = None
    elif how == "key refused":
        fake_daemon.live_tokens.clear()
    elif how == "unclaimed":
        claims.key = lambda: None

    def undo():
        fake_daemon.allowed_logins = [OWNER]
        fake_daemon.live_tokens.update(tokens)
        vars(claims).pop("key", None)

    return undo


@pytest.mark.parametrize("how", ["unlisted", "empty", "no list", "key refused", "unclaimed"])
def test_removing_a_login_ends_its_tailnet_sessions(tailnet_hub, fake_daemon, monkeypatch, how):
    monkeypatch.setattr(hub_server, "ALLOWED_LOGINS_SECONDS", 0.0)
    owner_tab(tailnet_hub)
    assert tailnet_hub.get("/api/overview").status_code == 200
    restore = removal(tailnet_hub, fake_daemon, how)
    ended = tailnet_hub.get("/api/overview")
    assert ended.status_code == 401 and ended.json()["detail"] == "sign in first"
    assert tailnet_hub.get("/api/settings/config").status_code == 401
    assert tailnet_hub.get("/api/session").json()["signed_in"] is False
    restore()  # listed again: the ended tab stays ended, and a new tailnet sign-in works
    assert tailnet_hub.get("/api/overview").status_code == 401
    assert tailnet_hub.get("/api/session").json()["signed_in"] is False
    owner_tab(tailnet_hub)
    assert tailnet_hub.get("/api/overview").status_code == 200


def test_a_link_session_does_not_depend_on_allowed_logins(tailnet_hub, fake_daemon, monkeypatch):
    monkeypatch.setattr(hub_server, "ALLOWED_LOGINS_SECONDS", 0.0)
    tailnet_hub.sign_in()
    fake_daemon.allowed_logins = []
    before = discovery_reads(fake_daemon)
    assert tailnet_hub.get("/api/claim").status_code == 200
    assert tailnet_hub.get("/api/session").json()["signed_in"] is True
    assert discovery_reads(fake_daemon) == before  # a link tab costs no allowed_logins read
    assert tailnet_hub.get("/api/overview").status_code == 200


@pytest.mark.parametrize("outage", ["unreachable", "restarting"])
def test_a_daemon_that_cannot_answer_signs_no_one_out(tailnet_hub, fake_daemon, monkeypatch, outage):
    monkeypatch.setattr(hub_server, "ALLOWED_LOGINS_SECONDS", 0.0)
    owner_tab(tailnet_hub)
    if outage == "unreachable":
        fake_daemon.down = True
    else:
        fake_daemon.discovery_status = 503
    before = discovery_reads(fake_daemon)
    assert tailnet_hub.get("/api/claim").status_code == 200
    assert tailnet_hub.get("/api/session").json()["signed_in"] is True
    assert discovery_reads(fake_daemon) > before  # the login was checked, and the outage did not end the tab
    fake_daemon.down = False
    fake_daemon.discovery_status = None
    assert tailnet_hub.get("/api/overview").status_code == 200


def test_a_tailnet_tab_that_joined_a_link_session_ends_alone(tailnet_hub, fake_daemon, monkeypatch):
    monkeypatch.setattr(hub_server, "ALLOWED_LOGINS_SECONDS", 0.0)
    link = tailnet_hub.sign_in().headers["x-hub-session"]
    cookie = tailnet_hub.client.cookies["hub_session"]
    sessions = tailnet_hub.client.app.fastapi.state.sessions
    expiry = sessions._sessions[auth._digest(cookie)][0]
    joined = tailnet_sign_in(tailnet_hub)
    assert joined.status_code == 204 and "set-cookie" not in joined.headers  # the same session, a new tab
    tab = joined.headers["x-hub-session"]
    assert tailnet_hub.get("/api/overview", headers={"X-Hub-Session": tab}).status_code == 200
    fake_daemon.allowed_logins = []
    assert tailnet_hub.get("/api/overview", headers={"X-Hub-Session": tab}).status_code == 401
    assert tailnet_hub.get("/api/overview", headers={"X-Hub-Session": link}).status_code == 200
    assert sessions._sessions[auth._digest(cookie)][0] == expiry
    fake_daemon.allowed_logins = [OWNER]
    assert tailnet_hub.get("/api/overview", headers={"X-Hub-Session": tab}).status_code == 401


def test_a_link_tab_that_joined_a_tailnet_session_is_never_checked(tailnet_hub, fake_daemon, monkeypatch):
    monkeypatch.setattr(hub_server, "ALLOWED_LOGINS_SECONDS", 0.0)
    tab = owner_tab(tailnet_hub)
    link = tailnet_hub.sign_in().headers["x-hub-session"]
    fake_daemon.allowed_logins = []
    assert tailnet_hub.get("/api/claim", headers={"X-Hub-Session": tab}).status_code == 401
    assert tailnet_hub.get("/api/claim", headers={"X-Hub-Session": link}).status_code == 200


@pytest.mark.parametrize("outage", [False, True])
def test_tailnet_tabs_share_one_allowed_logins_read(tailnet_hub, fake_daemon, monkeypatch, outage):
    monkeypatch.setattr(hub_server, "ALLOWED_LOGINS_SECONDS", 0.0)
    owner_tab(tailnet_hub)
    other = owner_tab(tailnet_hub)  # a second tailnet tab, so the burst is not one binding's
    fake_daemon.down = outage
    monkeypatch.setattr(hub_server, "ALLOWED_LOGINS_SECONDS", 60.0)  # the sign-in's answer is stale; the next is kept
    before = discovery_reads(fake_daemon)
    for binding in [other, *[tailnet_hub.client.headers["X-Hub-Session"]] * 10]:
        assert tailnet_hub.get("/api/claim", headers={"X-Hub-Session": binding}).status_code == 200
    assert discovery_reads(fake_daemon) == before + 1


def test_concurrent_requests_share_one_allowed_logins_read(tailnet_hub, fake_daemon, monkeypatch):
    monkeypatch.setattr(hub_server, "ALLOWED_LOGINS_SECONDS", 0.0)
    owner_tab(tailnet_hub)
    monkeypatch.setattr(hub_server, "ALLOWED_LOGINS_SECONDS", 60.0)
    call = DaemonClient._call

    async def slow(self, method, path, **kw):
        await asyncio.sleep(0.1)  # every request arrives while the first read is still in flight
        return await call(self, method, path, **kw)

    monkeypatch.setattr(DaemonClient, "_call", slow)
    before = discovery_reads(fake_daemon)
    with ThreadPoolExecutor(4) as pool:
        statuses = list(pool.map(lambda _: tailnet_hub.get("/api/claim").status_code, range(8)))
    assert statuses == [200] * 8
    assert discovery_reads(fake_daemon) == before + 1


# The owner's call on #177: an outage keeps a tailnet tab only for a short grace after the daemon last listed its login.
SAME_ORIGIN = {"Origin": f"http://{HUB_HOST}"}  # Settings writes need it


def grace_clock(hub) -> list[float]:
    """Drive the Hub's session clock by hand; returns the list whose one value is now."""
    now = [time.time()]
    hub.client.app.fastapi.state.sessions.clock = lambda: now[0]
    return now


def test_an_outage_within_the_grace_keeps_a_tailnet_tab(tailnet_hub, fake_daemon, monkeypatch):
    monkeypatch.setattr(hub_server, "ALLOWED_LOGINS_SECONDS", 0.0)
    now = grace_clock(tailnet_hub)
    owner_tab(tailnet_hub)
    now[0] += 60
    assert tailnet_hub.get("/api/claim").status_code == 200  # listed: the grace starts again from here
    fake_daemon.down = True
    now[0] += hub_server.OUTAGE_GRACE_SECONDS - 1
    assert tailnet_hub.get("/api/claim").status_code == 200
    assert tailnet_hub.get("/api/session").json()["signed_in"] is True


@pytest.mark.parametrize("outage", ["unreachable", "restarting"])
def test_an_outage_past_the_grace_answers_401_until_the_login_is_checked(tailnet_hub, fake_daemon, monkeypatch,
                                                                          outage):
    monkeypatch.setattr(hub_server, "ALLOWED_LOGINS_SECONDS", 0.0)
    now = grace_clock(tailnet_hub)
    owner_tab(tailnet_hub)
    if outage == "unreachable":
        fake_daemon.down = True
    else:
        fake_daemon.discovery_status = 503
    now[0] += hub_server.OUTAGE_GRACE_SECONDS + 1
    ended = tailnet_hub.get("/api/overview")
    assert ended.status_code == 401 and ended.json()["detail"] == "sign in first"
    assert tailnet_hub.get("/api/session").json()["signed_in"] is False
    fake_daemon.down = False
    fake_daemon.discovery_status = None
    # The daemon answers again and still lists the login: the same tab works, and the grace starts again.
    assert tailnet_hub.get("/api/overview").status_code == 200
    fake_daemon.down = True
    now[0] += hub_server.OUTAGE_GRACE_SECONDS - 1
    assert tailnet_hub.get("/api/claim").status_code == 200


def test_a_restart_the_tab_asks_for_ends_it_once_the_grace_runs_out(tailnet_hub, fake_daemon, monkeypatch):
    """The review's case: the owner removed the login from harness.local.yaml, which the daemon applies on a restart,
    and the removed tab asks for that restart itself, through the Hub's Settings route, hoping to stay in while every
    check lands in the outage."""
    monkeypatch.setattr(hub_server, "ALLOWED_LOGINS_SECONDS", 0.0)
    now = grace_clock(tailnet_hub)
    owner_tab(tailnet_hub)
    now[0] += 30
    asked = tailnet_hub.post("/api/settings/restart", json={"revision": fake_daemon.revision, "confirm": True},
                           headers=SAME_ORIGIN)
    assert asked.status_code == 202, asked.text
    fake_daemon.down = True  # the restart takes the daemon down, and it stays down
    fake_daemon.allowed_logins = ["partner@example.test"]  # what it will list once it is back
    now[0] += hub_server.OUTAGE_GRACE_SECONDS - 1
    assert tailnet_hub.get("/api/claim").status_code == 200  # a short restart signs no one out
    now[0] += 2
    assert tailnet_hub.get("/api/claim").status_code == 401
    again = tailnet_hub.post("/api/settings/restart", json={"revision": fake_daemon.revision, "confirm": True},
                           headers=SAME_ORIGIN)
    assert again.status_code == 401  # it cannot ask for another restart either
    fake_daemon.down = False  # back, without the login: the tab ends for good
    assert tailnet_hub.get("/api/claim").status_code == 401
    fake_daemon.allowed_logins = [OWNER]
    assert tailnet_hub.get("/api/claim").status_code == 401


def test_a_burst_past_the_grace_makes_one_attempt(tailnet_hub, fake_daemon, monkeypatch):
    monkeypatch.setattr(hub_server, "ALLOWED_LOGINS_SECONDS", 0.0)
    now = grace_clock(tailnet_hub)
    owner_tab(tailnet_hub)
    fake_daemon.down = True
    now[0] += hub_server.OUTAGE_GRACE_SECONDS + 1
    monkeypatch.setattr(hub_server, "ALLOWED_LOGINS_SECONDS", 60.0)
    before = discovery_reads(fake_daemon)
    assert [tailnet_hub.get("/api/claim").status_code for _ in range(10)] == [401] * 10
    assert discovery_reads(fake_daemon) == before + 1


def test_a_definite_no_within_the_grace_still_ends_the_tab_at_once(tailnet_hub, fake_daemon, monkeypatch):
    monkeypatch.setattr(hub_server, "ALLOWED_LOGINS_SECONDS", 0.0)
    now = grace_clock(tailnet_hub)
    owner_tab(tailnet_hub)
    fake_daemon.down = True
    now[0] += 10
    assert tailnet_hub.get("/api/claim").status_code == 200
    fake_daemon.down = False
    fake_daemon.allowed_logins = []
    now[0] += 1
    assert tailnet_hub.get("/api/claim").status_code == 401
    fake_daemon.allowed_logins = [OWNER]
    assert tailnet_hub.get("/api/claim").status_code == 401  # ended for good, not held back for the grace
