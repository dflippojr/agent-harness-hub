"""Run the Hub: `python -m harness_hub [serve options]`, or `python -m harness_hub signin-link`.

The daemon installer (agent-harness #547) starts the Hub as
`python -m harness_hub --daemon-url http://127.0.0.1:<port> --claim-file <dir>/claim.json --state-dir <dir>`.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

from . import __version__
from .auth import create_signin_code
from .config import (
    DEFAULT_DAEMON_URL,
    DEFAULT_HOST,
    DEFAULT_PORT,
    DEFAULT_STATE_DIR,
    Settings,
    is_loopback,
    normalize_daemon_url,
    normalize_public_url,
)
from .state import SERVER_FILE, SigninBusy, read_json
from .status import build_commit


def serve_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="harness_hub", description="Hub: the admin console for an Agent Harness "
                                     "daemon. Subcommand: signin-link (print a one-time sign-in link).")
    parser.add_argument("--version", action="version", version=f"harness_hub {__version__}")
    parser.add_argument("--daemon-url", default=DEFAULT_DAEMON_URL, help="the daemon this Hub administers")
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR,
                        help="owner-only directory for the Hub key and claim state")
    parser.add_argument("--host", default=DEFAULT_HOST, help="listen address (default: loopback only)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--claim-file", type=Path,
                        help="start a claim at once and publish its id and match code here (daemon installer)")
    parser.add_argument("--public-url", default="",
                        help="the https URL browsers use, e.g. a `tailscale serve` address; it is also allowed as Host")
    parser.add_argument("--allowed-host", action="append", default=[], metavar="HOST[:PORT]",
                        help="another exact Host header to answer (repeatable)")
    parser.add_argument("--name", default="Hub", help="the name the daemon records for this Hub")
    parser.add_argument("--status-interval", type=float, default=0.0, metavar="SECONDS",
                        help="every SECONDS, poll the daemon's /health and rewrite status.json in the state directory "
                             "(the production-signals sweep reads it); 0, the default, writes none")
    parser.add_argument("--trust-unverified-identity-headers", action="store_true",
                        help="UNSAFE: outside Windows, trust Tailscale-* identity headers from any local process "
                             "(like the daemon's listen.trust_unverified_identity_headers); ignored on Windows, "
                             "where the Hub checks that tailscaled sent them")
    return parser


def signin_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="harness_hub signin-link",
                                     description="Print a one-time link that signs a browser in to this Hub. Run it "
                                     "on the daemon host as the Hub's account; the link works once, for ten minutes.")
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    parser.add_argument("--url", default="", help="the Hub's URL (default: the running Hub's own URL)")
    return parser


def resolve_state_dir(path: Path) -> Path:
    """The state directory both commands use: a `~` can arrive unexpanded (`--state-dir=~/...` in bash, a launcher)."""
    return path.expanduser().resolve()


def signin_link(argv: list[str]) -> int:
    parser = signin_parser()
    args = parser.parse_args(argv)
    state_dir = resolve_state_dir(args.state_dir)
    hub_url = (read_json(state_dir / SERVER_FILE, {}) or {}).get("url") or ""
    url = args.url or hub_url or f"http://127.0.0.1:{DEFAULT_PORT}"
    try:
        url = normalize_public_url(url)
    except ValueError as exc:
        parser.error(str(exc))
    if _loopback_link_on_https_hub(url, hub_url):
        # The Hub only accepts writes from its https origin and sets a Secure cookie, so this link could not sign in.
        print(f"signin-link: this Hub runs at {hub_url.rstrip('/')}, so a sign-in link to {url} cannot work: the Hub "
              "accepts sign-in only from its public https origin. Omit --url to use that address; if it can't be "
              "reached (Tailscale is down), restart the Hub without --public-url and run signin-link again.",
              file=sys.stderr)
        return 1
    try:
        code = create_signin_code(state_dir)
    except SigninBusy:
        print("signin-link: another process is updating the sign-in codes; try again in a moment", file=sys.stderr)
        return 1
    print(f"{url.rstrip('/')}/#signin={code}")
    return 0


def _loopback_link_on_https_hub(url: str, hub_url: str) -> bool:
    link = urlsplit(url)
    return (urlsplit(hub_url).scheme == "https" and link.scheme == "http"
            and is_loopback(link.hostname or ""))


def serve(argv: list[str]) -> int:
    parser = serve_parser()
    args = parser.parse_args(argv)
    try:
        settings = Settings(
            daemon_url=normalize_daemon_url(args.daemon_url),
            state_dir=resolve_state_dir(args.state_dir),
            host=args.host,
            port=args.port,
            claim_file=args.claim_file.expanduser().resolve() if args.claim_file else None,
            public_url=normalize_public_url(args.public_url) if args.public_url else "",
            claim_name=args.name.strip() or "Hub",
            extra_hosts=tuple(h.strip().lower() for h in args.allowed_host if h.strip()),
            trust_unverified_identity_headers=args.trust_unverified_identity_headers,
            status_seconds=args.status_interval,
            build=build_commit(os.environ.get("HUB_BUILD_COMMIT")),
        )
    except ValueError as exc:
        parser.error(str(exc))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # one line per claim poll otherwise

    import uvicorn

    from .server import create_app

    # No proxy headers: a forwarded header must never change who the Hub thinks is calling.
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port, log_level="info",
                proxy_headers=False, forwarded_allow_ips="", server_header=False)
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["signin-link"]:
        return signin_link(argv[1:])
    if argv[:1] == ["serve"]:
        argv = argv[1:]
    return serve(argv)


if __name__ == "__main__":
    raise SystemExit(main())
