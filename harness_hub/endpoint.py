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
when it opened and is not checked again.

- Linux: `/proc/net/tcp` and `/proc/net/tcp6` give each socket's owner uid. A connection the daemon has not accepted
  yet is listed with its listener's uid. Root, or the daemon's own account, can still answer: both can read the Hub's
  owner-only state directory anyway. The check fails closed on an unmapped or overflow uid: it refuses when the
  Hub's own uid or the expected owner has no mapping in the Hub's user namespace, or when the expected owner is the
  kernel's overflow uid, since the table cannot single out an account there. `--daemon-owner` refuses `nobody` and
  the overflow uid.
- Windows: the TCP table (GetExtendedTcpTable, with each socket's creation time) names the process at the other end
  by PID, and that process's token gives the account. Windows reuses PIDs, so the socket counts as the process's only
  when the process also holds the port's listening socket and started before both sockets were created. A socket
  without a creation time refuses, as does another user's process, whose account the Hub cannot read.
- Elsewhere (macOS, the BSDs) there is no check, so a loopback `http://` daemon is refused unless the Hub runs with
  `--allow-insecure-http`.

The check proves which account holds the socket at the other end, not which program that is or where it sends what
it gets. Root and the daemon's own account are outside it, and so is a same-account forwarder: a program of the
daemon's account that listens on the port and forwards (`ssh -L`, `socat`) passes the check.

`--allow-insecure-http` is the explicit compatibility setting: no check, as before this change, and a warning when
the Hub starts.
"""

from __future__ import annotations

import ctypes
import ipaddress
import logging
import os
import struct
import sys
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
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
# httpx's own defaults (httpx.AsyncClient builds its transport with these when none is given), and the pool settings
# both transports below use, checked or not.
LIMITS = httpx.Limits(max_connections=100, max_keepalive_connections=20, keepalive_expiry=5.0)
POOL = {"max_connections": LIMITS.max_connections, "max_keepalive_connections": LIMITS.max_keepalive_connections,
        "keepalive_expiry": LIMITS.keepalive_expiry, "http1": True, "http2": False}
FIX = ("Start the daemon as {owner}, set --daemon-owner to the account that runs it, or use an https daemon URL. "
       "--allow-insecure-http turns the check off (unsafe).")

Address = tuple[object, ...]  # (host, port, ...) as the socket module gives it


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


OVERFLOW_UID_FILE = "/proc/sys/kernel/overflowuid"
UID_MAP_FILE = "/proc/self/uid_map"
DEFAULT_OVERFLOW_UID = 65534  # the kernel's default overflowuid


def overflow_uid() -> int:
    """The kernel's overflow uid, which it shows for an account it cannot map (its default where the setting cannot be
    read)."""
    try:
        return int(Path(OVERFLOW_UID_FILE).read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return DEFAULT_OVERFLOW_UID


def uid_mapped(uid: int) -> bool:
    """Whether `uid` has a mapping in this process's user namespace (/proc/self/uid_map). A map that cannot be read
    counts as no mapping."""
    try:
        rows = Path(UID_MAP_FILE).read_text(encoding="ascii").splitlines()
    except OSError:
        return False
    for row in rows:
        fields = row.split()
        if len(fields) == 3 and all(field.isdigit() for field in fields):
            first, _, count = (int(field) for field in fields)
            if first <= uid < first + count:
                return True
    return False


def own_uid() -> int:
    """The Hub's effective uid as its user namespace shows it (-1 where there is none)."""
    geteuid = getattr(os, "geteuid", None)
    return geteuid() if geteuid else -1


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
# GetExtendedTcpTable's TCP_TABLE_OWNER_MODULE_ALL: the TCP table with each socket's creation time, which psutil's
# table (TCP_TABLE_OWNER_PID_ALL) leaves out.
TCP_TABLE_OWNER_MODULE_ALL = 8
WINDOWS_FAMILIES = {4: 2, 6: 23}  # AF_INET and AF_INET6 as Windows numbers them
ERROR_NOT_SUPPORTED, ERROR_INSUFFICIENT_BUFFER = 50, 122
TCP_LISTEN, TCP_SYN_RCVD, TCP_ESTABLISHED = 2, 4, 5  # MIB_TCP_STATE
FILETIME_UNIX_EPOCH = 116444736000000000  # 1970-01-01 as a FILETIME (100 ns units since 1601, UTC)


class _Row4(ctypes.Structure):
    """MIB_TCPROW_OWNER_MODULE. Addresses and ports are in network byte order, a port in its DWORD's first two
    bytes."""

    _fields_ = [("state", ctypes.c_uint32), ("local_addr", ctypes.c_ubyte * 4), ("local_port", ctypes.c_ubyte * 4),
                ("remote_addr", ctypes.c_ubyte * 4), ("remote_port", ctypes.c_ubyte * 4), ("pid", ctypes.c_uint32),
                ("created", ctypes.c_int64), ("module", ctypes.c_uint64 * 16)]


