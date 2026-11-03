"""The `/api/session` router (sign-in, session state, sign-out) and the Hub's claim on its daemon (`/api/claim`), the
session check every `/api/` request passes (`server.require_owner`), and the tailnet sign-in's owner check: a tailnet
binding lasts only while the daemon lists its login as the owner's (`allowed_logins`)."""

from __future__ import annotations

import asyncio
import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from .. import __version__
from ..auth import SESSION_TTL_SECONDS
from ..claim import ClaimError
from ..daemon import DaemonUnreachable
from ..guard import TOKEN_SHAPE
from ..tailnet import login
from ..views import REDACTED, claim_view
from .common import HubContext, error, hub, json_body, tuning, unreachable

log = logging.getLogger("harness_hub")

SIGNIN_COMMAND = "python -m harness_hub signin-link"
SESSION_HEADER = "x-hub-session"
UNREACHABLE = 0  # read_logins' answer when the daemon did not answer at all
MAX_SIGNIN_BODY = 1024


async def read_logins(ctx: HubContext, token: str, *, failures: bool) -> list[str] | int | None:
    """The daemon's allowed_logins: the list, the HTTP status of a non-2xx reply, UNREACHABLE, or None for a 2xx
    reply without a list of strings. Every answer is kept for ALLOWED_LOGINS_SECONDS. A sign-in reuses only a
    list; with `failures`, the session check reuses any answer, so a down daemon is asked once per window, not
    once per request."""
    owner_logins = ctx.owner_logins
    async with ctx.owner_logins_read:
        if owner_logins and ctx.sessions.monotonic() < owner_logins["until"] and (
                failures or isinstance(owner_logins["answer"], list)):
            return owner_logins["answer"]
        try:
            reply = await ctx.daemon.admin(token)
        except DaemonUnreachable:
            answer: list[str] | int | None = UNREACHABLE
        else:
            allowed = reply.body.get("allowed_logins") if isinstance(reply.body, dict) else None
            if not reply.ok:
                answer = reply.status
            elif isinstance(allowed, list) and all(isinstance(item, str) for item in allowed):
                answer = list(allowed)
            else:
                answer = None
        at = ctx.sessions.monotonic()
        owner_logins.update(at=at, until=at + tuning().ALLOWED_LOGINS_SECONDS, answer=answer)
        return answer


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


async def still_owner(ctx: HubContext, owner: str) -> bool | None:
    """Whether the daemon still lists a tailnet session's login, or None when it did not say: it did not answer,
    or answered 5xx while it restarts. A definite no is an unlisted login, an empty list, a refused Hub key, a
    reply without a list or no Hub key at all."""
    key = ctx.claims.key()
    if not key:
        return False
    answer = await read_logins(ctx, key["token"], failures=True)
    if isinstance(answer, int):
        return None if answer == UNREACHABLE or answer >= 500 else False
    return answer is not None and owner in answer


async def signed_in(ctx: HubContext, request: Request) -> bool:
    """Whether the request's cookie and binding name a live session. A binding a tailnet sign-in opened also
    needs its login still listed by the daemon, the login stored at sign-in, never this request's headers; one
    that is not ends for good, and the session's other tabs keep working. A daemon that does not say, while it
    restarts, keeps the binding (#169) only for OUTAGE_GRACE_SECONDS after a read last listed the login, so a tab
    cannot outlast its removal by timing its checks into outages, ones it asked for included (#177); after
    that the binding answers 401 until a check finds the login listed again. Every answer is taken against the
    binding as it is once the read is back: one that expired or was revoked during the read authorizes nothing."""
    sessions = ctx.sessions
    session, binding = request.cookies.get(ctx.settings.cookie_name), request.headers.get(SESSION_HEADER)
    found = sessions.binding(session, binding)
    if found is None:
        return False
    owner, _ = found
    if owner is None:
        return True
    listed = await still_owner(ctx, owner)
    if listed:
        # The read still_owner took, with no await between; False if the binding ended while it ran (#177).
        return sessions.confirm(session, binding, ctx.owner_logins["at"])
    if listed is None:
        found = sessions.binding(session, binding)  # the time since the last listing, after the read's wait
        return found is not None and found[1] < tuning().OUTAGE_GRACE_SECONDS
    sessions.revoke(session, binding)
    log.warning("ended a tailnet session: the daemon no longer lists its login as the owner's")
    return False


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
                                      ctx.owner_logins["at"])
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



def add_routes(app: FastAPI) -> None:
    """Add the `/api/session` routes to the app itself, in this order. Not `include_router`: FastAPI 0.162 puts one
    nested entry in `app.routes` for an included router, which would change the route table (#190)."""
    app.add_api_route("/api/session", get_session, methods=["GET"])
    app.add_api_route("/api/session", sign_in, methods=["POST"])
    app.add_api_route("/api/session", sign_out, methods=["DELETE"])


def add_claim_routes(app: FastAPI) -> None:
    """Add the `/api/claim` routes, in this order, where they were in the route table: after Settings (#190)."""
    app.add_api_route("/api/claim", get_claim, methods=["GET"])
    app.add_api_route("/api/claim", start_claim, methods=["POST"])
    app.add_api_route("/api/claim", forget_claim, methods=["DELETE"])
