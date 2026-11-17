"""The Hub's tailnet identity check (`harness_hub/tailnet.py`) against the daemon's (agent-harness
`harness/tailscale_peer.py`), decision by decision (#253).

`tailnet.py` follows the daemon's rules for when the `Tailscale-*` identity headers count, but it is a copy that
nothing kept in step. This module imports the daemon's module from the checkout named by HUB_E2E_DAEMON_SRC, beside the
Hub's, and puts both through the same synthetic inputs, one decision at a time:

- which executable is tailscaled (the install directories, the executable's place in them);
- whether a server address is loopback, and whether two addresses are the same;
- whether a serve-config document forwards raw TCP to a port (the CLI's JSON output, parsed by each side);
- whether the `X-Forwarded-For` source tailscaled names is another node, and not this machine;
- whether a scope carries identity headers, and what removing them leaves;
- the peer verdict cache, and the peer verdict itself on injected process facts.

Where the two signatures differ, one adapter per decision asks both the same question. A difference either side
intends is listed in `KNOWN` with its reason. Any other difference fails, named by decision and case; so does a listed
one that no longer differs, so the list stays true. A daemon refactor that breaks an adapter fails the nightly drift
run (`drift.yml`), which is the signal to decide whether the Hub follows.

Nothing here looks up a process, lists the machine's interfaces or runs the Tailscale CLI: `psutil` and
`subprocess.run` are replaced with synthetic answers, so the module runs on Linux as well as Windows. Out of scope,
because they are state or I/O rather than a decision: the daemon's refusal that stays once it has seen a raw forward
(`raw_forward_seen`), its cache of `tailscale ip`, and the Hub's `fresh` lookups for sign-in.
"""

from __future__ import annotations

import contextlib
import importlib.util
import json
import socket
import subprocess
import sys
from collections import namedtuple

import psutil
import pytest

from harness_hub import tailnet

from .targets import daemon_source

pytestmark = pytest.mark.e2e

HUB_PORT = 8787
NODE_V4, NODE_V6 = "100.101.102.103", "fd7a:115c:a1e0::1"  # this node's Tailscale addresses
LAN = "192.168.1.20"
PEER = "100.64.0.2"  # another tailnet node

# (decision, case) -> why the two copies answer differently. Each entry must still differ.
KNOWN: dict[tuple[str, str], str] = {
    ("same address", "padded"): "The daemon strips spaces before parsing an address; the Hub does not. Both read the "
                                "connection table's addresses and the ASGI scope's, which never carry spaces.",
    ("loopback server address", "IPv4 with a zone"): "The Hub drops a `%zone` suffix before parsing; the daemon's "
                                                     "parser rejects one on IPv4. A server address in the ASGI scope "
                                                     "never carries one.",
    ("raw TCP forward", "null config"): "`tailscale serve status --json` prints `null` when nothing is served. The Hub "
                                        "reads that as no forward; the daemon wants an object and refuses anything "
                                        "else. Neither is reached through serve in that state, so no request is "
                                        "affected.",
    ("raw TCP forward", "list config"): "Same as the null config: the daemon refuses any document that is not an "
                                        "object; the Hub searches whatever it gets.",
    ("raw TCP forward", "bare other port"): "The Hub counts a target without a host as unreadable, so as a forward to "
                                            "its port (fail closed); the daemon reads the bare port and compares it.",
    ("raw TCP forward", "empty host, other port"): "As the bare port: the Hub needs a host before the colon.",
    ("raw TCP forward", "empty target"): "The daemon skips an empty `TCPForward`; the Hub counts it as unreadable, so "
                                         "as a forward (fail closed).",
    ("raw TCP forward", "null target"): "As the empty target: the daemon skips a null `TCPForward`, the Hub refuses.",
    ("raw TCP forward", "numeric other port"): "The Hub reads a non-string target as unreadable (fail closed); the "
                                               "daemon reads its digits as the port.",
    ("forwarded source", "other loopback"): "The Hub refuses every loopback and unspecified source outright; the "
                                            "daemon refuses only this machine's own addresses. tailscaled names a "
                                            "tailnet source, never a loopback one.",
    ("forwarded source", "unspecified IPv4"): "As the other loopback: the Hub refuses an unspecified source outright.",
    ("forwarded source", "unspecified IPv6"): "As the other loopback: the Hub refuses an unspecified source outright.",
    ("forwarded source", "IPv4 with a zone"): "The Hub drops a `%zone` suffix before parsing; the daemon's parser "
                                              "rejects one on IPv4. tailscaled writes a bare address.",
    ("forwarded source", "own link-local with a zone"): "The Hub drops the zone and finds the address on this "
                                                        "machine; the daemon keeps the zone in the source but strips "
                                                        "it from interface addresses, so they do not match. tailscaled "
                                                        "names a tailnet source, never a link-local one.",
    ("peer verdict", "child of the service"): "The daemon also accepts the `tailscaled.exe /subproc` child the service "
                                              "starts, which owns Serve's socket (agent-harness b55cdcc9: PID equality "
                                              "\"refused every real Serve request\"). The Hub accepts only the "
                                              "service's own process and has not followed yet.",
}


