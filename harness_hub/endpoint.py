"""Who answers at the daemon's address, checked before the Hub sends it anything (#264).

The Hub's daemon credentials (its `hub` key, and the verifier of a claim in progress) must reach only the daemon. An
https daemon URL is checked by TLS: the certificate must be valid for the URL's host, from the system's CAs or
`SSL_CERT_FILE` / `SSL_CERT_DIR`. A loopback `http://` URL is checked by nothing on the wire: a loopback address limits
who can reach the port, not who listens on it, and any local account can listen on a free port. The daemon offers no
TLS or owner-protected local-socket listener (agent-harness at the commit CI pins), so the Hub checks the listener
itself, in the operating system's connection table.

For each new TCP connection to a loopback `http://` daemon, after the handshake and before a byte is written, the Hub
finds the socket at the other end of that very connection and the account that owns it. The connection is used only
when that is the daemon's account: the account the Hub runs as, unless `--daemon-owner` names another. Otherwise it is
closed unused and the call fails with `EndpointUnverified`, which names the account found and how to fix it. The
check is in the connection pool's network backend, so it covers every call the same way: the public `/health`, the
claim and every owner API call, including the reads Overview sends in parallel. A kept-alive connection was checked
when it opened, and the far end of an open TCP connection cannot change.

- Linux: `/proc/net/tcp` and `/proc/net/tcp6` give each socket's owner uid. A connection the daemon has not accepted
  yet is listed with its listener's uid. Root, or the daemon's own account, can still answer: both can read the Hub's
  owner-only state directory anyway.
- Windows: the TCP table gives the process at the other end (psutil, as `tailnet.py` uses it) and its token the
  account. The Hub cannot read the account of another user's process, and that refuses the connection too.
- Elsewhere (macOS, the BSDs) there is no check, so a loopback `http://` daemon is refused unless the Hub runs with
  `--allow-insecure-http`.

`--allow-insecure-http` is the explicit compatibility setting: no check, as before this change, and a warning when
the Hub starts.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import struct
import sys
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from urllib.parse import urlsplit

import anyio
import httpcore
import httpx

from .config import is_loopback

log = logging.getLogger("harness_hub.endpoint")

PLATFORM = sys.platform  # the platform whose connection table the check reads; tests replace it

PROC_TABLES = ("/proc/net/tcp", "/proc/net/tcp6")
# The far end of a connection the Hub has just opened: established, or still a request socket (SYN_RECV, listed with
# its listener's uid) until the daemon's side has processed the handshake's last ACK.
PROC_LIVE_STATES = frozenset({"01", "03"})
# httpx's own defaults (httpx.AsyncClient builds its transport with these when none is given).
LIMITS = httpx.Limits(max_connections=100, max_keepalive_connections=20, keepalive_expiry=5.0)
FIX = ("Start the daemon as {owner}, set --daemon-owner to the account that runs it, or use an https daemon URL. "
       "--allow-insecure-http turns the check off (unsafe).")

Address = tuple  # (host, port, ...) as the socket module gives it


class EndpointUnverified(httpcore.ConnectError):
    """The other end of a connection to the daemon is not known to be the daemon's account; nothing was sent on it.

    A `ConnectError`, so callers that tell sent from unsent requests count it as unsent."""


def _ip(host: object) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    """An address without its IPv6 zone, an IPv4-mapped IPv6 address as IPv4."""
    ip = ipaddress.ip_address(str(host).split("%", 1)[0])
    return ip.ipv4_mapped if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped else ip


def _same(a: Address, b: tuple[object, int]) -> bool:
    try:
        return int(a[1]) == int(b[1]) and _ip(a[0]) == _ip(b[0])
    except (ValueError, TypeError, IndexError):
        return False


# --- Linux ------------------------------------------------------------------------------------------------------------

def _proc_hex(ip: ipaddress.IPv4Address | ipaddress.IPv6Address, port: int, table: str) -> str | None:
    """An address and port as `table` prints them: each 32-bit word of the address in host byte order, then the port,
    in upper-case hex. An IPv4 address appears in tcp6 in its IPv4-mapped form (a dual-stack socket)."""
    if table.endswith("6"):
        packed = ip.packed if ip.version == 6 else ipaddress.IPv6Address(b"\0" * 10 + b"\xff\xff" + ip.packed).packed
    elif ip.version == 4:
        packed = ip.packed
    else:
        return None
    words = struct.unpack(f"={len(packed) // 4}I", packed)
    return "".join(f"{word:08X}" for word in words) + f":{port:04X}"


def proc_owners(local: Address, remote: Address, tables: Iterable[str] = PROC_TABLES) -> list[int]:
    """The uids of the live sockets at the other end of the connection `local` -> `remote`: those whose own address
    is `remote` and whose peer is `local`."""
    near, far = (_ip(local[0]), int(local[1])), (_ip(remote[0]), int(remote[1]))
    uids = []
    for table in tables:
        own, peer = _proc_hex(*far, table), _proc_hex(*near, table)
        if own is None or peer is None:
            continue
        try:
            with open(table, encoding="ascii") as rows:
                next(rows, None)  # the header
                for row in rows:
                    fields = row.split()
                    if (len(fields) > 7 and fields[1].upper() == own and fields[2].upper() == peer
                            and fields[3] in PROC_LIVE_STATES):
                        uids.append(int(fields[7]))
        except FileNotFoundError:
            if table.endswith("6"):
                continue  # a kernel without IPv6
            raise
    return uids


def uid_name(uid: int) -> str:
    try:
        import pwd

        return f"{pwd.getpwuid(uid).pw_name} (uid {uid})"
    except (ImportError, KeyError):
        return f"uid {uid}"


def resolve_uid(owner: str) -> int:
    """A Linux account named by user name or uid. Raises ValueError for an account this machine does not have."""
    import pwd

    try:
        return pwd.getpwnam(owner).pw_uid
    except KeyError:
        pass
    if owner.isdigit():
        return int(owner)
    raise ValueError(f"--daemon-owner: no account named {owner!r} on this machine")


# --- Windows ----------------------------------------------------------------------------------------------------------

UNREADABLE = "an account this Hub cannot read"


def windows_owners(local: Address, remote: Address) -> list[str]:
    """The accounts (DOMAIN\\user) of the processes at the other end of the connection `local` -> `remote`, from the
    TCP table. A process whose account cannot be read (another user's) counts as `UNREADABLE`."""
    import psutil  # Windows-only dependency, as in tailnet.py

    accounts = []
    for conn in psutil.net_connections(kind="tcp"):
        if (conn.laddr and conn.raddr and _same(conn.laddr, remote) and _same(conn.raddr, local)
                and conn.status in (psutil.CONN_ESTABLISHED, psutil.CONN_SYN_RECV)):
            try:
                accounts.append(psutil.Process(conn.pid).username() if conn.pid else UNREADABLE)
            except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess, OSError):
                accounts.append(UNREADABLE)
    return accounts


