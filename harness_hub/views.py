"""What the browser may see: every daemon answer is copied field by field into these shapes, never passed through."""

from __future__ import annotations

import math
import re
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


def _int(value: Any) -> int | None:
    return value if type(value) is int else None


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
            "recovery": recovery_view(config.get("recovery")),
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


# Settings values are scalars or lists of strings in the daemon registry. No
# arbitrary nested objects (credentials, diagnostics, or provider state) survive.
def setting_value(value: Any) -> Any:
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return list(value)
    return None


def field_errors(value: Any) -> dict:
    if not isinstance(value, dict):
        return {}
    return {key: {"code": _str(row.get("code"), 64), "message": _str(row.get("message"), 2000)}
            for key, row in value.items() if isinstance(key, str) and isinstance(row, dict)}


def settings_error(body: Any, status: int) -> dict:
    body = body if isinstance(body, dict) else {}
    error = body.get("error") if isinstance(body.get("error"), dict) else {}
    details = error.get("details") if isinstance(error.get("details"), dict) else {}
    return {"detail": _str(body.get("detail"), 2000) or f"The daemon answered HTTP {status}",
            "error": {"code": _str(error.get("code"), 64), "keys": field_errors(error.get("keys")),
                      "details": {"expected_revision": _num(details.get("expected_revision")),
                                  "current_revision": _num(details.get("current_revision")),
                                  "manual": _str(details.get("manual"), 1000)}}}


def registry_entry(body: Any) -> dict:
    body = body if isinstance(body, dict) else {}
    out = {key: _str(body.get(key), 4000) for key in
           ("key", "label", "help", "category", "type", "scope", "apply", "sensitivity", "guidance", "source")}
    out.update({key: _bool(body.get(key)) for key in ("readable", "writable", "available", "file_only")})
    out.update({key: _num(body.get(key)) for key in ("minimum", "maximum")})
    out["enum"] = setting_value(body.get("enum"))
    out["modules"] = _names(body.get("modules"))
    hidden = body.get("sensitivity") in ("hidden", "redact") or body.get("readable") is False
    for key in ("default", "configured", "effective", "inherited", "pending"):
        out[key] = None if hidden else setting_value(body.get(key))
    return out


def registry_view(body: Any) -> dict:
    body = body if isinstance(body, dict) else {}
    rows = body.get("settings") if isinstance(body.get("settings"), list) else []
    changes = body.get("changes") if isinstance(body.get("changes"), list) else []
    return {
        **{key: _int(body.get(key)) for key in
           ("schema_version", "revision", "pending_revision", "target_revision")},
        **{key: _bool(body.get(key)) for key in
           ("confirmed", "supervised_restart", "restart_required", "accepted")},
        "status": _str(body.get("status"), 64),
        "recovery": recovery_view(body.get("recovery")),
        "warning": _str(body.get("warning"), 2000),
        "settings": [registry_entry(row) for row in rows],
        "changes": [{"key": _str(row.get("key"), 200), "from": setting_value(row.get("from")),
                     "to": setting_value(row.get("to")), "apply": _str(row.get("apply"), 32),
                     "action": _str(row.get("action"), 32)} for row in changes if isinstance(row, dict)],
        "errors": field_errors(body.get("errors")),
    }


def recovery_view(value: Any) -> dict | None:
    if not isinstance(value, dict):
        return {"recovery": _str(value, 64)} if isinstance(value, str) and value else None
    return {"recovery": _str(value.get("recovery"), 64), "reason": _str(value.get("reason"), 2000),
            "warning": _str(value.get("warning"), 2000), "at": _num(value.get("at"))}


def preflight_view(value: Any) -> dict | None:
    if not isinstance(value, dict):
        return None
    return {"ok": _bool(value.get("ok")), "problems": _names(value.get("problems")),
            "checked_at": _num(value.get("checked_at"))}


def backend_view(value: Any) -> dict:
    value = value if isinstance(value, dict) else {}
    models = value.get("popular_models") if isinstance(value.get("popular_models"), list) else []
    return {"name": _str(value.get("name"), 80), "model": _str(value.get("model"), 200),
            "effort": _str(value.get("effort"), 32), "available": _bool(value.get("available")),
            "popular_models": [{"id": _str(row.get("id"), 200), "label": _str(row.get("label"), 200)}
                               for row in models if isinstance(row, dict)]}


def control_view(operation: str, body: Any) -> Any:
    if operation == "backends":
        return [backend_view(row) for row in body] if isinstance(body, list) else []
    if operation == "backend_save":
        return backend_view(body)
    body = body if isinstance(body, dict) else {}
    if operation in ("smart", "smart_save"):
        return {"mode": _str(body.get("mode"), 32), "enabled": _bool(body.get("enabled")),
                "configured": _bool(body.get("configured")), "secret_ref": _str(body.get("secret_ref"), 4000)}
    if operation in ("memory", "memory_save"):
        commit = body.get("last_commit") if isinstance(body.get("last_commit"), dict) else {}
        return {"enabled": _bool(body.get("enabled")),
                "profile": body.get("profile") if isinstance(body.get("profile"), str) else "",
                "profile_max_chars": _num(body.get("profile_max_chars")),
                "last_commit": {"head": _str(commit.get("head"), 64), "path": _str(commit.get("path"), 4000),
                                "summary": _str(commit.get("summary"), 200), "at": _num(commit.get("at")),
                                "pushed": _bool(commit.get("pushed"))},
                "refresh_error": _str(body.get("refresh_error"), 2000)}
    if operation in ("github", "github_save"):
        return {"enabled": _bool(body.get("enabled")), "configured": _bool(body.get("configured")),
                "preflight": preflight_view(body.get("preflight"))}
    if operation == "google":
        return {"enabled": _bool(body.get("enabled")), "ready": _bool(body.get("ready")),
                "preflight": preflight_view(body.get("preflight"))}
    return {"sent": _bool(body.get("sent"))}


