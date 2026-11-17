"""Shared redaction core and coercion helpers: every page projects daemon answers through these."""

from __future__ import annotations

import json
import math
import re
import unicodedata
from typing import Any

from ..claim import RELEASE_COMMAND
from ..guard import KEY_PREFIX_SHAPE, TOKEN_SHAPE, leaks

MESSAGES = {
    "unclaimed": "This Hub has not claimed the daemon yet.",
    "pending": "Waiting for the owner to approve this claim on the daemon host.",
    "claimed": "This Hub is the daemon's Hub.",
    "persisting": "The daemon issued this Hub's key, but private state storage could not save it. "
                  "Saving will be retried. Keep the Hub running and repair storage on the host; "
                  "if the Hub exits before saving, release it on the daemon host and claim again.",
    "refused": "This daemon already has a Hub. Only the owner can release it, on the daemon host.",
    "expired": "The claim expired before it was approved. Start a new claim.",
    "denied": "The owner denied this claim on the daemon host. Start a new claim to try again.",
    "released": "The daemon no longer accepts this Hub's key: the Hub was released on the daemon host. "
                "Start a new claim to link it again.",
    "rate_limited": "The daemon has too many pairing requests waiting. Try again in a few minutes.",
    "failed": "The claim did not complete. Check that the daemon is running, then start a new claim.",
    "lost": "The daemon issued this Hub's key, but the reply never arrived. On the daemon host run "
            f"{RELEASE_COMMAND}, then start a new claim here.",
}


REDACTED = "[withheld: token-shaped]"
# How display_text hides a key's display prefix (`ho-LW5TqUX`) or anything shaped like one (#189).
# FIELD, the default and the strongest: one anywhere in the value, a full token too, replaces the entire field, as does
# one only the text with or without control and format characters shows (`h\u200bo-x`), unless the guard would withhold
# it anyway (`guarded`: the guard's secrets, from a caller that must not return them as `secrets` would).
FIELD = "field"
WORD = "word"  # Daemon diagnostics, which can echo a prefix: each one is redacted in place, a full token left in place.
GUARD = "guard"  # Text whose ordinary words (`hp-instruct-7b`) must stay readable (#84): only the guard checks it.
REDACT_MODES = (FIELD, WORD, GUARD)


def _prefix(match: re.Match) -> str:
    # A full token is left in place: the response guard must withhold the entire view, not hide a daemon leak.
    return match[0] if TOKEN_SHAPE.match(match[0]) else REDACTED


def display_text(value: Any, limit: int = 200, *, secrets: tuple[str, ...] = (), strip_controls: bool = False,
                 redact: str = FIELD, guarded: tuple[str, ...] = ()) -> str:
    """Daemon or publisher text as the browser may see it, cut to `limit`: the one place display text is redacted. An
    exact known secret, even one split by a format character, is returned whole, so the guard withholds the view.
    `strip_controls` drops control and format characters first, so publisher text cannot reorder surrounding labels or
    carry invisible controls. `redact` is FIELD, WORD or GUARD, above."""
    if redact not in REDACT_MODES:
        raise ValueError(f"unknown redaction mode {redact!r}")
    if not isinstance(value, str):
        return ""
    stripped = "".join(char for char in value if unicodedata.category(char) not in {"Cc", "Cf"}) if (
        strip_controls or redact == FIELD or secrets) else value
    shown, other = (stripped, value) if strip_controls else (value, stripped)
    for secret in secrets:
        if secret and (secret in shown or secret in other):
            return secret  # The response guard must withhold the entire view.
    if redact == WORD:
        # Cut first: a token cut short at the limit is then redacted, not passed under the guard's full-length shape.
        return KEY_PREFIX_SHAPE.sub(_prefix, shown[:limit])
    if redact == FIELD and KEY_PREFIX_SHAPE.search(shown):
        return REDACTED
    token = next((token for token in TOKEN_SHAPE.finditer(shown) if token.end() > limit), None)
    cut = shown[:token.end()] if token and token.start() < limit else shown[:limit]  # Keep a token whole for the guard.
    if redact == FIELD and KEY_PREFIX_SHAPE.search(other) and not leaks(json.dumps(cut).encode(), secrets + guarded):
        return REDACTED
    return cut


def _bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _num(value: Any) -> float | None:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


def _int(value: Any) -> int | None:
    return value if type(value) is int else None


def _dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list:
    return value if isinstance(value, list) else []


def _names(value: Any) -> list[str]:
    return [display_text(v, 64, redact=GUARD) for v in _list(value)[:32] if isinstance(v, str)]


def _finite(value: Any) -> float | None:
    if type(value) is int and value.bit_length() > 64:
        return None
    number = _num(value)
    return number if number is not None and math.isfinite(number) else None


ID_SEGMENT = re.compile(r"[A-Za-z0-9_-]{1,80}")


def _app_id(value: Any) -> str:
    return value if isinstance(value, str) and ID_SEGMENT.fullmatch(value) else ""