def windows_account() -> str:
    import psutil

    return psutil.Process().username()


# --- the check --------------------------------------------------------------------------------------------------------

def supported(platform: str | None = None) -> bool:
    return (platform or PLATFORM) in ("linux", "win32")


@dataclass(frozen=True)
class Owner:
    """The account the daemon's listener must belong to: a uid on Linux, DOMAIN\\user on Windows."""

    uid: int | None = None
    account: str = ""

    def describe(self) -> str:
        return uid_name(self.uid) if self.uid is not None else self.account or "the daemon's account"


def resolve_owner(owner: str = "", platform: str | None = None) -> Owner:
    """The expected owner: `owner` (a user name or uid on Linux, DOMAIN\\user on Windows), or when empty the account
    this Hub runs as. Raises ValueError for one that cannot be the daemon's. Elsewhere the check is not supported,
    and the owner is only kept for the error message."""
    owner, platform = owner.strip(), platform or PLATFORM
    if platform == "linux":
        return Owner(uid=resolve_uid(owner) if owner else os.geteuid())
    if platform == "win32":
        if owner and "\\" not in owner:
            raise ValueError("--daemon-owner on Windows is DOMAIN\\user (or COMPUTER\\user), as Task Manager's "
                             "Details tab shows it")
        return Owner(account=owner or windows_account())
    return Owner(account=owner)


class ListenerCheck:
    """Whether the other end of a new connection to the daemon belongs to the expected account. Called with the
    connection's own address and the daemon's, it returns when it does and raises `EndpointUnverified` otherwise."""

    def __init__(self, url: str, owner: Owner, *, platform: str | None = None,
                 proc: Callable[[Address, Address], list[int]] = proc_owners,
                 windows: Callable[[Address, Address], list[str]] = windows_owners):
        self.url, self.owner, self.platform = url, owner, platform or PLATFORM
        self.proc, self.windows = proc, windows
        self._lock = threading.Lock()
        self._failure = ""  # the last refusal, so the log names each change once, not every poll
        self.checks = 0

    def __call__(self, local: Address, remote: Address) -> None:
        self.checks += 1
        try:
            refusal = self._refusal(local, remote)
        except Exception as exc:  # noqa: BLE001 - a lookup that fails proves nothing
            refusal = (f"the daemon URL {self.url} is not verified: the Hub could not read the connection table "
                       f"({type(exc).__name__}: {exc}), so it sent nothing. " + FIX.format(owner=self.owner.describe()))
        self._remember(refusal)
        if refusal:
            raise EndpointUnverified(refusal)

    def _refusal(self, local: Address, remote: Address) -> str:
        expected = self.owner.describe()
        if not local or not remote:
            return f"the daemon URL {self.url} is not verified: the connection has no address to look up."
        if self.platform == "linux":
            found = self.proc(local, remote)
            matches = bool(found) and all(uid == self.owner.uid for uid in found)
            names = sorted({uid_name(uid) for uid in found})
        elif self.platform == "win32":
            accounts = self.windows(local, remote)
            matches = bool(accounts) and all(a.casefold() == self.owner.account.casefold() for a in accounts)
            names = sorted(set(accounts))
        else:
            return (f"the daemon URL {self.url} is not verified: on this platform ({self.platform}) the Hub cannot "
                    "tell which account listens on a loopback port, so it sends the daemon nothing. Use an https "
                    "daemon URL, or --allow-insecure-http to send the Hub key without the check (unsafe).")
        if matches:
            return ""
        if not names:
            return (f"the daemon URL {self.url} is not verified: the connection table has no socket at the other "
                    "end of the Hub's connection, so it sent nothing. " + FIX.format(owner=expected))
        return (f"the daemon URL {self.url} is not verified: it is answered by a process of {', '.join(names)}, "
                f"not of {expected}, the daemon's account, so the Hub sent it nothing. " + FIX.format(owner=expected))

    def _remember(self, refusal: str) -> None:
        with self._lock:
            changed, self._failure = refusal != self._failure, refusal
        if changed and refusal:
            log.warning("%s", refusal)
        elif changed:
            log.info("the daemon URL %s is verified again: its listener belongs to %s", self.url,
                     self.owner.describe())


