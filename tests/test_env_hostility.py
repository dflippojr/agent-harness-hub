"""The Hub in the environments it really runs in: corporate proxies, a daemon that redirects, tailnet host names.

These run over real sockets on this machine (local stubs only, no external network): the Hub's daemon client is the
real one, so proxy settings and redirects reach it exactly as they would in production. The proxy cases reproduce
#83, where the Hub key went through `HTTP_PROXY`.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket
import threading
import time

import pytest
import uvicorn
from fastapi.testclient import TestClient
from hypothesis import given
from hypothesis import strategies as st

from harness_hub import tailnet
from harness_hub.config import Settings, normalize_daemon_url
from harness_hub.daemon import DaemonClient, DaemonUnreachable
from harness_hub.server import create_app
from harness_hub.tailnet import PeerCheck, TailnetIdentity, forwarded_source, is_loopback, login, tcp_forwards

from .conftest import HUB_HOST, FakeDaemon, HubClient, assert_no_secrets
from .test_tailnet import OWNER, FakePeers, identity

PROXY_VARIABLES = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")
NO_PROXY_VARIABLES = ("NO_PROXY", "no_proxy")
KEY = "ho-" + "SyntheticHubKey_" * 2 + "0123456789a"  # 43 characters after the prefix, like a real key
VERIFIER = "synthetic-verifier-" + "v" * 30


class Listener:
    """A local TCP stub standing in for a proxy or a redirect's target. It records the head of every request that
    reaches it and answers 502, so a client that wrongly connects fails fast instead of hanging."""

    def __init__(self):
        self.sock = socket.create_server(("127.0.0.1", 0))
        self.sock.settimeout(0.1)
        self.heads: list[bytes] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.sock.getsockname()[1]}"

    def authorizations(self) -> list[bytes]:
        return [line for head in self.heads for line in head.split(b"\r\n")
                if line.lower().startswith((b"authorization:", b"proxy-authorization:"))]

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except OSError:  # the accept timeout, or the socket closed on exit
                continue
            with conn:
                conn.settimeout(1)
                head = b""
                with contextlib.suppress(OSError):
                    while b"\r\n\r\n" not in head and len(head) < 65536:
                        chunk = conn.recv(4096)
                        head += chunk
                        if not chunk or not head[:1].isalpha():  # closed, or not HTTP (a TLS handshake)
                            break
                self.heads.append(head)
                with contextlib.suppress(OSError):
                    conn.sendall(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")

    def __enter__(self) -> Listener:
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        self._thread.join(5)
        self.sock.close()


class Redirecting:
    """The fake daemon behind a switch: while `to` is set, every request (or only those whose path ends with `only`)
    gets a redirect there instead."""

    def __init__(self, app):
        self.app = app
        self.to = ""
        self.only = ""
        self.status = 307
        self.redirected: list[str] = []

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and self.to and scope["path"].endswith(self.only):
            self.redirected.append(scope["path"])
            await send({"type": "http.response.start", "status": self.status,
                        "headers": [(b"location", (self.to + scope["path"]).encode()), (b"content-length", b"0")]})
            await send({"type": "http.response.body", "body": b""})
            return
        await self.app(scope, receive, send)


@contextlib.contextmanager
def serving(app, host: str = "127.0.0.1"):
    """Serve an ASGI app on a real socket on this machine and yield its URL."""
    sock = socket.create_server((host, 0), family=socket.AF_INET6 if ":" in host else socket.AF_INET)
    server = uvicorn.Server(uvicorn.Config(app, lifespan="off", log_level="warning"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        assert thread.is_alive() and time.monotonic() < deadline, "the stub daemon did not start"
        time.sleep(0.01)
    try:
        yield f"http://{f'[{host}]' if ':' in host else host}:{sock.getsockname()[1]}"
    finally:
        server.should_exit = True
        thread.join(10)
        sock.close()


@pytest.fixture
def clean_env(monkeypatch):
    """No proxy settings from the machine running the tests: each test sets exactly the ones it names."""
    for name in (*PROXY_VARIABLES, *NO_PROXY_VARIABLES):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


@pytest.fixture
def proxy(clean_env):
    with Listener() as listener:
        yield listener


@contextlib.contextmanager
def real_hub(tmp_path, daemon_url: str):
    """A Hub with its real daemon client (no test transport), driven by an in-process browser."""
    settings = Settings(daemon_url=daemon_url, state_dir=tmp_path / "state", poll_seconds=0.02)
    with TestClient(create_app(settings), base_url=f"http://{HUB_HOST}") as client:
        yield HubClient(client, settings.state_dir)


def claim(hub: HubClient, daemon: FakeDaemon) -> None:
    hub.sign_in()
    started = hub.post("/api/claim")
    assert started.status_code == 201, started.text
    daemon.approve(started.json()["request_id"])
    hub.wait_for("claimed")


def every_call(client: DaemonClient):
    """Each kind of request the Hub sends: public, with the Hub key, and the claim's challenge and verifier."""
    return [client.health(), client.inventory(KEY), client.admin(KEY, "/hub-claim"),
            client.settings(KEY, "config"), client.apps(KEY, "keys"), client.pairing(KEY, "list"),
            client.administration(KEY, "accounts", {}), client.start_hub_claim("Hub", "synthetic-challenge"),
            client.redeem_hub_claim("pr-synthetic", VERIFIER)]


