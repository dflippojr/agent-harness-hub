"""The `/api/session` router (sign-in, session state, sign-out) and the Hub's claim on its daemon (`/api/claim`). The
session check every `/api/` request passes is `owners.signed_in`; the tailnet sign-in asks the daemon for the owner's
logins (`allowed_logins`)."""

from __future__ import annotations

import asyncio
import logging

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from .. import __version__
from ..auth import SESSION_TTL_SECONDS
from ..claim import ClaimError
from ..guard import TOKEN_SHAPE
from ..owners import SESSION_HEADER, UNREACHABLE, read_logins, signed_in
from ..tailnet import login
from ..views import REDACTED, claim_view
from .common import HubContext, HubRoute, error, hub, json_body, unreachable

log = logging.getLogger("harness_hub")

SIGNIN_COMMAND = "python -m harness_hub signin-link"
MAX_SIGNIN_BODY = 1024


async def allowed_logins(ctx: HubContext, token: str) -> list[str] | JSONResponse:
    answer = await read_logins(ctx, token, failures=False)
    if answer == UNREACHABLE:
        return unreachable(f"the daemon at {ctx.settings.daemon_url} did not answer; use a sign-in link",
                           signin_command=SIGNIN_COMMAND)
    if isinstance(answer, int):
        return error(502, f"the owner API answered HTTP {answer}; use a sign-in link",
                     signin_command=SIGNIN_COMMAND)
    if answer is None:
        return error(502, "the daemon does not list the owner's tailnet logins (it needs Owner API 1.26 or "
                     "later); use a sign-in link", signin_command=SIGNIN_COMMAND)
    return answer


async def tailnet_sign_in(ctx: HubContext, request: Request) -> JSONResponse | None:
    """Refuse a tailnet sign-in unless the daemon lists this request's trusted login; None means accept. An
    unclaimed Hub, an open-owner daemon and an unlisted login get the same answer; the log tells them apart."""
    caller = login(request.headers)
    if not caller:
        return error(401, "this request carries no tailnet identity from tailscaled; use a sign-in link",
                     signin_command=SIGNIN_COMMAND)
    if not ctx.sessions.tailnet_attempt(caller):
        return error(429, "too many tailnet sign-in attempts; wait a minute")
    refused = error(403, "this tailnet login cannot sign in to this Hub; use a sign-in link",
                    signin_command=SIGNIN_COMMAND)
    # A raw TCP forward lets tailscaled relay a client's own identity headers (tailnet.py). Only where the peer
    # was verified: with unverified headers trusted, any local process can sign in anyway.
    server = request.scope.get("server")
    if ctx.peer_check.platform_ok() and await asyncio.to_thread(ctx.peer_check.raw_forward_to,
                                                                server[1] if server else None):
        log.warning("refused tailnet sign-in: this node's tailscale serve config has a raw TCP forward to the "
                    "Hub's port, or could not be read")
        return refused
    key = ctx.claims.key()
    if not key:
        log.warning("refused tailnet sign-in: this Hub has not claimed its daemon")
        return refused
    allowed = await allowed_logins(ctx, key["token"])
    if isinstance(allowed, JSONResponse):
        return allowed
    if not allowed:
        # Open-owner legacy mode: the daemon names no owner, so no tailnet login proves ownership.
        log.warning("refused tailnet sign-in: the daemon has no owner allowlist (allowed_logins)")
        return refused
    if caller not in allowed:
        log.warning("refused tailnet sign-in for a login the daemon does not list as the owner's")
        return refused
    return None


async def get_session(request: Request):
    # `tailnet_login` is the caller's own identity, present only when tailscaled added it (tailnet.py). Whether
    # it is the owner's is decided at sign-in, against the daemon's list. A token-shaped login is redacted here, so
    # the response guard never withholds this route: the page reads it first on every load.
    caller = TOKEN_SHAPE.sub(REDACTED, login(request.headers))
    return {"signed_in": await signed_in(hub(request), request), "signin_command": SIGNIN_COMMAND,
            "tailnet_login": caller or None, "version": __version__}


