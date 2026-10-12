"""Trust the Tailscale identity headers only from tailscaled.

`tailscale serve` forwards tailnet requests to the Hub's loopback listener and adds `Tailscale-User-Login` and the
other `Tailscale-*` identity headers. Any other program on this machine, or a container that reaches the host loopback,
can reach the same listener and send the same headers. So, like the daemon (agent-harness `harness/tailscale_peer.py`),
the Hub looks up who owns the other end of the TCP connection: the identity headers count only when that process is
the running Tailscale Windows service and its executable is tailscaled in the Tailscale install directory. Otherwise
they are removed before anything reads them. The install path alone is not enough: any local account can start that
same binary (say, as a userspace proxy) and send whatever headers it likes through it.

The check is implemented for Windows. Elsewhere the headers are removed unless the Hub runs with
`--trust-unverified-identity-headers`, which trusts any local process that sends them.

tailscaled proves that it added the headers, not who it added them for. serve runs WhoIs on the connection's tailnet
source, and a program on this machine that connects to the node's own serve address arrives from the node's own
Tailscale IP, which WhoIs resolves to the node's owner (checked on tailscaled 1.102.4 on Windows). So once the peer
is verified as tailscaled, and only then, the Hub reads the one header that names that source: serve replaces
`X-Forwarded-For` with it (a client's own value does not survive). The identity is removed when that header is
missing, carries more than one address, or names an address of this machine. Outside that verified case no
`X-Forwarded-*` header is ever read.

tailscaled also carries bytes it never parses: a raw TCP forward in the node's serve config (`tailscale serve --tcp`,
or `--tls-terminated-tcp`) passes a client's own `Tailscale-*` and `X-Forwarded-For` headers straight to the Hub from
the service's process. Anything that may change the serve config (the backend's user, local admins) could add one, so
sign-in reads the config through the LocalAPI (the `tailscale` CLI in the install directory) and refuses when any TCP
handler forwards to the Hub's port, or when the config cannot be read. This is best effort, not enforcement: it
catches only a forward still in place when the sign-in request arrives. Whatever can change the serve config (the
backend's user, local admins, a sandbox that can reach the tailscaled pipe) can open a connection through a raw
forward and remove the forward before sending the request. The owner accepted that residual risk.

tailscaled also carries a peer's bytes when this node advertises a subnet route: it ends a routed TCP connection
itself and opens a new one to the target from the service's own socket, which no serve config shows. With the Hub
listening on `0.0.0.0` or a LAN address that the route covers, a tailnet device could send its own identity headers
and pass every check above. `tailscale serve` proxies to the Hub's loopback listener, so the identity headers count
only on a connection whose server address is loopback; a missing or non-loopback one refuses them.

A trusted login is still not the owner: `POST /api/session` accepts it only if the daemon lists it in
`allowed_logins` (server.py).
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import os
import subprocess
import sys
import threading
import time
from collections import OrderedDict
from pathlib import Path

from starlette.types import ASGIApp, Receive, Scope, Send

log = logging.getLogger(__name__)

HEADER_PREFIX = b"tailscale-"
LOGIN_HEADER = b"tailscale-user-login"
FORWARDED_FOR = b"x-forwarded-for"
CACHE_SIZE = 256
CACHE_SECONDS = 5.0  # absolute, from the lookup: a closed connection's port can be reused by another process
TAILSCALED = "tailscaled.exe"
TAILSCALE_CLI = "tailscale.exe"
SERVE_STATUS_SECONDS = 10.0
SERVICE = "Tailscale"
SIGN_IN_PATH = "/api/session"


def supported() -> bool:
    return sys.platform == "win32"


def install_dirs() -> list[Path]:
    """Where the Tailscale installer puts tailscaled on Windows."""
    roots = {os.environ.get(name) for name in ("ProgramW6432", "ProgramFiles")}
    return [Path(root) / "Tailscale" for root in sorted(r for r in roots if r)]


def _same_ip(a: str, b: str) -> bool:
    try:
        x, y = ipaddress.ip_address(a), ipaddress.ip_address(b)
    except ValueError:
        return False
    if isinstance(x, ipaddress.IPv6Address) and x.ipv4_mapped:
        x = x.ipv4_mapped
    if isinstance(y, ipaddress.IPv6Address) and y.ipv4_mapped:
        y = y.ipv4_mapped
    return x == y


def connection_owner(client: tuple[str, int], server: tuple[str, int]) -> int | None:
    """The PID of the process whose socket is `client` and connects to `server`, from the OS connection table."""
    import psutil  # Windows-only dependency: the check runs nowhere else

    for conn in psutil.net_connections(kind="tcp"):
        laddr, raddr = conn.laddr, conn.raddr
        if (laddr and raddr and laddr.port == client[1] and raddr.port == server[1]
                and _same_ip(laddr.ip, client[0]) and _same_ip(raddr.ip, server[0])):
            return conn.pid
    return None


def process_exe(pid: int) -> str:
    import psutil

    return psutil.Process(pid).exe()


def service_pid() -> int | None:
    """The PID of the Tailscale Windows service, or None when it is not running. Needs only SERVICE_QUERY_STATUS,
    which a non-admin account has; a missing service raises."""
    import psutil

    service = psutil.win_service_get(SERVICE)
    return service.pid() if service.status() == "running" else None


def serve_config() -> object:
    """This node's serve config, foreground sessions and services included, from the LocalAPI through the installed
    CLI (`null` when there is none). Any failure raises."""
    cli = next((d / TAILSCALE_CLI for d in install_dirs() if (d / TAILSCALE_CLI).is_file()), None)
    if cli is None:
        raise FileNotFoundError(f"no {TAILSCALE_CLI} in {[str(d) for d in install_dirs()]}")
    # A fixed argument list and the CLI from the admin-owned install directory, never one found on PATH.
    done = subprocess.run([str(cli), "serve", "status", "--json"], capture_output=True,  # noqa: S603
                          encoding="utf-8", timeout=SERVE_STATUS_SECONDS, check=True,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return json.loads(done.stdout)


def tcp_forwards(config: object) -> list[str]:
    """Every raw TCP forward target (`TCPForward`) anywhere in a serve config: its own TCP handlers, those of
    foreground sessions (`Foreground`) and of services (`Services`)."""
    found: list[str] = []
    if isinstance(config, dict):
        for key, value in config.items():
            if key == "TCPForward":
                found.append(value if isinstance(value, str) else repr(value))
            else:
                found.extend(tcp_forwards(value))
    elif isinstance(config, list):
        for item in config:
            found.extend(tcp_forwards(item))
    return found


def _forward_port(target: str) -> int | None:
    text = target.strip()
    if "://" in text:
        text = text.split("://", 1)[1]
    host, sep, port = text.rpartition(":")
    try:
        return int(port) if sep and host else None
    except ValueError:
        return None


def local_addresses() -> set[str]:
    """Every address on this machine's interfaces, the node's Tailscale IPs among them."""
    import psutil

    return {a.address for addrs in psutil.net_if_addrs().values() for a in addrs}


def _ip(text: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        ip = ipaddress.ip_address(text.split("%", 1)[0].strip())
    except ValueError:
        return None
    return ip.ipv4_mapped if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped else ip


def is_loopback(host: object) -> bool:
    """Whether a connection's server address is loopback. A missing one, a host name or a socket path is not."""
    ip = _ip(host) if isinstance(host, str) and host else None
    return ip is not None and ip.is_loopback