def needs_check(url: str) -> bool:
    """Whether a daemon URL's listener must be checked: plain HTTP (always loopback; config.py refuses any other)."""
    parts = urlsplit(url)
    return parts.scheme == "http" and is_loopback(parts.hostname or "")


def listener_check(url: str, *, owner: str = "", allow_insecure_http: bool = False,
                   platform: str | None = None) -> ListenerCheck | None:
    """The check for a daemon URL, or None when it needs none: https, or plain HTTP with the explicit opt-in."""
    if allow_insecure_http or not needs_check(url):
        return None
    platform = platform or PLATFORM
    return ListenerCheck(url, resolve_owner(owner, platform), platform=platform)


INSECURE_WARNING = ("--allow-insecure-http: the Hub sends its daemon key and any claim verifier to whatever listens on "
                    "{url}, without checking which account that is: any local account can listen on a free port. "
                    "Remove the setting to restore the check (README, \"Daemon connection\").")


def identity_notice(url: str, check: ListenerCheck | None, *, allow_insecure_http: bool) -> tuple[int, str]:
    """The startup log line that says how this Hub verifies its daemon: a warning for the explicit opt-in, an error
    where a loopback http:// daemon cannot be checked, otherwise information."""
    if allow_insecure_http and needs_check(url):
        return logging.WARNING, INSECURE_WARNING.format(url=url)
    if check is None:
        return logging.INFO, f"the daemon at {url} is verified by its TLS certificate"
    if not supported(check.platform):
        return logging.ERROR, (f"the daemon URL {url} cannot be verified on this platform ({check.platform}), so the "
                               "Hub will send the daemon nothing. Use an https daemon URL, or --allow-insecure-http to "
                               "send the Hub key without the check (unsafe).")
    return logging.INFO, (f"the Hub sends the daemon at {url} nothing until the listener there is verified as "
                          f"{check.owner.describe()}'s, on each new connection")


# --- the transport ----------------------------------------------------------------------------------------------------

class CheckedBackend(httpcore.AsyncNetworkBackend):
    """httpcore's network backend, with each new TCP connection checked before it is handed to the pool. A connection
    that fails is closed before anything is written on it."""

    def __init__(self, check: Callable[[Address, Address], None], inner: httpcore.AsyncNetworkBackend | None = None):
        self.check = check
        self.inner = inner or httpcore.AnyIOBackend()

    async def connect_tcp(self, host: str, port: int, timeout: float | None = None, local_address: str | None = None,
                          socket_options=None) -> httpcore.AsyncNetworkStream:
        stream = await self.inner.connect_tcp(host, port, timeout=timeout, local_address=local_address,
                                              socket_options=socket_options)
        try:
            local, remote = stream.get_extra_info("client_addr"), stream.get_extra_info("server_addr")
            await anyio.to_thread.run_sync(self.check, local, remote)
        except BaseException:
            with anyio.CancelScope(shield=True):
                await stream.aclose()
            raise
        return stream

    async def connect_unix_socket(self, path: str, timeout: float | None = None,
                                  socket_options=None) -> httpcore.AsyncNetworkStream:
        raise EndpointUnverified("the Hub reaches its daemon over TCP only")

    async def sleep(self, seconds: float) -> None:
        await self.inner.sleep(seconds)


class DaemonTransport(httpx.AsyncHTTPTransport):
    """httpx's transport, without proxies, whose connections pass `check` (when there is one) before any request is
    written on them."""

    def __init__(self, verify, check: Callable[[Address, Address], None] | None = None):
        super().__init__(verify=verify, trust_env=False, limits=LIMITS)
        if check is not None:
            # The pool httpx built, rebuilt with the same settings and the checked network backend.
            self._pool = httpcore.AsyncConnectionPool(
                ssl_context=verify, max_connections=LIMITS.max_connections,
                max_keepalive_connections=LIMITS.max_keepalive_connections,
                keepalive_expiry=LIMITS.keepalive_expiry, http1=True, http2=False,
                network_backend=CheckedBackend(check))
