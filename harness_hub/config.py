"""Hub server settings, from the command line (the daemon installer's launch contract)."""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import SplitResult, urlsplit

DEFAULT_DAEMON_URL = "http://127.0.0.1:8100"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8120
DEFAULT_STATE_DIR = Path.home() / ".agent-harness-hub"


def normalize_daemon_url(value: str) -> str:
    """An http(s) origin with no path, query, fragment or credentials."""
    parts = urlsplit(value.strip())
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("daemon URL must be http:// or https:// with a host")
    if parts.username or parts.password or parts.query or parts.fragment or parts.path not in ("", "/"):
        raise ValueError("daemon URL must be an origin only (no credentials, path, query or fragment)")
    if parts.scheme == "http" and not _is_loopback(parts.hostname):
        raise ValueError("daemon URL must be https unless it is a loopback address")
    _check_port(parts)
    return f"{parts.scheme}://{parts.netloc}"


def _check_port(parts: SplitResult) -> None:
    """Reject a port urlsplit cannot read (`:abc`, `:99999`) and port 0, which nothing listens on."""
    try:
        port = parts.port
    except ValueError:
        raise ValueError("URL port must be a number from 1 to 65535") from None
    if port == 0:
        raise ValueError("URL port must be a number from 1 to 65535")


def normalize_public_url(value: str) -> str:
    """The URL the owner's browser uses for the Hub, such as a `tailscale serve` HTTPS address."""
    url = normalize_daemon_url(value)
    if urlsplit(url).scheme != "https" and not _is_loopback(urlsplit(url).hostname or ""):
        raise ValueError("public URL must be https unless it is a loopback address")
    return url


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


@dataclass(frozen=True)
class Settings:
    daemon_url: str = DEFAULT_DAEMON_URL
    state_dir: Path = DEFAULT_STATE_DIR
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    claim_file: Path | None = None
    public_url: str = ""
    claim_name: str = "Hub"
    poll_seconds: float = 2.0
    extra_hosts: tuple[str, ...] = field(default=())
    trust_unverified_identity_headers: bool = False  # off Windows only: any local process may then send a login
    status_seconds: float = 0.0  # seconds between status.json heartbeats (status.py); 0 writes none
    build: str = "unknown"  # the deployed commit, for status.json

    def __post_init__(self) -> None:
        object.__setattr__(self, "daemon_url", normalize_daemon_url(self.daemon_url))
        if self.public_url:
            object.__setattr__(self, "public_url", normalize_public_url(self.public_url))
        if not _is_loopback(self.host) and not self.secure_cookies:
            raise ValueError("non-loopback listen addresses require an https --public-url and an HTTPS proxy")
        normalized = []
        for host in self.extra_hosts:
            origin = host if "://" in host else f"{'https' if self.secure_cookies else 'http'}://{host}"
            parts = urlsplit(normalize_public_url(origin))
            scheme = "https" if self.secure_cookies else "http"
            if parts.scheme != scheme:
                raise ValueError("allowed host must use the same scheme as the Hub public URL")
            normalized.append(parts.netloc.lower())
        object.__setattr__(self, "extra_hosts", tuple(normalized))

    def allowed_origins(self) -> frozenset[str]:
        scheme = "https" if self.secure_cookies else "http"
        return frozenset(f"{scheme}://{host}" for host in self.allowed_hosts())

    @property
    def cookie_name(self) -> str:
        return "__Host-hub_session" if self.secure_cookies else "hub_session"

    @property
    def url(self) -> str:
        """Where a browser on this machine reaches the Hub."""
        if self.public_url:
            return self.public_url
        host = self.host if self.host not in ("0.0.0.0", "::", "") else "127.0.0.1"  # noqa: S104
        if ":" in host:
            host = f"[{host}]"
        return f"http://{host}:{self.port}"

    @property
    def secure_cookies(self) -> bool:
        return self.url.startswith("https://")

    def allowed_hosts(self) -> frozenset[str]:
        """Exact Host header values this Hub answers; anything else is refused (DNS rebinding)."""
        hosts = {f"127.0.0.1:{self.port}", f"localhost:{self.port}", f"[::1]:{self.port}"}
        if self.host not in ("0.0.0.0", "::", "", "127.0.0.1", "localhost", "::1"):  # noqa: S104
            hosts.add(f"[{self.host}]:{self.port}" if ":" in self.host else f"{self.host}:{self.port}")
        if self.public_url:
            netloc = urlsplit(self.public_url).netloc
            hosts.add(netloc)
            default_port = ":443" if self.public_url.startswith("https://") else ":80"
            if netloc.endswith(default_port):
                hosts.add(netloc[: -len(default_port)])
        hosts.update(self.extra_hosts)
        return frozenset(h.lower() for h in hosts)