# --- loading the daemon's module -------------------------------------------------------------------------------------

def _no_lookup(*_args, **_kwargs):
    raise AssertionError("the daemon's tailscale_peer looked up a process or interface at import time")


@pytest.fixture(scope="module")
def peer():
    """agent-harness `harness/tailscale_peer.py` from the HUB_E2E_DAEMON_SRC checkout, imported under its own name.
    Every psutil lookup raises while it loads, so an import-time lookup fails here."""
    src = daemon_source()
    if src is None:
        pytest.skip("set HUB_E2E_DAEMON_SRC to an agent-harness checkout")
    path = src / "harness" / "tailscale_peer.py"
    spec = importlib.util.spec_from_file_location("hub_conformance_tailscale_peer", path)
    assert spec and spec.loader, f"cannot load {path}"
    module = importlib.util.module_from_spec(spec)
    with pytest.MonkeyPatch.context() as mp:
        for name in ("net_connections", "net_if_addrs", "Process", "process_iter", "pids", "win_service_get",
                     "win_service_iter"):
            if hasattr(psutil, name):
                mp.setattr(psutil, name, _no_lookup)
        mp.syspath_prepend(str(src))  # absolute `harness.*` imports, if it grows any
        spec.loader.exec_module(module)
    yield module
    module.EXECUTOR.shutdown(wait=False)


def compare(decision: str, cases: dict[str, object], hub, daemon) -> None:
    """Ask both copies every case. Report every unexpected difference, and every listed one that no longer differs."""
    unexpected, stale = [], []
    for case, value in cases.items():
        h, d = hub(value), daemon(value)
        reason = KNOWN.get((decision, case))
        if reason is None and h != d:
            unexpected.append(f"{decision} / {case} {value!r}: Hub {h!r}, daemon {d!r}")
        elif reason is not None and h == d:
            stale.append(f"{decision} / {case} {value!r}: both {h!r}; remove it from KNOWN")
    listed = {case for (dec, case) in KNOWN if dec == decision}
    missing = sorted(listed - cases.keys())
    assert not (unexpected or stale or missing), "\n".join(
        [*unexpected, *stale, *(f"{decision} / {case}: in KNOWN but no such case" for case in missing)])


def test_tailnet_known_differences_all_name_a_decision():
    decisions = {"executable", "same address", "loopback server address", "raw TCP forward", "forwarded source",
                 "peer verdict"}
    assert {dec for dec, _ in KNOWN} <= decisions
    assert all(reason.strip() for reason in KNOWN.values())


def test_tailnet_shared_constants(peer):
    names = ("HEADER_PREFIX", "FORWARDED_FOR", "TAILSCALED", "TAILSCALE_CLI", "SERVICE", "CACHE_SIZE",
             "CACHE_SECONDS")
    assert {n: getattr(tailnet, n) for n in names} == {n: getattr(peer, n) for n in names}


# --- executable and install directories --------------------------------------------------------------------------

def test_tailnet_platform_support(peer, monkeypatch):
    for platform in ("win32", "linux", "darwin", "cygwin"):
        monkeypatch.setattr(sys, "platform", platform)
        assert tailnet.supported() == peer.supported(), f"supported() on {platform}"


