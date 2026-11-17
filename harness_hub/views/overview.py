"""Browser projection for the Overview page and the Hub claim, health and daemon ownership."""

from __future__ import annotations

import re
import time
from typing import Any

from .. import __version__
from ..claim import CLAIMED, PENDING, RELEASE_COMMAND, ClaimManager
from .settings import recovery_view
from .text import GUARD, MESSAGES, _bool, _dict, _names, _num, display_text


def _flags(value: Any) -> dict[str, bool]:
    if not isinstance(value, dict):
        return {}
    return {display_text(k, 64, redact=GUARD): v for k, v in list(value.items())[:64]
            if isinstance(k, str) and isinstance(v, bool)}


def _protocols(value: Any) -> dict[str, dict[str, int]]:
    if not isinstance(value, dict):
        return {}
    out = {}
    for name, span in list(value.items())[:8]:
        if isinstance(name, str) and isinstance(span, dict):
            out[display_text(name, 32, redact=GUARD)] = {k: span[k] for k in ("min", "max")
                                                         if isinstance(span.get(k), int)}
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
        view.update(key_id=display_text(key.get("key_id"), 64, redact=GUARD), claimed_at=_num(key.get("claimed_at")))
    return view


def health_view(body: Any) -> dict:
    body = _dict(body)
    caps = _dict(body.get("capabilities"))
    config = _dict(body.get("config"))
    return {
        "daemon": {
            "release": display_text(body.get("release"), 64, redact=GUARD),
            "build_id": display_text(body.get("build_id"), 64, redact=GUARD),
            "profile": display_text(body.get("profile") or caps.get("profile"), 32, redact=GUARD),
            "protocols": _protocols(body.get("protocols")),
        },
        "health": {
            "ok": body.get("ok") is True,
            "config_revision": config.get("revision") if isinstance(config.get("revision"), int) else None,
            "config_confirmed": _bool(config.get("confirmed")),
            "supervised_restart": _bool(config.get("supervised_restart")),
            "recovery": recovery_view(config.get("recovery")),
        },
        "capabilities": {
            "profile": display_text(caps.get("profile"), 32, redact=GUARD),
            "required": _flags(caps.get("required")),
            "modules": _flags(caps.get("modules")),
            "hosted_backends": _names(caps.get("hosted_backends")),
            "config_registry": _bool(caps.get("config_registry")),
        },
    }


# Owner API versions the Hub relies on, each from the daemon change that introduced it.
MIN_OWNER_API = (1, 24)  # agent-harness aa5a904, the minimum daemon in the README; harness/admin.py reports 1.24
TAILNET_OWNER_API = (1, 26)  # agent-harness #621: the discovery root lists allowed_logins for tailnet sign-in


def owner_api_compatibility(version: Any) -> str:
    """How the Hub's minimum compares with a reported Owner API version. 1.24 is a floor, not proof of support."""
    match = re.fullmatch(r"([0-9]+)\.([0-9]+)", version) if isinstance(version, str) else None
    if match is None:
        return "unknown"
    parsed = (int(match[1]), int(match[2]))
    if parsed < MIN_OWNER_API:
        return "too_old"
    if parsed < TAILNET_OWNER_API:
        return "no_tailnet_signin"
    return "supported"


def ownership_view(discovery: Any, hub_claim: Any, key_id: str) -> dict:
    """Whether the daemon has a Hub, and whether it is this one. Only the record's display fields are kept."""
    discovery = _dict(discovery)
    hub_claim = _dict(hub_claim)
    record = hub_claim.get("hub") if isinstance(hub_claim.get("hub"), dict) else None
    shown = None
    if record:
        shown = {"name": display_text(record.get("name"), 120, redact=GUARD),
                 "kind": display_text(record.get("kind"), 16, redact=GUARD),
                 "key_id": display_text(record.get("key_id"), 64, redact=GUARD),
                 "claimed_at": _num(record.get("claimed_at"))}
    api_version = display_text(discovery.get("api_version"), 16, redact=GUARD)
    return {
        "api_version": api_version,
        "compatibility": owner_api_compatibility(api_version),
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
