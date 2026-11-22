"""The owner's sign-in check (#255): whether a request's session is live, and the daemon's allowed-logins answer that a
tailnet binding depends on. The server's middleware calls `signed_in` for every `/api/` request."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from starlette.requests import Request

from .daemon import DaemonUnreachable

if TYPE_CHECKING:
    from .routes.common import HubContext

log = logging.getLogger("harness_hub")
SESSION_HEADER = "x-hub-session"
UNREACHABLE = 0  # read_logins' answer when the daemon did not answer at all


@dataclass(frozen=True)
class OwnerLogins:
    """The daemon's last allowed_logins answer, kept briefly, on the sessions' monotonic clock: `at` is when the read
    began, `until` when the answer stops being reused, `answer` the list, an HTTP status, UNREACHABLE or None."""

    at: float
    until: float
    answer: list[str] | int | None


async def read_logins(ctx: HubContext, token: str, *, failures: bool) -> list[str] | int | None:
    """The daemon's allowed_logins: the list, the HTTP status of a non-2xx reply, UNREACHABLE, or None for a 2xx
    reply without a list of strings. Every answer is kept for `Tuning.allowed_logins_seconds`. A sign-in reuses only a
    list; with `failures`, the session check reuses any answer, so a down daemon is asked once per window, not
    once per request."""
    async with ctx.owner_logins_read:
        kept = ctx.owner_logins
        if kept is not None and ctx.sessions.monotonic() < kept.until and (
                failures or isinstance(kept.answer, list)):
            return kept.answer
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
        ctx.owner_logins = OwnerLogins(at=at, until=at + ctx.tuning.allowed_logins_seconds, answer=answer)
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
    restarts, keeps the binding (#169) only for `Tuning.outage_grace_seconds` after a read last listed the login,
    so a tab cannot outlast its removal by timing its checks into outages, ones it asked for included (#177); after
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
        return sessions.confirm(session, binding, ctx.owner_logins.at)
    if listed is None:
        found = sessions.binding(session, binding)  # the time since the last listing, after the read's wait
        return found is not None and found[1] < ctx.tuning.outage_grace_seconds
    sessions.revoke(session, binding)
    log.warning("ended a tailnet session: the daemon no longer lists its login as the owner's")
    return False
