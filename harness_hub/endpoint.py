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

Every input the check reads fails closed: a connection table, `/proc` file, socket creation time or process lookup
that cannot be read or parsed refuses the connection, on Linux and on Windows. Each record it parses is validated in
full: a /proc/net/tcp row must be in one of the two shapes the kernel prints, its state one that shape prints, and every
field in its printed format and within the kernel type it is printed from (or the constant the kernel prints there);
/proc/self/uid_map must be a map the kernel would write; and each Windows TCP row must have a state Windows defines and
a creation time that is a FILETIME.

The check proves which account holds the socket at the other end, not which program that is or where it sends what
it gets. Root and the daemon's own account are outside it, and so is a same-account forwarder: a program of the
daemon's account that listens on the port and forwards (`ssh -L`, `socat`) passes the check.

`--allow-insecure-http` is the explicit compatibility setting: no check, as before this change, and a warning when
the Hub starts.
"""

from __future__ import annotations

import ctypes
import errno
import ipaddress
import logging
import math
import os
import re
import socket
import struct
import sys
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path
from urllib.parse import urlsplit

import anyio
import httpcore
import httpx

from .config import is_loopback

log = logging.getLogger("harness_hub.endpoint")

PLATFORM = sys.platform  # the platform whose connection table the check reads; tests replace it

PROC_TABLES = ("/proc/net/tcp", "/proc/net/tcp6")
# Each table's header, by whether it is tcp6 (which names its third column remote_address).
PROC_HEADERS = {six: ["sl", "local_address", "remote_address" if six else "rem_address", "st", "tx_queue", "rx_queue",
                      "tr", "tm->when", "retrnsmt", "uid", "timeout", "inode"] for six in (False, True)}
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


def _address(address: Address) -> bool:
    """Whether a socket address has an IP address and a port the check can read."""
    try:
        _ip(address[0])
        return 0 <= int(address[1]) <= 65535
    except (ValueError, TypeError, IndexError):
        return False


# An address family the system does not support: EAFNOSUPPORT, and WSAEAFNOSUPPORT as Windows reports it.
NO_FAMILY = frozenset({errno.EAFNOSUPPORT, 10047})


def ipv6_available() -> bool:
    """Whether this machine has IPv6 at all, so that IPv6 and dual-stack sockets can exist: an AF_INET6 socket can be
    created. Only an unsupported address family counts as no IPv6; anything else that stops the probe counts as IPv6
    being there, so that a missing IPv6 table refuses."""
    family = getattr(socket, "AF_INET6", None)
    if family is None:
        return True
    try:
        socket.socket(family, socket.SOCK_STREAM).close()
    except OSError as exc:
        return exc.errno not in NO_FAMILY
    return True


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


# The formats the kernel prints /proc/net/tcp's fields in (net/ipv4/tcp_ipv4.c and net/ipv6/tcp_ipv6.c: get_openreq*,
# get_timewait*_sock, get_tcp*_sock).
DEC = "(?:0|[1-9][0-9]*)"  # %u, %lu: no sign, no leading zero
SDEC = "(?:0|-?[1-9][0-9]*)"  # %d
X8 = "[0-9A-F]{8}"  # %08X
LX8 = "(?:[0-9A-F]{8}|[1-9A-F][0-9A-F]{8,15})"  # %08lX: eight digits at least, and no leading zero past them
POINTER = r"(?:[0-9a-f]{16}|[0-9a-f]{8}|\(____ptrval____\)|\(ptrval\))"  # %pK: hashed or zeroed, or not hashable yet


# The C types the kernel prints /proc/net/tcp's fields from, by their largest and least values.
U8, U32, U64 = 2 ** 8 - 1, 2 ** 32 - 1, 2 ** 64 - 1
INT_MIN, INT_MAX, LONG_MAX = -(2 ** 31), 2 ** 31 - 1, 2 ** 63 - 1

Field = Callable[[str], bool]  # whether a field is in its printed format and within its kernel type


def _printed(pattern: str, within: Callable[[str], bool] = lambda field: True) -> Field:
    form = re.compile(pattern)
    return lambda field: form.fullmatch(field) is not None and within(field)


def _decimal(least: int, most: int) -> Field:
    """A decimal field (%u, %lu or %d) printed from a kernel type that runs from `least` to `most`."""
    return _printed(SDEC if least < 0 else DEC, lambda field: least <= int(field) <= most)


def _hex8(most: int) -> Field:
    """An %08X field printed from a kernel type no larger than `most`."""
    return _printed(X8, lambda field: int(field, 16) <= most)


def _timer(codes: frozenset[int]) -> Field:
    """%02X:%08lX: a timer code this kind of socket prints, and a delay (a clock_t the kernel never prints negative)."""
    def within(field: str) -> bool:
        code, _, delay = field.partition(":")
        return int(code, 16) in codes and int(delay, 16) <= LONG_MAX

    return _printed(f"[0-9A-F]{{2}}:{LX8}", within)


def _exactly(text: str) -> Field:
    """A field the kernel prints as a constant."""
    return lambda field: field == text


@dataclass(frozen=True)
class ProcRowKind:
    """A kind of row the kernel prints in /proc/net/tcp: each of its fields, and the states it is printed in."""

    fields: tuple[Field, ...]
    states: frozenset[int]


def _proc_row_kinds(width: int) -> dict[int, list[ProcRowKind]]:
    """The kinds of /proc/net/tcp row (`width` 8) or tcp6 row (32), by field count, each field as the kernel prints it
    (net/ipv4/tcp_ipv4.c and net/ipv6/tcp_ipv6.c): a full socket (get_tcp*_sock, 17 fields), a request socket
    (get_openreq*, 12) and a time-wait socket (get_timewait*_sock, 12)."""
    address = _printed(f"[0-9A-F]{{{width}}}:[0-9A-F]{{4}}")  # u32 words and a u16 port: their widths bound them
    # slot (an int count), local and remote address, state (checked against the kind)
    head = (_printed(f"{DEC}:", lambda field: int(field[:-1]) <= INT_MAX), address, address, _printed("[0-9A-F]{2}"))
    empty_queues, zero, pointer = _exactly("00000000:00000000"), _exactly("0"), _printed(POINTER)
    full = ProcRowKind((
        *head,
        _printed(f"{X8}:{X8}"),  # send and receive queues (u32, int): eight hex digits are their range
        _timer(frozenset({0, 1, 2, 4})),  # none, retransmit, keepalive, zero-window probe
        _hex8(U8),  # retransmits (u8)
        _decimal(0, U32),  # uid (uid_t)
        _decimal(0, U8),  # timeout: unanswered probes (u8)
        _decimal(0, U64),  # inode (unsigned long)
        _decimal(INT_MIN, INT_MAX),  # refcount, printed as an int
        pointer,
        _decimal(0, LONG_MAX),  # rto (clock_t)
        _decimal(0, LONG_MAX),  # ato (clock_t)
        _decimal(0, 511),  # (u8 quick << 1) | pingpong
        _decimal(0, U32),  # cwnd (u32)
        _decimal(INT_MIN, INT_MAX),  # ssthresh, printed as an int (-1 in initial slow start)
    ), frozenset(range(1, 12)))  # ESTABLISHED (1) to CLOSING (11)
    request = ProcRowKind((
        *head, empty_queues, _timer(frozenset({1})),
        _hex8(U8),  # timeouts so far (u8)
        _decimal(0, U32),  # the listener's uid (uid_t)
        zero, zero, zero, pointer,  # timeout, inode, refcount
    ), frozenset({3}))  # SYN_RECV
    time_wait = ProcRowKind((
        *head, empty_queues, _timer(frozenset({3})),
        _exactly("00000000"), zero, zero, zero,  # retransmits, uid, timeout, inode
        _decimal(INT_MIN, INT_MAX),  # refcount, printed as an int
        pointer,
    ), frozenset({5, 6}))  # FIN_WAIT2, TIME_WAIT
    return {17: [full], 12: [request, time_wait]}


PROC_ROW_KINDS = {width: _proc_row_kinds(width) for width in (8, 32)}


def _proc_row(fields: list[str], width: int) -> bool:
    """Whether a row of /proc/net/tcp (`width` 8) or tcp6 (32) is one the kernel prints: a kind of row with that many
    fields, in a state that kind is printed in, every field in its printed format and within the kernel type it is
    printed from, or the constant the kernel prints there."""
    if len(fields) < 4 or not re.fullmatch("[0-9A-F]{2}", fields[3]):
        return False
    state = int(fields[3], 16)
    return any(state in kind.states and all(valid(field) for valid, field in zip(kind.fields, fields, strict=True))
               for kind in PROC_ROW_KINDS[width].get(len(fields), []))


def proc_owners(local: Address, remote: Address, tables: Iterable[str] = PROC_TABLES) -> list[int]:
    """The uids of the live sockets at the other end of the connection `local` -> `remote`: those whose own address
    is `remote` and whose peer is `local`. A table that cannot be read, or whose header or any row is not in the
    kernel's printed form (`_proc_row`), raises, and the check refuses. A missing tcp6 reads as empty only on a machine
    without IPv6."""
    near, far = (_ip(local[0]), int(local[1])), (_ip(remote[0]), int(remote[1]))
    uids = []
    for table in tables:
        own, peer = _proc_hex(*far, table), _proc_hex(*near, table)
        if own is None or peer is None:
            continue  # an IPv6 connection has no row in the IPv4 table
        six = table.endswith("6")
        try:
            lines = Path(table).read_text(encoding="ascii").splitlines()
        except FileNotFoundError:
            if six and not ipv6_available():
                continue  # no IPv6 here, so no IPv6 or dual-stack socket to list
            raise
        if not lines or lines[0].split() != PROC_HEADERS[six]:
            raise ValueError(f"{table} does not start with the header the kernel writes")
        for line in lines[1:]:
            fields = line.split()
            if not _proc_row(fields, 32 if six else 8):
                raise ValueError(f"{table} has a row the Hub cannot parse")
            if fields[1] == own and fields[2] == peer and fields[3] in PROC_LIVE_STATES:
                uids.append(int(fields[7]))
    return uids


OVERFLOW_UID_FILE = "/proc/sys/kernel/overflowuid"
UID_MAP_FILE = "/proc/self/uid_map"


def _is_uid(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value < 2 ** 32


def overflow_uid() -> int | None:
    """The kernel's overflow uid, which it shows for an account it cannot map, or None when the setting cannot be read
    or parsed (the check then refuses)."""
    try:
        text = Path(OVERFLOW_UID_FILE).read_text(encoding="ascii").strip()
    except (OSError, ValueError):
        return None
    return int(text) if text.isdigit() and _is_uid(int(text)) else None


UID_MAP_MAX_ROWS = 340  # UID_GID_MAP_MAX_EXTENTS


def uid_map_rows() -> list[tuple[int, int, int]] | None:
    """This process's uid map (/proc/self/uid_map) as (first, outside, count) rows, or None when it cannot be read or
    is not a map the kernel writes (uid_m_show prints what map_write accepted): at most 340 rows of three unsigned
    32-bit numbers, each range non-empty and inside the 32-bit space, no two ranges overlapping inside or outside."""
    try:
        lines = Path(UID_MAP_FILE).read_text(encoding="ascii").splitlines()
    except (OSError, ValueError):
        return None
    if len(lines) > UID_MAP_MAX_ROWS:
        return None
    rows = []
    for line in lines:
        fields = line.split()
        if len(fields) != 3 or not all(re.fullmatch(DEC, field) for field in fields):
            return None
        first, outside, count = (int(field) for field in fields)
        if count < 1 or first + count > U32 or outside + count > U32:  # u32 each, and no range wrapping past one
            return None
        rows.append((first, outside, count))
    for side in (0, 1):  # the ranges inside the namespace, then outside it
        ranges = sorted((row[side], row[side] + row[2]) for row in rows)
        if any(end > start for (_, end), (start, _) in pairwise(ranges)):
            return None
    return rows


def uid_mapped(uid: int) -> bool | None:
    """Whether `uid` has a mapping in this process's user namespace, or None when its uid map cannot be read or is not
    one the kernel writes (the check then refuses)."""
    rows = uid_map_rows()
    return None if rows is None else any(first <= uid < first + count for first, _, count in rows)


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
TCP_STATES = range(1, 13)  # every MIB_TCP_STATE: CLOSED (1) to DELETE_TCB (12)
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
    rows, aligned as a row is. Each field is held to its Win32 type: the state (DWORD) to the MIB_TCP_STATE values
    (1 to 12), and the creation time (LARGE_INTEGER) to a FILETIME, 0 to 2**63 - 1, 0 where Windows gives none. An
    address (DWORD or 16 bytes), PID or scope id (DWORD) is valid at any value; only a port's low 16 bits are read, as
    its upper 16 may be uninitialized (MIB_TCPROW_OWNER_PID); the owning-module information is opaque. A table cut
    short, or a row outside its types, raises ValueError."""
    row_type, ip = (_Row4, ipaddress.IPv4Address) if version == 4 else (_Row6, ipaddress.IPv6Address)
    (count,) = struct.unpack_from("=I", data)
    if not count:
        return []
    align = ctypes.alignment(row_type)
    rows = (row_type * count).from_buffer_copy(data, -(-4 // align) * align)

    def end(address, port) -> tuple[str, int]:
        return str(ip(bytes(address))), int.from_bytes(bytes(port)[:2], "big")

    parsed = [TcpRow(row.state, end(row.local_addr, row.local_port), end(row.remote_addr, row.remote_port), row.pid,
                     row.created) for row in rows]
    for row in parsed:
        if row.state not in TCP_STATES:
            raise ValueError("the TCP table has a row in a state Windows does not define")
        if row.created < 0:
            raise ValueError("the TCP table has a row whose creation time is not a FILETIME")
    return parsed


def windows_tcp_table(version: int) -> bytes:
    """Windows' TCP table for one IP version (TCP_TABLE_OWNER_MODULE_ALL), as GetExtendedTcpTable writes it. Only a
    machine without IPv6 (no AF_INET6 sockets) reads an IPv6 table Windows does not support as empty."""
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
        if error == ERROR_NOT_SUPPORTED and version == 6 and not ipv6_available():
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


def _is_time(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0


def is_account(value: object) -> bool:
    """Whether `value` is a Windows account as DOMAIN\\user: a domain and a user around one backslash."""
    if not isinstance(value, str):
        return False
    domain, backslash, user = value.partition("\\")
    return bool(backslash) and bool(domain.strip()) and bool(user.strip()) and "\\" not in user


def filetime_seconds(filetime: int) -> float:
    """A FILETIME in seconds since the epoch, as psutil gives a process's start."""
    return (filetime - FILETIME_UNIX_EPOCH) / 10_000_000


def windows_owners(local: Address, remote: Address, rows: Callable[[], list[TcpRow]] = windows_rows) -> list[str]:
    """The accounts (DOMAIN\\user) of the processes at the other end of the connection `local` -> `remote`, from
    Windows' TCP table. The table names a socket's process by PID, and Windows reuses PIDs, so a socket counts as its
    process's only when that process also holds the listening socket on its port and started before every socket the
    check relies on was created: the socket itself and each such listener. Otherwise, or when the table gives any of
    them no creation time, this raises `Unattributed`. A process whose account cannot be read (another user's) or
    parsed counts as `UNREADABLE`. A start time that cannot be read or parsed, or an identity re-check that fails,
    raises `Unattributed`."""
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
        if not _is_time(started):
            raise Unattributed(f"the Hub cannot read when process {end.pid} started (it reads as {started!r})")
        for name, row in sockets:
            if started >= filetime_seconds(row.created):
                raise Unattributed(f"process {end.pid} started after {name} was created, so it is not the process "
                                   "that created that socket")
        try:
            account = process.username()
        except (psutil.Error, OSError):
            account = UNREADABLE
        if not is_account(account):
            account = UNREADABLE
        try:
            alive = process.is_running()
        except (psutil.Error, OSError) as exc:
            raise Unattributed(f"the Hub cannot tell whether process {end.pid} is still the process it read "
                               f"({type(exc).__name__})") from exc
        if alive is not True:
            raise Unattributed(f"process {end.pid} exited or changed while the Hub read it")
        accounts.append(account)
    return accounts


OWN_ACCOUNT_FIX = "so it cannot verify the daemon's listener. Set --daemon-owner to the daemon's account (DOMAIN\\user)"


def windows_account() -> str:
    """This Hub's own account, as DOMAIN\\user. Raises ValueError when it cannot be read or parsed: the check could
    not tell the daemon's account without it."""
    try:
        import psutil  # Windows-only dependency, as in tailnet.py
    except ImportError as exc:
        raise ValueError(f"this Hub cannot read its own account (no psutil), {OWN_ACCOUNT_FIX}") from exc
    try:
        account = psutil.Process().username()
    except (psutil.Error, OSError) as exc:
        raise ValueError(f"this Hub cannot read its own account ({type(exc).__name__}), {OWN_ACCOUNT_FIX}") from exc
    if not is_account(account):
        raise ValueError(f"this Hub's own account reads as {account!r}, not DOMAIN\\user, {OWN_ACCOUNT_FIX}")
    return account


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
    owner, platform, overflow = owner.strip(), platform or PLATFORM, overflow_uid()
    if owner == "nobody" or (overflow is not None and owner.isdigit() and int(owner) == overflow):
        shown = "unreadable here" if overflow is None else overflow
        raise ValueError(f"--daemon-owner cannot be nobody or the kernel's overflow uid ({shown}): the connection "
                         "table cannot single out that account. Name the account that runs the daemon")
    if platform == "linux":
        if owner and resolve_uid(owner) == overflow:
            raise ValueError(f"--daemon-owner {owner!r} is the kernel's overflow uid ({overflow}), which the "
                             "connection table cannot single out. Name the account that runs the daemon")
        return Owner(uid=resolve_uid(owner) if owner else os.geteuid())
    if platform == "win32":
        if owner and not is_account(owner):
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
                 overflow: Callable[[], int | None] = overflow_uid, mapped: Callable[[int], bool | None] = uid_mapped,
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
        if not (_address(local) and _address(remote)):
            return (f"the daemon URL {self.url} is not verified: the connection's addresses cannot be parsed, so the "
                    "Hub sent it nothing.")
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
        namespace, or an expected owner that is the kernel's overflow uid. It fails closed, too, on each of those
        inputs it cannot read or parse: the overflow uid, the Hub's own uid and its uid map. Only Linux has uids."""
        if self.platform != "linux":
            return ""
        overflow, me, expected = self.overflow(), self.me(), self.owner.uid
        own = self.mapped(me) if _is_uid(me) else None
        theirs = self.mapped(expected) if _is_uid(expected) else False
        if not _is_uid(overflow):
            reason = (f"the kernel's overflow uid cannot be read from {OVERFLOW_UID_FILE}, so the connection table "
                      "cannot tell an unmapped account from a real one. Make that file readable to the Hub")
        elif not _is_uid(me):
            reason = (f"this Hub cannot read its own uid (it reads as {me!r}), so it cannot tell which accounts the "
                      "connection table can show")
        elif own is None or theirs is None:
            reason = (f"this Hub's uid map ({UID_MAP_FILE}) cannot be read or parsed, so it cannot tell which "
                      "accounts the connection table can show. Make that file readable to the Hub")
        elif not own:
            reason = (f"this Hub's own uid is unmapped in its user namespace (it reads as {me}; the kernel's "
                      f"overflow uid is {overflow}), so the connection table cannot single out an account. Run the "
                      "Hub with its uid mapped, or outside the user namespace")
        elif not theirs:
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