class _Row6(ctypes.Structure):
    """MIB_TCP6ROW_OWNER_MODULE."""

    _fields_ = [("local_addr", ctypes.c_ubyte * 16), ("local_scope", ctypes.c_uint32),
                ("local_port", ctypes.c_ubyte * 4), ("remote_addr", ctypes.c_ubyte * 16),
                ("remote_scope", ctypes.c_uint32), ("remote_port", ctypes.c_ubyte * 4), ("state", ctypes.c_uint32),
                ("pid", ctypes.c_uint32), ("created", ctypes.c_int64), ("module", ctypes.c_uint64 * 16)]


@dataclass(frozen=True)
class TcpRow:
    """A socket in Windows' TCP table: its state, its own and its peer's address, the PID that owns it, and when it
    was created (a FILETIME; 0 where Windows gives none)."""

    state: int
    local: tuple[str, int]
    remote: tuple[str, int]
    pid: int
    created: int


def parse_tcp_table(data: bytes, version: int) -> list[TcpRow]:
    """The rows of a TCP_TABLE_OWNER_MODULE_ALL table as GetExtendedTcpTable writes it: a 32-bit row count, then the
    rows, aligned as a row is. A table cut short raises ValueError."""
    row_type, ip = (_Row4, ipaddress.IPv4Address) if version == 4 else (_Row6, ipaddress.IPv6Address)
    (count,) = struct.unpack_from("=I", data)
    if not count:
        return []
    align = ctypes.alignment(row_type)
    rows = (row_type * count).from_buffer_copy(data, -(-4 // align) * align)

    def end(address, port) -> tuple[str, int]:
        return str(ip(bytes(address))), int.from_bytes(bytes(port)[:2], "big")

    return [TcpRow(row.state, end(row.local_addr, row.local_port), end(row.remote_addr, row.remote_port), row.pid,
                   row.created) for row in rows]


def windows_tcp_table(version: int) -> bytes:
    """Windows' TCP table for one IP version (TCP_TABLE_OWNER_MODULE_ALL), as GetExtendedTcpTable writes it. A
    machine without IPv6 has an empty IPv6 table."""
    get = ctypes.WinDLL("iphlpapi").GetExtendedTcpTable
    get.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32), ctypes.c_int, ctypes.c_uint32, ctypes.c_int,
                    ctypes.c_uint32]
    get.restype = ctypes.c_uint32
    size = ctypes.c_uint32(0)
    for _ in range(8):  # the table can grow between the call that sizes it and the one that fills it
        buffer = ctypes.create_string_buffer(size.value) if size.value else None
        error = get(buffer, ctypes.byref(size), False, WINDOWS_FAMILIES[version], TCP_TABLE_OWNER_MODULE_ALL, 0)
        if error == 0 and buffer is not None:
            return buffer.raw
        if error == ERROR_NOT_SUPPORTED and version == 6:
            return bytes(4)  # no IPv6 here: an empty table
        if error not in (0, ERROR_INSUFFICIENT_BUFFER):
            raise OSError(error, f"GetExtendedTcpTable failed with error {error}")
    raise OSError(ERROR_INSUFFICIENT_BUFFER, "the TCP table kept growing while the Hub read it")


def windows_rows() -> list[TcpRow]:
    return [row for version in (4, 6) for row in parse_tcp_table(windows_tcp_table(version), version)]


class Unattributed(Exception):
    """The socket at the other end cannot be tied to the process Windows' table names for it, so that process's
    account proves nothing about who holds the socket."""


def _listens_for(listener: tuple[str, int], end: tuple[str, int]) -> bool:
    """Whether a listening socket at `listener` takes connections to `end`: the same port, on that address or on
    every address."""
    ip = _ip(listener[0])
    return int(listener[1]) == int(end[1]) and (ip.is_unspecified or ip == _ip(end[0]))


def filetime_seconds(filetime: int) -> float:
    """A FILETIME in seconds since the epoch, as psutil gives a process's start."""
    return (filetime - FILETIME_UNIX_EPOCH) / 10_000_000