async def call_all(base_url: str) -> list:
    client = DaemonClient(base_url)
    try:
        replies = []
        for call in every_call(client):
            try:
                replies.append(await call)
            except DaemonUnreachable as exc:
                replies.append(exc)
        return replies
    finally:
        await client.aclose()


# --- Proxies (#83) ---------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("no_proxy", [None, ("NO_PROXY", "intranet.example,.corp.example"),
                                      ("no_proxy", "intranet.example"), ("NO_PROXY", "")])
@pytest.mark.parametrize("variable", PROXY_VARIABLES)
def test_the_hub_never_sends_its_key_through_a_proxy(tmp_path, monkeypatch, proxy, variable, no_proxy):
    """The whole claim and signed-in page loads, against a daemon on a real socket, with one proxy variable set and
    a `NO_PROXY` that does not cover the daemon."""
    monkeypatch.setenv(variable, proxy.url)
    if no_proxy:
        monkeypatch.setenv(*no_proxy)
    daemon = FakeDaemon()
    with serving(daemon.app) as url, real_hub(tmp_path, url) as hub:
        claim(hub, daemon)  # claim start (challenge) and redeem (verifier, minted key)
        for page in ("/api/overview", "/api/settings/config", "/api/apps"):
            assert hub.get(page).status_code == 200, page
    assert proxy.heads == [] and proxy.authorizations() == []
    assert daemon.hub_token in daemon.live_tokens
    assert_no_secrets(hub.bodies, daemon.hub_token)


@pytest.mark.parametrize("variable", PROXY_VARIABLES)
def test_an_https_daemon_is_never_reached_through_a_proxy(monkeypatch, proxy, variable):
    """`HTTPS_PROXY` applies to an https daemon. The client must go straight to the daemon's own address: here a stub
    that speaks no TLS, so every call fails, but on the right socket."""
    monkeypatch.setenv(variable, proxy.url)
    with Listener() as daemon:
        replies = asyncio.run(call_all(daemon.url.replace("http://", "https://")))
    assert proxy.heads == []
    assert len(daemon.heads) == len(replies) and all(isinstance(r, DaemonUnreachable) for r in replies)
    assert daemon.authorizations() == []  # it never got past the handshake


def test_the_system_proxy_is_ignored(clean_env, proxy):
    """Windows (registry) and macOS (System Configuration) proxies reach httpx through `urllib.request.getproxies`,
    which the environment variables never show."""
    clean_env.setattr("httpx._utils.getproxies", lambda: {"http": proxy.url, "https": proxy.url, "all": proxy.url})
    daemon = FakeDaemon()
    with serving(daemon.app) as url:
        replies = asyncio.run(call_all(url))
    assert proxy.heads == []
    assert not any(isinstance(reply, DaemonUnreachable) for reply in replies)
    assert replies[0].ok and replies[1].status == 401  # reached the daemon itself, which does not know this key


