"""Sign-in with the owner's tailnet identity: whose `Tailscale-*` headers count, and which logins get a session."""

from __future__ import annotations

import os
import socket
import sys
from pathlib import Path

import pytest

from harness_hub import __main__ as cli
from harness_hub import auth
from harness_hub.tailnet import PeerCheck, connection_owner, install_dirs, is_tailscaled, serve_config, tcp_forwards

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

    def check(self) -> PeerCheck:
        return PeerCheck(owner=self.owner, exe=lambda pid: self.exe_path, dirs=lambda: [INSTALL],
                         service=lambda: self.service_pid, platform_ok=lambda: self.windows,
                         own_addresses=lambda: OWN_ADDRESSES, serve=self.serve)

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


def test_unknown_own_addresses_admit_no_source(make_hub):
    peers = FakePeers()
    check = peers.check()
    check.own_addresses = lambda: (_ for _ in ()).throw(OSError("no interfaces"))
    hub = make_hub(peer_check=check)
    assert hub.get("/api/session", headers=identity()).json()["tailnet_login"] is None


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


@pytest.mark.skipif(sys.platform != "win32" or not any((d / "tailscale.exe").is_file() for d in install_dirs()),
                    reason="needs the Tailscale CLI on Windows")
def test_real_serve_config_is_readable():
    config = serve_config()
    assert config is None or isinstance(config, dict)