def forwarded_source(scope: Scope) -> str:
    """The tailnet source address tailscaled set in `X-Forwarded-For`, or '' when there is not exactly one. Read it
    only on a connection verified to come from tailscaled."""
    values = [v for n, v in scope.get("headers") or () if n.lower() == FORWARDED_FOR]
    if len(values) != 1:
        return ""
    ip = _ip(values[0].decode("latin-1"))
    return str(ip) if ip else ""


def is_tailscaled(exe: str, dirs: list[Path]) -> bool:
    """The name alone proves nothing: the executable must sit directly in a Tailscale install directory."""
    if not exe:
        return False
    path = Path(os.path.normcase(os.path.realpath(exe)))
    if path.name != os.path.normcase(TAILSCALED):
        return False
    return any(path.parent == Path(os.path.normcase(os.path.realpath(d))) for d in dirs)


class PeerCheck:
    """Whether a connection's peer is the Tailscale service's tailscaled, on the Hub's loopback address. Verdicts are
    cached per (client host, port) for a few seconds so keep-alive requests do not repeat the lookup; a failed lookup
    is not cached and counts as not tailscaled. A cached verdict can outlive the connection it was made for, so
    sign-in asks for a fresh lookup (`fresh`)."""

    def __init__(self, *, owner=connection_owner, exe=process_exe, dirs=install_dirs, service=service_pid,
                 platform_ok=supported, own_addresses=local_addresses, serve=serve_config, server_ok=is_loopback,
                 clock=time.monotonic, size: int = CACHE_SIZE, ttl: float = CACHE_SECONDS):
        self.owner, self.exe, self.dirs, self.platform_ok, self.clock = owner, exe, dirs, platform_ok, clock
        self.own_addresses, self.service, self.serve, self.server_ok = own_addresses, service, serve, server_ok
        self.size, self.ttl = size, ttl
        self._cache: OrderedDict[tuple[str, int], tuple[float, bool]] = OrderedDict()
        self._lock = threading.Lock()
        self.lookups = 0

    def cached(self, client: tuple[str, int]) -> bool | None:
        with self._lock:
            hit = self._cache.get(client)
            if hit is None:
                return None
            if self.clock() >= hit[0]:
                del self._cache[client]
                return None
            self._cache.move_to_end(client)
            return hit[1]

    def _remember(self, client: tuple[str, int], verdict: bool) -> None:
        with self._lock:
            self._cache[client] = (self.clock() + self.ttl, verdict)
            self._cache.move_to_end(client)
            while len(self._cache) > self.size:
                self._cache.popitem(last=False)

    def verify(self, client: tuple[str, int] | None, server: tuple[str, int] | None, *, fresh: bool = False) -> bool:
        if not client or not server or not self.platform_ok():
            return False
        if not self.server_ok(server[0]):
            # A subnet-routed connection also comes from tailscaled's own socket, on the routed address.
            return False
        key = (str(client[0]), int(client[1]))
        hit = None if fresh else self.cached(key)
        if hit is not None:
            return hit
        self.lookups += 1
        try:
            pid = self.owner(key, (str(server[0]), int(server[1])))
            verdict = bool(pid) and pid == self.service() and is_tailscaled(self.exe(pid), self.dirs())
        except Exception as exc:  # noqa: BLE001 - any failure means the peer is not known to be tailscaled
            log.warning("could not identify the process behind a request with Tailscale identity headers: %s", exc)
            return False
        self._remember(key, verdict)
        return verdict

    def remote(self, source: str) -> bool:
        """Whether the tailnet source tailscaled reported is another node: not empty, not this machine."""
        ip = _ip(source) if source else None
        if ip is None or ip.is_loopback or ip.is_unspecified:
            return False
        try:
            own = {str(_ip(a)) for a in self.own_addresses() if _ip(a)}
        except Exception as exc:  # noqa: BLE001 - without this machine's addresses, no source can be cleared
            log.warning("could not list this machine's addresses to check a tailnet request's source: %s", exc)
            return False
        return str(ip) not in own


    def raw_forward_to(self, port: int | None) -> bool:
        """Whether a raw TCP forward in this node's serve config could reach the Hub's `port`, so tailscaled might
        be relaying a client's own headers. A config that cannot be read, or a target whose port is unclear, counts
        as one."""
        if not port:
            return True
        try:
            targets = tcp_forwards(self.serve())
        except Exception as exc:  # noqa: BLE001 - an unread config cannot be cleared
            log.warning("could not read this node's tailscale serve config: %s", exc)
            return True
        return any(_forward_port(target) in (None, port) for target in targets)