# --- Redirects -------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_a_daemon_redirect_is_never_followed(clean_env, status):
    """Not with the key, the verifier or anything else: the target sees no connection at all."""
    daemon = Redirecting(FakeDaemon().app)
    daemon.status = status
    with Listener() as target, serving(daemon) as url:
        daemon.to = target.url
        replies = asyncio.run(call_all(url))
    assert target.heads == []
    assert [reply.status for reply in replies] == [status] * len(replies)
    assert len(daemon.redirected) == len(replies)


def test_the_hub_does_not_follow_a_daemon_redirect(tmp_path, clean_env):
    """A daemon that starts redirecting, at claim start, during the redeem and on signed-in pages, never sends the
    Hub's secrets elsewhere and never sends the browser a redirect of its own."""
    fake = FakeDaemon()
    daemon = Redirecting(fake.app)
    with Listener() as target, serving(daemon) as url, real_hub(tmp_path, url) as hub:
        hub.sign_in()
        daemon.to = target.url
        assert hub.post("/api/claim").status_code >= 400
        # Only the redeem is redirected, from before the first poll, so no poll can reach the fake and mint a key.
        daemon.only = "/token"
        started = hub.post("/api/claim")
        assert started.status_code == 201, started.text
        fake.approve(started.json()["request_id"])
        # The daemon minted nothing, so the claim fails rather than following the redirect.
        assert hub.wait_for("failed")["state"] == "failed"
        assert any(path.endswith("/token") for path in daemon.redirected) and not fake.minted
        daemon.to = daemon.only = ""
        claim(hub, fake)
        daemon.to = target.url
        pages = [hub.get(page) for page in ("/api/overview", "/api/settings/config", "/api/apps", "/api/claim")]
    assert target.heads == []
    assert [(page.status_code, "location" in page.headers) for page in pages] == [
        (200, False), (502, False), (200, False), (200, False)]
    assert all("HTTP 307" in page.text for page in pages[:3])  # shown as the daemon's error, not followed
    assert_no_secrets(hub.bodies, fake.hub_token)


# --- Daemon URLs that resolve somewhere unexpected ----------------------------------------------------------------

@pytest.mark.parametrize("url", [
    "http://node.tail1234.ts.net:8100", "http://100.101.102.103:8100", "http://[fd7a:115c:a1e0::1]:8100",
    "http://[::ffff:100.101.102.103]:8100",
    # Spellings of 127.0.0.1 that resolvers accept but are not a loopback literal, and names that only look local.
    "http://2130706433:8100", "http://0x7f.0.0.1:8100", "http://127.1:8100", "http://localhost.:8100",
    "http://127.0.0.1.nip.io:8100", "http://localhost.tail1234.ts.net:8100",
])
def test_plain_http_is_only_for_a_loopback_daemon(url):
    """The key goes in clear over plain http, so it may only go to this machine's loopback address."""
    with pytest.raises(ValueError, match="https"):
        normalize_daemon_url(url)


@pytest.mark.parametrize("url", ["https://node.tail1234.ts.net", "https://100.101.102.103:8100",
                                 "https://[fd7a:115c:a1e0::1]:8100", "http://127.0.0.1:8100", "http://[::1]:8100",
                                 "http://localhost:8100", "http://[::ffff:127.0.0.1]:8100"])
def test_tailnet_and_loopback_daemon_urls_are_accepted(url):
    assert normalize_daemon_url(url) == url


def ipv6_loopback() -> bool:
    try:
        with socket.create_server(("::1", 0), family=socket.AF_INET6):
            return True
    except OSError:
        return False