def windows_owners(local: Address, remote: Address, rows: Callable[[], list[TcpRow]] = windows_rows) -> list[str]:
    """The accounts (DOMAIN\\user) of the processes at the other end of the connection `local` -> `remote`, from
    Windows' TCP table. The table names a socket's process by PID, and Windows reuses PIDs, so a socket counts as its
    process's only when that process also holds the listening socket on its port and started before every socket the
    check relies on was created: the socket itself and each such listener. Otherwise, or when the table gives any of
    them no creation time, this raises `Unattributed`. A process whose account cannot be read (another user's) counts
    as `UNREADABLE`."""
    import psutil  # Windows-only dependency, as in tailnet.py

    table = rows()
    accounts = []
    for end in table:
        if end.state not in (TCP_ESTABLISHED, TCP_SYN_RCVD) or not (_same(end.local, remote)
                                                                    and _same(end.remote, local)):
            continue
        port = end.local[1]
        listeners = [row for row in table
                     if row.state == TCP_LISTEN and row.pid == end.pid and _listens_for(row.local, end.local)]
        if not end.pid or not listeners:
            raise Unattributed(f"the socket at the other end belongs to process {end.pid}, which holds no listening "
                               f"socket on port {port}")
        # Every socket the check relies on: the far end itself, and each listener of its process on the port.
        sockets = [("the socket at the other end", end)]
        sockets += [(f"the listening socket on port {port}", row) for row in listeners]
        for name, row in sockets:
            if row.created <= 0:
                raise Unattributed(f"Windows gives no creation time for {name}, so the Hub cannot tell whether process "
                                   f"{end.pid} created it")
        try:
            process = psutil.Process(end.pid)
            started = process.create_time()
        except (psutil.Error, OSError) as exc:
            raise Unattributed(f"the Hub cannot read when process {end.pid} started ({type(exc).__name__})") from exc
        for name, row in sockets:
            if started >= filetime_seconds(row.created):
                raise Unattributed(f"process {end.pid} started after {name} was created, so it is not the process "
                                   "that created that socket")
        try:
            account = process.username()
        except (psutil.Error, OSError):
            account = UNREADABLE
        if not process.is_running():
            raise Unattributed(f"process {end.pid} exited or changed while the Hub read it")
        accounts.append(account)
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
    if owner == "nobody" or (owner.isdigit() and int(owner) == overflow_uid()):
        raise ValueError(f"--daemon-owner cannot be nobody or the kernel's overflow uid ({overflow_uid()}): the "
                         "connection table cannot single out that account. Name the account that runs the daemon")
    if platform == "linux":
        if owner and resolve_uid(owner) == overflow_uid():
            raise ValueError(f"--daemon-owner {owner!r} is the kernel's overflow uid ({overflow_uid()}), which the "
                             "connection table cannot single out. Name the account that runs the daemon")
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
                 windows: Callable[[Address, Address], list[str]] = windows_owners,
                 overflow: Callable[[], int] = overflow_uid, mapped: Callable[[int], bool] = uid_mapped,
                 me: Callable[[], int] = own_uid):
        self.url, self.owner, self.platform = url, owner, platform or PLATFORM
        self.proc, self.windows = proc, windows
        self.overflow, self.mapped, self.me = overflow, mapped, me
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
            blocked = self.unidentifiable()
            if blocked:
                return blocked
            found = self.proc(local, remote)
            matches = bool(found) and all(uid == self.owner.uid for uid in found)
            names = sorted({uid_name(uid) for uid in found})
        elif self.platform == "win32":
            try:
                accounts = self.windows(local, remote)
            except Unattributed as exc:
                return (f"the daemon URL {self.url} is not verified: {exc}, so the Hub sent it nothing. "
                        + FIX.format(owner=expected))
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

    def unidentifiable(self) -> str:
        """Why the connection table cannot single out the expected owner here, or ''. The owner check fails closed on
        an unmapped or overflow uid: the Hub's own uid or the expected one without a mapping in the Hub's user
        namespace, or an expected owner that is the kernel's overflow uid. Only Linux has uids to check."""
        if self.platform != "linux":
            return ""
        overflow, me, expected = self.overflow(), self.me(), self.owner.uid
        if not self.mapped(me):
            reason = (f"this Hub's own uid is unmapped in its user namespace (it reads as {me}; the kernel's "
                      f"overflow uid is {overflow}), so the connection table cannot single out an account. Run the "
                      "Hub with its uid mapped, or outside the user namespace")
        elif expected is None or not self.mapped(expected):
            reason = (f"the expected owner, uid {expected}, is unmapped in this Hub's user namespace, so the "
                      "connection table cannot show it. Set --daemon-owner to a mapped account that runs the daemon")
        elif expected == overflow:
            reason = (f"the expected owner is uid {overflow}, the kernel's overflow uid, which the connection table "
                      "cannot single out. Set --daemon-owner to the account that runs the daemon")
        else:
            return ""
        return f"the daemon URL {self.url} is not verified: {reason}. The Hub sends it nothing."

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
    blocked = check.unidentifiable()
    if blocked:
        return logging.ERROR, blocked
    return logging.INFO, (f"the Hub sends the daemon at {url} nothing on a new connection until it has verified that "
                          f"the listener belongs to {check.owner.describe()}")


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
        super().__init__(verify=verify, trust_env=False, limits=LIMITS, http1=POOL["http1"], http2=POOL["http2"])
        if check is not None:
            # httpx keeps the connection pool in `_pool` (httpx is pinned). If a release moves it, refuse to start
            # rather than send without the check.
            if not isinstance(getattr(self, "_pool", None), httpcore.AsyncConnectionPool):
                raise RuntimeError("httpx's transport has no connection pool where the daemon identity check is "
                                   "installed; the Hub will not send its daemon anything without the check")
            self._pool = httpcore.AsyncConnectionPool(ssl_context=verify, network_backend=CheckedBackend(check), **POOL)