async def sign_in(request: Request):
    ctx = hub(request)
    sessions, settings = ctx.sessions, ctx.settings
    length = request.headers.get("content-length")
    if length is not None:
        try:
            if int(length) < 0:
                return error(400, "invalid Content-Length")
            if int(length) > MAX_SIGNIN_BODY:
                return error(413, "sign-in body is too large")
        except ValueError:
            return error(400, "invalid Content-Length")
    data = bytearray()
    async for chunk in request.stream():
        if len(data) + len(chunk) > MAX_SIGNIN_BODY:
            return error(413, "sign-in body is too large")
        data.extend(chunk)
    try:
        body = json_body(data)
    except ValueError:
        body = None
    if isinstance(body, dict) and body.get("method") == "tailnet":
        refusal = await tailnet_sign_in(ctx, request)
        if refusal is not None:
            return refusal
        # The read tailnet_sign_in took, with no await between, so the grace counts from the daemon's yes.
        credentials = sessions.create(request.cookies.get(settings.cookie_name), login(request.headers),
                                      ctx.owner_logins.at)
    else:
        code = body.get("code") if isinstance(body, dict) else None
        was_throttled = sessions.throttled()
        try:
            # Off the event loop: the code file's cross-process lock can be held by a stalled signin-link (#134).
            credentials = await asyncio.to_thread(sessions.redeem_code, code,
                                                  request.cookies.get(settings.cookie_name))
        except OSError as exc:  # SigninBusy included; the code is not spent
            log.warning("sign-in could not update the sign-in codes: %s", exc)
            return error(503, "sign-in is busy; try again in a moment")
        if credentials is None:
            if was_throttled:
                return error(429, "too many sign-in attempts; wait a minute")
            return error(401, "that sign-in link is not valid; links work once, for ten minutes",
                         signin_command=SIGNIN_COMMAND)
    session, binding = credentials
    response = Response(status_code=204, headers={SESSION_HEADER: binding})
    if session == request.cookies.get(settings.cookie_name):
        return response  # another tab of the signed-in session: the cookie and its expiry stay as they were
    response.set_cookie(settings.cookie_name, session, max_age=SESSION_TTL_SECONDS,
                        httponly=True, samesite="strict",
                        secure=settings.secure_cookies, path="/")
    return response


async def sign_out(request: Request):
    ctx = hub(request)
    settings = ctx.settings
    ctx.sessions.end(request.cookies.get(settings.cookie_name))
    response = Response(status_code=204)
    response.delete_cookie(settings.cookie_name, path="/", secure=settings.secure_cookies,
                           httponly=True, samesite="strict")
    return response


async def get_claim(request: Request):
    ctx = hub(request)
    return claim_view(ctx.claims)


async def start_claim(request: Request):
    ctx = hub(request)
    try:
        await ctx.claims.start()
    except ClaimError as exc:
        status = exc.status or {"refused": 409, "rate_limited": 429, "claimed": 409,
                  "persisting": 503, "pending": 503}.get(exc.state, 502)
        return JSONResponse({"detail": str(exc), "claim": claim_view(ctx.claims)}, status_code=status)
    return JSONResponse(claim_view(ctx.claims), status_code=201)


async def forget_claim(request: Request):
    ctx = hub(request)
    try:
        await ctx.claims.forget()
    except ClaimError as exc:
        return error(503, str(exc), claim=claim_view(ctx.claims))
    return claim_view(ctx.claims)


def routes() -> list[Route]:
    """The `/api/session` routes, in this order, for the app's own route table: flat entries, not a nested router or
    `Mount`, which would change the route table (#190)."""
    return [
        HubRoute("/api/session", get_session, ["GET"]),
        HubRoute("/api/session", sign_in, ["POST"]),
        HubRoute("/api/session", sign_out, ["DELETE"]),
    ]


def claim_routes() -> list[Route]:
    """The `/api/claim` routes, in this order, where they were in the route table: after Settings (#190)."""
    return [
        HubRoute("/api/claim", get_claim, ["GET"]),
        HubRoute("/api/claim", start_claim, ["POST"]),
        HubRoute("/api/claim", forget_claim, ["DELETE"]),
    ]