def module_view(body: Any) -> list[dict]:
    """Only module identity, presence and up to eight scalar status fields reach the browser."""
    rows = body.get("modules", []) if isinstance(body, dict) else []
    out = []
    for row in rows:
        shown = {"name": _str(row.get("name"), 200), "state": row.get("state")}
        detail = row.get("status")
        if isinstance(detail, dict):
            fields = {}
            for key, value in list(detail.items())[:8]:
                if not isinstance(key, str) or len(key) > 200:
                    continue
                if isinstance(value, str):
                    fields[key] = _str(value, 200)
                elif value is None or type(value) is bool or type(value) is int and value.bit_length() <= 64 or (
                        type(value) is float and math.isfinite(value)):
                    fields[key] = value
            shown["status"] = fields
        out.append(shown)
    return out


APP_STATES = {"active", "idle", "stale", "never_used", "revoked", "erasure_pending"}
SESSION_STATES = ("queued", "running", "waiting_approval", "waiting_target", "waiting_app", "waiting_limit",
                  "done", "failed", "cancelled")


def _finite(value: Any) -> float | None:
    number = _num(value)
    return number if number is not None and math.isfinite(number) else None


def app_view(row: Any, key_id: str = "") -> dict:
    row = row if isinstance(row, dict) else {}
    store = row.get("store") if isinstance(row.get("store"), dict) else {}
    usage = store.get("usage") if isinstance(store.get("usage"), dict) else {}
    sessions = store.get("sessions") if isinstance(store.get("sessions"), dict) else {}
    out = {key: _str(row.get(key), 200) for key in ("id", "name", "kind", "role", "catalog_app_id")}
    out["id"] = out["id"] if re.fullmatch(r"[A-Za-z0-9_-]{1,80}", out["id"]) else ""
    out.update({key: _finite(row.get(key)) for key in
                ("created_at", "last_used_at", "token_age_seconds", "revoked_at", "erase_after", "erased_at",
                 "retention_days")})
    out["scopes"] = _str(row.get("scopes"), 2000)
    out["origins"] = [_str(origin, 2000) for origin in row.get("origins", [])[:32]
                      if isinstance(origin, str)] if isinstance(row.get("origins"), list) else []
    state = row.get("state")
    out["state"] = state if isinstance(state, str) and state in APP_STATES else "unknown"
    out["errors"] = row.get("errors") is True
    out["this_hub"] = bool(key_id and row.get("id") == key_id)
    out["token_age_warning"] = out["token_age_seconds"] is not None and out["token_age_seconds"] >= 90 * 86400
    out["store"] = {
        "sessions": {key: _int(sessions[key]) for key in SESSION_STATES if key in sessions},
        "usage": {key: _finite(usage.get(key)) for key in
                  ("requests", "prompt_tokens", "completion_tokens", "cost_usd")},
        "errors": _int(store.get("errors")), "last_error": _str(store.get("last_error"), 200),
        "last_error_at": _finite(store.get("last_error_at")),
    }
    return out


def apps_view(body: Any, key_id: str) -> dict:
    body = body if isinstance(body, dict) else {}
    entries = []
    for row in body.get("entries", []):
        if not isinstance(row, dict):
            continue
        manifest = row.get("manifest") if isinstance(row.get("manifest"), dict) else {}
        app = manifest.get("app") if isinstance(manifest.get("app"), dict) else {}
        entries.append({"catalog_app_id": _str(row.get("catalog_app_id"), 200),
                        "name": _str(app.get("display_name"), 200),
                        "state": _str(row.get("state"), 32), "paired": _names(row.get("paired")),
                        "verified": row.get("verified") is True})
    return {"apps": [app_view(row, key_id) for row in body.get("apps", []) if isinstance(row, dict)],
            "entries": entries}


def erasures_view(body: Any) -> list[dict]:
    return [{**{key: _str(row.get(key), 200) for key in ("id", "name", "kind")},
             **{key: _finite(row.get(key)) for key in ("revoked_at", "erase_after")}}
            for row in body if isinstance(row, dict)] if isinstance(body, list) else []


def limits_view(body: Any) -> dict:
    body = body if isinstance(body, dict) else {}
    out = {"app_id": _str(body.get("app_id"), 200)}
    for group in ("configured", "effective"):
        values = body.get(group) if isinstance(body.get(group), dict) else {}
        out[group] = {key: _int(values.get(key)) for key in ("max_running", "max_queued")}
    out.update({key: _int(body.get(key)) for key in ("running", "queued", "parked")})
    return out