@pytest.mark.skipif(not ipv6_loopback(), reason="this machine has no IPv6 loopback")
def test_an_ipv6_literal_daemon_gets_the_key_directly(tmp_path, monkeypatch, proxy):
    for name in PROXY_VARIABLES:
        monkeypatch.setenv(name, proxy.url)
    daemon = FakeDaemon()
    with serving(daemon.app, host="::1") as url, real_hub(tmp_path, url) as hub:
        assert url.startswith("http://[::1]:")
        claim(hub, daemon)
        assert hub.get("/api/overview").status_code == 200
    assert proxy.heads == []
    assert_no_secrets(hub.bodies, daemon.hub_token)


# --- Tailnet hosts ---------------------------------------------------------------------------------------------------

TAILNET_NAME = "hub-node.tail1234.ts.net"


@pytest.mark.parametrize(("host", "served"), [
    (TAILNET_NAME, True), (TAILNET_NAME.upper(), True),
    ("100.101.102.103", True), ("[fd7a:115c:a1e0::5]:8443", True),
    (f"{TAILNET_NAME}.attacker.example", False), (f"evil-{TAILNET_NAME}", False), ("hub-node.ts.net", False),
    ("100.101.102.103.nip.io", False), ("100.101.102.104", False), ("fd7a:115c:a1e0::5", False),
    ("[fd7a:115c:a1e0::5]", False), ("[fd7a:115c:a1e0::6]:8443", False), ("", False),
])
def test_a_hub_on_tailnet_names_answers_only_its_own(tmp_path, host, served):
    settings = Settings(daemon_url="https://daemon.test", state_dir=tmp_path / "state",
                        public_url=f"https://{TAILNET_NAME}",
                        extra_hosts=("100.101.102.103", "[fd7a:115c:a1e0::5]:8443"))
    assert settings.cookie_name == "__Host-hub_session"
    with TestClient(create_app(settings, transport=FakeDaemon().transport())) as client:
        response = client.get("/api/session", headers={"Host": host})
    assert response.status_code == (200 if served else 421)


@pytest.fixture
def tailnet_peers():
    peers = FakePeers()
    peers.addresses.add("fe80::1%tailscale0")  # psutil lists a link-local address with its zone
    return peers


@pytest.fixture
def tailnet_hub(make_hub, tailnet_peers):
    return make_hub(peer_check=tailnet_peers.check())


@pytest.mark.parametrize(("source", "remote"), [
    ("100.64.0.7", True), ("fd7a:115c:a1e0::7", True), ("FD7A:115C:A1E0:0:0:0:0:7", True),
    ("::ffff:100.64.0.7", True), ("fd7a:115c:a1e0::7%tailscale0", True), (" 100.64.0.7 ", True),
    # This node itself, however tailscaled spells it.
    ("fd7a:115c:a1e0:0:0:0:0:1", False), ("FD7A:115C:A1E0::1", False), ("fd7a:115c:a1e0::1%3", False),
    ("fe80::1", False), ("::ffff:7f00:1", False), ("0.0.0.0", False), ("::", False),  # noqa: S104
    # Not one address: refused rather than guessed.
    ("[fd7a:115c:a1e0::7]", False), ("100.64.0.7:41641", False), ("node.tail1234.ts.net", False),
    ("100.064.000.007", False), ("100.64.0.7/32", False), ("", False),
])
def test_tailnet_sources_count_only_as_another_node(tailnet_hub, source, remote):
    headers = {**identity(), "X-Forwarded-For": source}
    shown = tailnet_hub.get("/api/session", headers=headers).json()["tailnet_login"]
    assert shown == (OWNER if remote else None)


@pytest.mark.parametrize(("target", "blocks"), [
    (f"{TAILNET_NAME}:8120", True), ("[fd7a:115c:a1e0::1]:8120", True), ("100.100.1.1:8120", True),
    ("tcp://127.0.0.1:8120", True), (":8120", True), (TAILNET_NAME, True), ("[fd7a:115c:a1e0::1]", True),
    (f"{TAILNET_NAME}:8443", False), ("[fd7a:115c:a1e0::1]:22", False), ("100.100.1.1:3389", False),
])
def test_raw_forwards_to_tailnet_targets(target, blocks):
    """A forward whose port is the Hub's, or cannot be read, blocks tailnet sign-in; a clear other port does not."""
    check = PeerCheck(serve=lambda: {"TCP": {"443": {"TCPForward": target}}})
    assert check.raw_forward_to(8120) is blocks