@pytest.mark.parametrize("env", [
    {"ProgramW6432": "C:\\Program Files", "ProgramFiles": "C:\\Program Files (x86)"},
    {"ProgramW6432": "C:\\Program Files", "ProgramFiles": "C:\\Program Files"},
    {"ProgramFiles": "D:\\Apps"},
    {"ProgramW6432": "D:\\Apps"},
    {"ProgramW6432": "", "ProgramFiles": ""},
    {},
], ids=["both", "same", "ProgramFiles only", "ProgramW6432 only", "empty", "neither"])
def test_tailnet_install_directories(peer, monkeypatch, env):
    for name in ("ProgramW6432", "ProgramFiles"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    assert tailnet.install_dirs() == peer.install_dirs()


def test_tailnet_executable_matching(peer, tmp_path):
    install = tmp_path / "Program Files" / "Tailscale"
    install.mkdir(parents=True)
    other = tmp_path / "Users" / "someone" / "Tailscale"
    other.mkdir(parents=True)
    link = tmp_path / "linked"
    with contextlib.suppress(OSError):  # symlinks may need a privilege on Windows
        link.symlink_to(install, target_is_directory=True)
    exe = peer.TAILSCALED
    cases = {
        "in the install directory": (str(install / exe), [install]),
        "through ..": (str(install / ".." / "Tailscale" / exe), [install]),
        "second install directory": (str(install / exe), [other, install]),
        "upper case name": (str(install / exe.upper()), [install]),
        "empty": ("", [install]),
        "the CLI, not the daemon": (str(install / peer.TAILSCALE_CLI), [install]),
        "below the install directory": (str(install / "sub" / exe), [install]),
        "another directory named Tailscale": (str(other / exe), [install]),
        "no install directory": (str(install / exe), []),
        "name without .exe": (str(install / "tailscaled"), [install]),
    }
    if link.is_symlink():
        cases["through a linked directory"] = (str(link / exe), [install])
        cases["linked install directory"] = (str(install / exe), [link])
    compare("executable", cases, lambda c: tailnet.is_tailscaled(*c), lambda c: peer.is_tailscaled(*c))


# --- loopback and same address -------------------------------------------------------------------------------------

def test_tailnet_same_address(peer):
    cases = {
        "equal IPv4": ("127.0.0.1", "127.0.0.1"),
        "mapped and plain": ("::ffff:127.0.0.1", "127.0.0.1"),
        "plain and mapped": ("127.0.0.1", "::ffff:127.0.0.1"),
        "IPv6 spellings": ("::1", "0:0:0:0:0:0:0:1"),
        "different": ("127.0.0.1", "127.0.0.2"),
        "IPv4 and IPv6 loopback": ("127.0.0.1", "::1"),
        "same zone": ("fe80::1%eth0", "fe80::1%eth0"),
        "zone and none": ("fe80::1%eth0", "fe80::1"),
        "not addresses": ("localhost", "localhost"),
        "one not an address": ("127.0.0.1", "localhost"),
        "empty": ("", ""),
        "padded": (" 127.0.0.1", "127.0.0.1"),
    }
    compare("same address", cases, lambda c: tailnet._same_ip(*c), lambda c: peer._same_ip(*c))


def test_tailnet_loopback_server_address(peer):
    cases = {
        "IPv4": "127.0.0.1",
        "IPv4 range": "127.8.9.10",
        "IPv6": "::1",
        "mapped IPv4": "::ffff:127.0.0.1",
        "IPv6 with a zone": "::1%1",
        "padded": " 127.0.0.1 ",
        "LAN": LAN,
        "unspecified": "0.0.0.0",  # noqa: S104 - an input, not a bind
        "Tailscale": NODE_V4,
        "host name": "localhost",
        "socket path": "/run/hub.sock",
        "empty": "",
        "missing": None,
        "IPv4 with a zone": "127.0.0.1%1",
    }
    compare("loopback server address", cases, tailnet.is_loopback, peer.is_loopback)


# --- raw TCP forward in a serve-config document --------------------------------------------------------------------

def _tcp(target) -> dict:
    return {"TCP": {"443": {"HTTPS": True}, "5432": {"TCPForward": target}}}


WEB = {"TCP": {"443": {"HTTPS": True}},
       "Web": {"node.example.ts.net:443": {"Handlers": {"/": {"Proxy": f"http://127.0.0.1:{HUB_PORT}"}}}}}

SERVE_DOCUMENTS = {
    "empty config": {},
    "web only": WEB,
    "forward to the port": _tcp(f"127.0.0.1:{HUB_PORT}"),
    "forward to another port": _tcp("127.0.0.1:9000"),
    "forward by host name": _tcp(f"localhost:{HUB_PORT}"),
    "forward to IPv6": _tcp(f"[::1]:{HUB_PORT}"),
    "forward with a scheme": _tcp(f"tcp://127.0.0.1:{HUB_PORT}"),
    "padded target": _tcp(f" 127.0.0.1:{HUB_PORT} "),
    "leading zero port": _tcp(f"127.0.0.1:0{HUB_PORT}"),
    "foreground session": {"Foreground": {"a1b2": _tcp(f"127.0.0.1:{HUB_PORT}")}},
    "service": {"Services": {"svc:hub": {"TCP": {"443": {"TCPForward": f"127.0.0.1:{HUB_PORT}",
                                                         "TerminateTLS": "hub.example.ts.net"}}}}},
    "service, another port": {"Services": {"svc:db": _tcp("127.0.0.1:5432")}},
    "inside a list": {"Extra": [_tcp(f"127.0.0.1:{HUB_PORT}")]},
    "unreadable port": _tcp("127.0.0.1:http"),
    "no port": _tcp("127.0.0.1"),
    "bare port": _tcp(str(HUB_PORT)),
    "numeric port": _tcp(HUB_PORT),
    "bare other port": _tcp("9000"),
    "empty host, other port": _tcp(":9000"),
    "empty target": _tcp(""),
    "null target": _tcp(None),
    "numeric other port": _tcp(9000),
    "null config": None,
    "list config": [WEB],
}


@pytest.fixture
def serve_cli(monkeypatch, tmp_path):
    """A Tailscale install directory with a CLI file in it, and `subprocess.run` answering `serve status --json` with
    `serve_cli.stdout`. `serve_cli.calls` records each command line."""
    root = tmp_path / "Program Files"
    (root / "Tailscale").mkdir(parents=True)
    cli = root / "Tailscale" / tailnet.TAILSCALE_CLI
    cli.write_text("", encoding="utf-8")
    monkeypatch.setenv("ProgramFiles", str(root))
    monkeypatch.delenv("ProgramW6432", raising=False)

    class Cli:
        path = cli
        stdout = "{}"
        calls: list[list[str]] = []

        @staticmethod
        def run(args, *_a, **_kw):
            Cli.calls.append(list(args))
            return subprocess.CompletedProcess(args, 0, stdout=Cli.stdout, stderr="")

    monkeypatch.setattr(subprocess, "run", Cli.run)
    return Cli


def _daemon_refuses_tcp(peer, port: int) -> bool:
    """The daemon's lookup: read the config, and refuse when it cannot or when it forwards raw TCP to the port."""
    try:
        return peer.raw_forward_to(peer.serve_config(), port)
    except Exception:  # noqa: BLE001 - as PeerCheck.verify: an unread serve config refuses
        return True


def test_tailnet_raw_tcp_forward(peer, serve_cli):
    def ask(side):
        def run(document):
            serve_cli.stdout = document if isinstance(document, str) else json.dumps(document)
            return side()
        return run

    cases = dict(SERVE_DOCUMENTS, **{"not JSON": "tailscale: serve config unavailable"})
    compare("raw TCP forward", cases, ask(lambda: tailnet.PeerCheck().raw_forward_to(HUB_PORT)),
            ask(lambda: _daemon_refuses_tcp(peer, HUB_PORT)))
    # Both ask the same CLI the same question.
    assert {tuple(c) for c in serve_cli.calls} == {(str(serve_cli.path), "serve", "status", "--json")}


def test_tailnet_raw_tcp_forward_without_a_cli(peer, serve_cli):
    serve_cli.path.unlink()
    assert tailnet.PeerCheck().raw_forward_to(HUB_PORT) is True
    assert _daemon_refuses_tcp(peer, HUB_PORT) is True
    assert serve_cli.calls == []


# --- forwarded source ----------------------------------------------------------------------------------------------

Addr = namedtuple("Addr", "family address netmask broadcast ptp")
AF_LINK = getattr(psutil, "AF_LINK", -1)
INTERFACES = {
    "lo": [Addr(socket.AF_INET, "127.0.0.1", None, None, None), Addr(socket.AF_INET6, "::1", None, None, None)],
    "Ethernet": [Addr(socket.AF_INET, LAN, None, None, None), Addr(socket.AF_INET6, "fe80::1%eth0", None, None, None),
                 Addr(AF_LINK, "00-15-5D-01-02-03", None, None, None)],
    "Tailscale": [Addr(socket.AF_INET, NODE_V4, None, None, None), Addr(socket.AF_INET6, NODE_V6, None, None, None)],
}

XFF = b"x-forwarded-for"
SOURCES = {
    "missing": [],
    "another node": [(XFF, PEER.encode())],
    "another node, IPv6": [(XFF, b"fd7a:115c:a1e0::2")],
    "another node, mapped": [(XFF, f"::ffff:{PEER}".encode())],
    "another node, padded": [(XFF, f" {PEER} ".encode())],
    "header name in capitals": [(b"X-Forwarded-For", PEER.encode())],
    "this node": [(XFF, NODE_V4.encode())],
    "this node, IPv6": [(XFF, NODE_V6.encode())],
    "this node, mapped": [(XFF, f"::ffff:{NODE_V4}".encode())],
    "this machine's LAN": [(XFF, LAN.encode())],
    "loopback": [(XFF, b"127.0.0.1")],
    "IPv6 loopback": [(XFF, b"::1")],
    "two headers": [(XFF, PEER.encode()), (XFF, b"100.64.0.3")],
    "a list in one header": [(XFF, f"{PEER}, 100.64.0.3".encode())],
    "not an address": [(XFF, b"peer.example.ts.net")],
    "empty": [(XFF, b"")],
    "other loopback": [(XFF, b"127.0.0.2")],
    "unspecified IPv4": [(XFF, b"0.0.0.0")],
    "unspecified IPv6": [(XFF, b"::")],
    "IPv4 with a zone": [(XFF, f"{PEER}%1".encode())],
    "own link-local with a zone": [(XFF, b"fe80::1%eth0")],
}


def test_tailnet_forwarded_source(peer, monkeypatch):
    """A verified tailscaled request counts only when its one forwarded source is another node. The Hub knows this
    machine's addresses from its interfaces; the daemon from `tailscale ip` (here the node's Tailscale addresses) and
    its interfaces. Both read the same synthetic interface table."""
    monkeypatch.setattr(psutil, "net_if_addrs", lambda: INTERFACES)

    def hub(headers):
        scope = {"type": "http", "headers": headers}
        return tailnet.PeerCheck(own_addresses=tailnet.local_addresses).remote(tailnet.forwarded_source(scope),
                                                                                fresh=True)

    def daemon(headers):
        check = peer.PeerCheck(tailscale_ips=lambda: frozenset({NODE_V4, NODE_V6}), interface_addrs=peer.interface_ips)
        return check.from_other_node(peer.forwarded_for({"type": "http", "headers": headers}))

    compare("forwarded source", SOURCES, hub, daemon)


# --- identity headers ----------------------------------------------------------------------------------------------

IDENTITY_SCOPES = {
    "no headers key": {"type": "http"},
    "headers None": {"type": "http", "headers": None},
    "no headers": {"type": "http", "headers": []},
    "login": {"type": "http", "headers": [(b"host", b"hub"), (b"tailscale-user-login", b"a@example.com")]},
    "name in capitals": {"type": "http", "headers": [(b"Tailscale-User-Name", b"A")]},
    "all capitals": {"type": "websocket", "headers": [(b"TAILSCALE-USER-LOGIN", b"a@example.com")]},
    "several": {"type": "http", "headers": [(b"tailscale-user-login", b"a"), (b"accept", b"*/*"),
                                            (b"tailscale-user-profile-pic", b"p"),
                                            (b"tailscale-app-capabilities", b"{}")]},
    "prefix inside the name": {"type": "http", "headers": [(b"x-tailscale-user-login", b"a")]},
    "no dash": {"type": "http", "headers": [(b"tailscale", b"a")]},
    "forwarded for only": {"type": "http", "headers": [(b"x-forwarded-for", PEER.encode())]},
}


def test_tailnet_identity_headers(peer):
    for case, scope in IDENTITY_SCOPES.items():
        assert tailnet.has_identity(scope) == peer.has_identity(scope), f"has_identity / {case}"
        hub, daemon = dict(scope), dict(scope)
        tailnet.strip_identity(hub)
        peer.strip_identity(daemon)
        assert hub == daemon, f"strip_identity / {case}"


# --- the peer verdict cache and the verdict --------------------------------------------------------------------------

def test_tailnet_verdict_cache(peer):
    def trace(cls):
        now = [100.0]
        check = cls(clock=lambda: now[0], size=2, ttl=5.0)
        a, b, c = ("127.0.0.1", 50001), ("127.0.0.1", 50002), ("::1", 50003)
        out = [check.cached(a)]
        check._remember(a, True)
        check._remember(b, False)
        out += [check.cached(a), check.cached(b)]
        now[0] += 4.9
        out.append(check.cached(a))  # a is now the most recent
        check._remember(c, True)  # evicts b, the least recent
        out += [check.cached(b), check.cached(c)]
        now[0] += 0.1  # a's verdict expires at exactly ttl
        out += [check.cached(a), check.cached(c)]
        return out

    assert trace(tailnet.PeerCheck) == trace(peer.PeerCheck)


SERVICE_PID, CHILD_PID, OTHER_PID = 4100, 4200, 4300


def test_tailnet_peer_verdict(peer, tmp_path):
    """Whether a connection's peer is tailscaled, on facts both copies take as injected seams: the connection's owner,
    the service's PID, the owner's parent and executable. The daemon also reads the serve config here; it is clean."""
    install = tmp_path / "Tailscale"
    install.mkdir()
    good, bad = str(install / tailnet.TAILSCALED), str(tmp_path / "Downloads" / tailnet.TAILSCALED)
    loop = ("127.0.0.1", 52000)
    hub_srv = ("127.0.0.1", HUB_PORT)
    cases = {
        # (client, server, platform supported, owner PID, service PID, owner's parent, owner's exe)
        "the service, installed": (loop, hub_srv, True, SERVICE_PID, SERVICE_PID, 4, good),
        "the service, IPv6": (("::1", 52000), ("::1", HUB_PORT), True, SERVICE_PID, SERVICE_PID, 4, good),
        "the service, elsewhere": (loop, hub_srv, True, SERVICE_PID, SERVICE_PID, 4, bad),
        "another process": (loop, hub_srv, True, OTHER_PID, SERVICE_PID, 1000, good),
        "no owner": (loop, hub_srv, True, None, SERVICE_PID, 0, good),
        "service stopped": (loop, hub_srv, True, SERVICE_PID, None, 4, good),
        "owner lookup fails": (loop, hub_srv, True, RuntimeError("access denied"), SERVICE_PID, 4, good),
        "non-loopback server": ((PEER, 52000), (LAN, HUB_PORT), True, SERVICE_PID, SERVICE_PID, 4, good),
        "no client": (None, hub_srv, True, SERVICE_PID, SERVICE_PID, 4, good),
        "no server": (loop, None, True, SERVICE_PID, SERVICE_PID, 4, good),
        "unsupported platform": (loop, hub_srv, False, SERVICE_PID, SERVICE_PID, 4, good),
        "child of the service": (loop, hub_srv, True, CHILD_PID, SERVICE_PID, SERVICE_PID, good),
    }

    def seams(case):
        _client, _server, ok, owner, service, parent, exe = case

        def find_owner(_client, _server):
            if isinstance(owner, Exception):
                raise owner
            return owner
        return {"owner": find_owner, "exe": lambda _pid: exe, "dirs": lambda: [install],
                "service": lambda: service, "platform_ok": lambda: ok}, parent

    def hub(case):
        kwargs, _parent = seams(case)
        return tailnet.PeerCheck(**kwargs).verify(case[0], case[1], fresh=True)

    def daemon(case):
        kwargs, parent = seams(case)
        check = peer.PeerCheck(**kwargs, child_of=lambda _pid, service: parent == service, serve=lambda: {})
        return check.verify(case[0], case[1])

    compare("peer verdict", cases, hub, daemon)
