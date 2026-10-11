"""What the browser may see: every daemon answer is copied field by field into these shapes, never passed through."""

from __future__ import annotations

import time
from typing import Any

from . import __version__
from .claim import CLAIMED, PENDING, RELEASE_COMMAND, ClaimManager

MESSAGES = {
    "unclaimed": "This Hub has not claimed the daemon yet.",
    "pending": "Waiting for the owner to approve this claim on the daemon host.",
    "claimed": "This Hub is the daemon's Hub.",
    "refused": "This daemon already has a Hub. Only the owner can release it, on the daemon host.",
    "expired": "The claim expired before it was approved. Start a new claim.",
    "denied": "The owner denied this claim on the daemon host. Start a new claim to try again.",
    "released": "The daemon no longer accepts this Hub's key: the Hub was released on the daemon host. "
                "Start a new claim to link it again.",
    "rate_limited": "The daemon has too many pairing requests waiting. Try again in a few minutes.",
    "failed": "The claim did not complete. Check that the daemon is running, then start a new claim.",
}


def _str(value: Any, limit: int = 200) -> str:
    return value[:limit] if isinstance(value, str) else ""


def _bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _num(value: Any) -> float | None:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


def _flags(value: Any) -> dict[str, bool]:
    if not isinstance(value, dict):
        return {}
    return {_str(k, 64): v for k, v in list(value.items())[:64] if isinstance(k, str) and isinstance(v, bool)}


def _names(value: Any) -> list[str]:
    return [_str(v, 64) for v in value[:32] if isinstance(v, str)] if isinstance(value, list) else []


def _protocols(value: Any) -> dict[str, dict[str, int]]:
    if not isinstance(value, dict):
        return {}
    out = {}
    for name, span in list(value.items())[:8]:
        if isinstance(name, str) and isinstance(span, dict):
            out[_str(name, 32)] = {k: span[k] for k in ("min", "max") if isinstance(span.get(k), int)}
    return out


def claim_view(claims: ClaimManager) -> dict:
    state = claims.state()
    view: dict[str, Any] = {"state": state, "message": MESSAGES[state], "release_command": RELEASE_COMMAND}
    if state == PENDING:
        claim = claims.pending() or {}
        view.update(request_id=claim["request_id"], match_code=claim["match_code"],
                    expires_at=claim["expires_at"], approve_command=claims.approve_command(claim))
    elif state == CLAIMED:
        key = claims.key() or {}
        view.update(key_id=_str(key.get("key_id"), 64), claimed_at=_num(key.get("claimed_at")))
    return view


def health_view(body: Any) -> dict:
    body = body if isinstance(body, dict) else {}
    caps = body.get("capabilities") if isinstance(body.get("capabilities"), dict) else {}
    config = body.get("config") if isinstance(body.get("config"), dict) else {}
    return {
        "daemon": {
            "release": _str(body.get("release"), 64),
            "build_id": _str(body.get("build_id"), 64),
            "profile": _str(body.get("profile") or caps.get("profile"), 32),
            "protocols": _protocols(body.get("protocols")),
        },
        "health": {
            "ok": body.get("ok") is True,
            "config_revision": config.get("revision") if isinstance(config.get("revision"), int) else None,
            "config_confirmed": _bool(config.get("confirmed")),
            "supervised_restart": _bool(config.get("supervised_restart")),
            "recovery": bool(config.get("recovery")),
        },
        "capabilities": {
            "profile": _str(caps.get("profile"), 32),
            "required": _flags(caps.get("required")),
            "modules": _flags(caps.get("modules")),
            "hosted_backends": _names(caps.get("hosted_backends")),
            "config_registry": _bool(caps.get("config_registry")),
        },
    }


def ownership_view(discovery: Any, hub_claim: Any, key_id: str) -> dict:
    """Whether the daemon has a Hub, and whether it is this one. Only the record's display fields are kept."""
    discovery = discovery if isinstance(discovery, dict) else {}
    hub_claim = hub_claim if isinstance(hub_claim, dict) else {}
    record = hub_claim.get("hub") if isinstance(hub_claim.get("hub"), dict) else None
    shown = None
    if record:
        shown = {"name": _str(record.get("name"), 120), "kind": _str(record.get("kind"), 16),
                 "key_id": _str(record.get("key_id"), 64), "claimed_at": _num(record.get("claimed_at"))}
    return {
        "api_version": _str(discovery.get("api_version"), 16),
        "daemon_has_hub": _bool(hub_claim.get("claimed")),
        "this_hub": bool(record and key_id and record.get("key_id") == key_id),
        "record": shown,
    }


def overview(claims: ClaimManager, daemon_url: str, health: dict | None, ownership: dict | None,
             error: str = "") -> dict:
    return {
        "hub": {"version": __version__, "daemon_url": daemon_url},
        "claim": claim_view(claims),
        "reachable": health is not None,
        "error": error,
        **(health or {"daemon": None, "health": None, "capabilities": None}),
        "ownership": ownership,
        "fetched_at": time.time(),
    }