def spellings(address: str) -> list[str]:
    """Ways tailscaled, psutil or a header could write one address."""
    import ipaddress

    ip = ipaddress.ip_address(address)
    out = [str(ip), str(ip).upper(), f" {ip} "]
    if ip.version == 6:
        out += [ip.exploded, ip.exploded.upper(), f"{ip}%tailscale0", f"{ip}%3"]
    else:
        out += [f"::ffff:{ip}", f"::FFFF:{ip}", str(ipaddress.IPv6Address(f"::ffff:{ip}"))]
    return out


@given(st.ip_addresses().map(str))
def test_this_node_is_never_another_node_in_any_spelling(own):
    check = PeerCheck(own_addresses=lambda: {own, "127.0.0.1", "::1"})
    assert not any(check.remote(spelling) for spelling in spellings(own))


header_text = st.text() | st.sampled_from([TAILNET_NAME, "100.64.0.7", "fd7a:115c:a1e0::7", "[::1]:8120",
                                           "fe80::1%eth0", "::ffff:100.64.0.7", "100.64.0.7, 100.64.0.8"])
json_values = st.recursive(st.none() | st.booleans() | st.integers() | header_text,
                           lambda inner: st.lists(inner, max_size=4)
                           | st.dictionaries(st.sampled_from(["TCPForward", "TCP", "Web", "Foreground", "Services",
                                                              "Proxy"]) | st.text(max_size=8), inner, max_size=4),
                           max_leaves=12)


@given(header_text, st.binary(max_size=64), json_values)
def test_tailnet_parsing_never_crashes(text, raw, config):
    """Hosts, sources and serve configs from a tailnet are untrusted input: odd ones are refused, never fatal."""
    assert isinstance(is_loopback(text), bool)
    for value in (text.encode("utf-8", "surrogatepass"), raw):
        assert isinstance(forwarded_source({"headers": [(b"x-forwarded-for", value)]}), str)
    assert isinstance(PeerCheck(own_addresses=lambda: {"100.100.1.1"}).remote(text), bool)
    assert all(isinstance(target, str) for target in tcp_forwards(config))
    assert isinstance(PeerCheck(serve=lambda: config).raw_forward_to(8120), bool)
    from starlette.datastructures import Headers

    shown = login(Headers(raw=[(b"tailscale-user-login", text.encode("utf-8", "surrogatepass"))]))
    assert shown == "" or (shown == shown.strip() and shown.isprintable())


def test_identity_middleware_survives_odd_tailnet_scopes():
    """A connection from a tailnet peer can carry IPv6 or unusual addresses in the ASGI scope; none may crash it."""
    seen = []

    async def app(scope, receive, send):
        seen.append(scope)

    peers = FakePeers()
    middleware = TailnetIdentity(app, check=peers.check())
    for client, server in [(("fd7a:115c:a1e0::7", 41641), ("::1", 8120)), (("100.64.0.7", 1), ("127.0.0.1", 8120)),
                           (None, None), (("fe80::1%3", 50000), ("fe80::1%3", 8120)), (("", 0), ("", 0))]:
        scope = {"type": "http", "method": "GET", "path": "/api/session", "client": client, "server": server,
                 "headers": [(b"tailscale-user-login", OWNER.encode()), (b"x-forwarded-for", b"100.64.0.7")]}
        asyncio.run(middleware(scope, None, None))
    assert len(seen) == 5
    assert all(json.dumps(scope["headers"], default=repr) for scope in seen)


def test_the_tailnet_module_is_what_these_tests_cover():
    # `forwarded_source` and friends are the tailnet module's own parsers, not copies.
    assert tailnet.forwarded_source is forwarded_source and tailnet.login is login