def has_identity(scope: Scope) -> bool:
    return any(name.lower().startswith(HEADER_PREFIX) for name, _ in scope.get("headers") or ())


def strip_identity(scope: Scope) -> None:
    """Remove every `Tailscale-*` header so no later reader sees an identity that tailscaled did not add."""
    scope["headers"] = [(n, v) for n, v in scope.get("headers") or () if not n.lower().startswith(HEADER_PREFIX)]


def login(headers) -> str:
    """The tailnet login on a request whose identity headers survived `TailnetIdentity`, or ''. Starlette `Headers`;
    more than one login header is ambiguous and counts as none."""
    values = headers.getlist("tailscale-user-login")
    if len(values) != 1:
        return ""
    value = values[0].strip()
    return value if 0 < len(value) <= 320 and value.isprintable() else ""


class TailnetIdentity:
    """Strip `Tailscale-*` headers unless tailscaled sent them. It runs before anything else reads a header."""

    def __init__(self, app: ASGIApp, *, check: PeerCheck | None = None, trust_unverified: bool = False):
        self.app = app
        self.check = check or PeerCheck()
        self.trust_unverified = trust_unverified

    async def trusted(self, scope: Scope) -> bool:
        check = self.check
        if not check.platform_ok():
            return self.trust_unverified  # no peer to verify, so X-Forwarded-For proves nothing either
        client, server = scope.get("client"), scope.get("server")
        # Sign-in turns the identity into a credential: never on a verdict cached for an earlier connection.
        fresh = scope["type"] == "http" and scope.get("method") == "POST" and scope.get("path") == SIGN_IN_PATH
        if fresh or (client and server and check.cached((str(client[0]), int(client[1]))) is None):
            verified = await asyncio.to_thread(check.verify, client, server, fresh=fresh)
        else:
            verified = check.verify(client, server)
        if not verified:
            return False
        source = forwarded_source(scope)
        if await asyncio.to_thread(check.remote, source):
            return True
        # Every request from a browser on this machine lands here, so only a sign-in attempt is worth a warning.
        log.log(logging.WARNING if fresh else logging.DEBUG, "ignored a tailnet identity from tailscaled whose "
                "source is %s", f"this machine ({source})" if source else "missing or ambiguous")
        return False

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in ("http", "websocket") and has_identity(scope) and not await self.trusted(scope):
            scope = dict(scope)
            strip_identity(scope)
        await self.app(scope, receive, send)
