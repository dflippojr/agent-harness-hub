"""What the browser may see: every daemon answer is copied field by field into these shapes, never passed through."""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
import unicodedata
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any
from urllib.parse import urlsplit

from . import __version__
from .administration import TARGET
from .claim import CLAIMED, PENDING, RELEASE_COMMAND, ClaimManager
from .guard import KEY_PREFIX_SHAPE, LINK_CODE, TOKEN_SHAPE

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
            "harness hub release --confirm, then start a new claim here.",
}


def _str(value: Any, limit: int = 200) -> str:
    if not isinstance(value, str):
        return ""
    for token in TOKEN_SHAPE.finditer(value):
        if token.start() >= limit:
            break
        if limit < token.end():
            return value[:token.end()]  # Cut short, it would pass the guard: keep it whole so the view is withheld.
    return value[:limit]


REDACTED = "[withheld: token-shaped]"


def _prefix(match: re.Match) -> str:
    # A full token is left in place: the response guard must withhold the entire view, not hide a daemon leak.
    return match[0] if TOKEN_SHAPE.match(match[0]) else REDACTED


def _diagnostic_text(value: Any, limit: int = 200) -> str:
    """Daemon diagnostic text, which can echo a key's display prefix: each token-shaped word is redacted in place."""
    # Cut first: a token cut short at the limit is then redacted, not passed under the guard's full-length shape.
    return KEY_PREFIX_SHAPE.sub(_prefix, value[:limit]) if isinstance(value, str) else ""


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
    discovery = discovery if isinstance(discovery, dict) else {}
    hub_claim = hub_claim if isinstance(hub_claim, dict) else {}
    record = hub_claim.get("hub") if isinstance(hub_claim.get("hub"), dict) else None
    shown = None
    if record:
        shown = {"name": _str(record.get("name"), 120), "kind": _str(record.get("kind"), 16),
                 "key_id": _str(record.get("key_id"), 64), "claimed_at": _num(record.get("claimed_at"))}
    api_version = _str(discovery.get("api_version"), 16)
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
    return {key: {"code": _str(row.get("code"), 64), "message": _diagnostic_text(row.get("message"), 2000)}
            for key, row in value.items() if isinstance(key, str) and isinstance(row, dict)}


def settings_error(body: Any, status: int) -> dict:
    body = body if isinstance(body, dict) else {}
    error = body.get("error") if isinstance(body.get("error"), dict) else {}
    details = error.get("details") if isinstance(error.get("details"), dict) else {}
    return {"detail": _diagnostic_text(body.get("detail"), 2000) or f"The daemon answered HTTP {status}",
            "error": {"code": _str(error.get("code"), 64), "keys": field_errors(error.get("keys")),
                      "details": {"expected_revision": _num(details.get("expected_revision")),
                                  "current_revision": _num(details.get("current_revision")),
                                  "manual": _diagnostic_text(details.get("manual"), 1000)}}}


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
        "warning": _diagnostic_text(body.get("warning"), 2000),
        "settings": [registry_entry(row) for row in rows],
        "changes": [{"key": _str(row.get("key"), 200), "from": setting_value(row.get("from")),
                     "to": setting_value(row.get("to")), "apply": _str(row.get("apply"), 32),
                     "action": _str(row.get("action"), 32)} for row in changes if isinstance(row, dict)],
        "errors": field_errors(body.get("errors")),
    }


def recovery_view(value: Any) -> dict | None:
    if not isinstance(value, dict):
        return {"recovery": _str(value, 64)} if isinstance(value, str) and value else None
    return {"recovery": _str(value.get("recovery"), 64), "reason": _diagnostic_text(value.get("reason"), 2000),
            "warning": _diagnostic_text(value.get("warning"), 2000), "at": _num(value.get("at"))}


def preflight_view(value: Any) -> dict | None:
    if not isinstance(value, dict):
        return None
    problems = value.get("problems") if isinstance(value.get("problems"), list) else []
    return {"ok": _bool(value.get("ok")),
            "problems": [_diagnostic_text(v, 64) for v in problems[:32] if isinstance(v, str)],
            "checked_at": _num(value.get("checked_at"))}


def backend_view(value: Any) -> dict:
    value = value if isinstance(value, dict) else {}
    models = value.get("popular_models") if isinstance(value.get("popular_models"), list) else []
    return {"name": _str(value.get("name"), 80), "model": _str(value.get("model"), 200),
            "effort": _str(value.get("effort"), 32), "available": _bool(value.get("available")),
            "popular_models": [{"id": _str(row.get("id"), 200), "label": _str(row.get("label"), 200)}
                               for row in models if isinstance(row, dict)]}


def memory_route_absent(body: Any) -> bool:
    """The daemon serves `/memory` only while its memory_library module is installed and switched on; otherwise the
    route is absent and the daemon answers its generic 404, which the page shows as a module state, not a fault."""
    return isinstance(body, dict) and body.get("detail") == "Not Found"


def control_view(operation: str, body: Any, installed: bool = True) -> Any:
    if operation == "backends":
        return [backend_view(row) for row in body] if isinstance(body, list) else []
    if operation == "backend_save":
        return backend_view(body)
    body = body if isinstance(body, dict) else {}
    if operation in ("smart", "smart_save"):
        return {"mode": _str(body.get("mode"), 32), "enabled": _bool(body.get("enabled")),
                "configured": _bool(body.get("configured")),
                "secret_ref": _diagnostic_text(body.get("secret_ref"), 4000)}
    if operation in ("memory", "memory_save"):
        commit = body.get("last_commit") if isinstance(body.get("last_commit"), dict) else {}
        return {"installed": installed, "enabled": _bool(body.get("enabled")),
                "profile": body.get("profile") if isinstance(body.get("profile"), str) else "",
                "profile_max_chars": _num(body.get("profile_max_chars")),
                "last_commit": {"head": _str(commit.get("head"), 64), "path": _str(commit.get("path"), 4000),
                                "summary": _str(commit.get("summary"), 200), "at": _num(commit.get("at")),
                                "pushed": _bool(commit.get("pushed"))},
                "refresh_error": _diagnostic_text(body.get("refresh_error"), 2000)}
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
                    fields[key] = _diagnostic_text(value, 200)
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
    if type(value) is int and value.bit_length() > 64:
        return None
    number = _num(value)
    return number if number is not None and math.isfinite(number) else None


def _app_id(value: Any) -> str:
    return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,80}", value) else ""


def _app_text(value: Any, limit: int = 200, secrets: tuple[str, ...] = ()) -> str:
    """Redact token-shaped display text without masking an exact credential leak from the guard."""
    if isinstance(value, str):
        for secret in secrets:
            if secret and secret in value:
                return secret  # The response guard must withhold the entire view.
        if KEY_PREFIX_SHAPE.search(value):
            return REDACTED
    return _str(value, limit)


def app_view(row: Any, key_id: str = "", secrets: tuple[str, ...] = ()) -> dict:
    row = row if isinstance(row, dict) else {}
    store = row.get("store") if isinstance(row.get("store"), dict) else {}
    usage = store.get("usage")
    sessions = store.get("sessions") if isinstance(store.get("sessions"), dict) else {}
    out = {key: _str(row.get(key), 200) for key in ("id", "name", "kind", "role", "catalog_app_id")}
    out["id"] = _app_id(row.get("id"))
    for field in ("name", "catalog_app_id"):
        out[field] = _app_text(row.get(field), secrets=secrets)
    out.update({key: _finite(row.get(key)) for key in
                ("created_at", "last_used_at", "token_age_seconds", "revoked_at", "erase_after", "erased_at",
                 "retention_days")})
    out["scopes"] = _app_text(row.get("scopes"), 2000, secrets)
    out["origins"] = [_app_text(origin, 2000, secrets) for origin in row.get("origins", [])[:32]
                      if isinstance(origin, str)] if isinstance(row.get("origins"), list) else []
    state = row.get("state")
    out["state"] = state if isinstance(state, str) and state in APP_STATES else "unknown"
    out["errors"] = row.get("errors") is True
    out["this_hub"] = bool(key_id and row.get("id") == key_id)
    out["token_age_warning"] = out["token_age_seconds"] is not None and out["token_age_seconds"] >= 90 * 86400
    out["store"] = {
        "sessions": {key: _int(sessions[key]) for key in SESSION_STATES if key in sessions},
        "usage": usage_tally(usage),
        "errors": _int(store.get("errors")), "last_error": _app_text(store.get("last_error"), 200, secrets),
        "last_error_at": _finite(store.get("last_error_at")),
    }
    return out


def apps_view(body: Any, key_id: str, secrets: tuple[str, ...] = ()) -> dict:
    body = body if isinstance(body, dict) else {}
    entries = []
    for row in body.get("entries", []):
        if not isinstance(row, dict):
            continue
        manifest = row.get("manifest") if isinstance(row.get("manifest"), dict) else {}
        app = manifest.get("app") if isinstance(manifest.get("app"), dict) else {}
        entries.append({"catalog_app_id": _app_text(row.get("catalog_app_id"), 200, secrets),
                        "name": _app_text(app.get("display_name"), 200, secrets),
                        "state": _str(row.get("state"), 32), "paired": _names(row.get("paired")),
                        "verified": row.get("verified") is True})
    return {"apps": [app_view(row, key_id, secrets) for row in body.get("apps", []) if isinstance(row, dict)],
            "entries": entries}


def erasures_view(body: Any, secrets: tuple[str, ...] = ()) -> list[dict]:
    return [{"id": _app_id(row.get("id")), **{key: _app_text(row.get(key), 200, secrets) for key in ("name", "kind")},
             **{key: _finite(row.get(key)) for key in ("revoked_at", "erase_after")}}
            for row in body if isinstance(row, dict) and _app_id(row.get("id"))] if isinstance(body, list) else []


def valid_limits(body: Any) -> bool:
    """Unknown or malformed caps must never become editable default resets."""
    if not isinstance(body, dict):
        return False
    for group in ("configured", "effective"):
        values = body.get(group)
        if not isinstance(values, dict):
            return False
        for key in ("max_running", "max_queued"):
            if key not in values:
                return False
            value = values[key]
            if value is None and group == "configured":
                continue
            if type(value) is not int or not 1 <= value <= 2**63 - 1:
                return False
    return True


def limits_view(body: Any) -> dict:
    body = body if isinstance(body, dict) else {}
    out = {"app_id": _str(body.get("app_id"), 200)}
    for group in ("configured", "effective"):
        values = body.get(group) if isinstance(body.get(group), dict) else {}
        out[group] = {key: _int(values.get(key)) for key in ("max_running", "max_queued")}
    out.update({key: _int(body.get(key)) for key in ("running", "queued", "parked")})
    return out


USAGE_FIELDS = ("requests", "prompt_tokens", "completion_tokens", "cost_usd")
SPEND_NEAR = 0.8  # an App whose session-cap use reaches this share of a cap is flagged as near its limit


def usage_tally(value: Any, local: bool = False) -> dict:
    """One usage tally; a local-model tally shows tokens only, so its cost is None (the owner's choice in #61)."""
    value = value if isinstance(value, dict) else {}
    out = {key: _finite(value.get(key)) for key in USAGE_FIELDS}
    if local:
        out["cost_usd"] = None
    return out


def cap_use(limits: Any) -> dict | None:
    """Sessions counted against an App's effective caps; parked sessions count toward both (as the daemon counts)."""
    if not valid_limits(limits):
        return None
    shown = limits_view(limits)
    running, queued, parked = (shown[key] or 0 for key in ("running", "queued", "parked"))
    caps = shown["effective"]
    running_used, queued_used = running + parked, queued + parked
    share = max(running_used / caps["max_running"], queued_used / caps["max_queued"])
    return {"max_running": caps["max_running"], "max_queued": caps["max_queued"], "running_used": running_used,
            "queued_used": queued_used, "parked": parked, "share": round(share, 4), "near": share >= SPEND_NEAR}


def spend_view(inventory: Any, backends: Any, limits: dict, key_id: str, secrets: tuple[str, ...] = ()) -> dict:
    """Every App's lifetime usage and session-cap use, and each backend's today and 7-day usage.

    The daemon has no per-App time windows yet (agent-harness#686), so per-App rows carry lifetime totals only."""
    rows = inventory.get("apps", []) if isinstance(inventory, dict) else []
    apps = []
    for row in rows:
        if not isinstance(row, dict) or row.get("kind") not in ("app", "device") or row.get("role") == "hub":
            continue
        shown = app_view(row, key_id, secrets)
        if not shown["id"] or shown["this_hub"]:
            continue
        apps.append({"id": shown["id"], "name": shown["name"], "kind": shown["kind"], "state": shown["state"],
                     "usage": shown["store"]["usage"], "caps": cap_use(limits.get(shown["id"]))})
    out_backends = []
    for row in backends if isinstance(backends, list) else []:
        if not isinstance(row, dict):
            continue
        billing = _str(row.get("billing"), 32)
        local = billing == "local"
        out_backends.append({"name": _app_text(row.get("name"), 80, secrets), "billing": billing, "local": local,
                             "today": usage_tally(row.get("today"), local),
                             "week": usage_tally(row.get("week"), local)})
    return {"apps": apps, "backends": out_backends}


TEXT = "text"
NUMBER = "number"
BOOL = "bool"
LONG = "long"
IDENTIFIER = "identifier"
HASH = "hash"
NAMES = [TEXT]
GOOGLE = {"linked": BOOL, "email": TEXT, "linked_at": NUMBER, "last_sign_in_at": NUMBER,
          "active_web_sessions": NUMBER, "invitation_expires_at": NUMBER, "revoked": NUMBER}
ACCOUNT = {**dict.fromkeys(("user_id", "account_hint", "login", "display_name"), TEXT),
           "enabled": BOOL, **dict.fromkeys(("disk_quota_bytes", "disk_used_bytes", "max_running", "max_queued",
                                            "running", "queued", "last_activity_at", "created_at",
                                            "updated_at"), NUMBER),
           "google": GOOGLE, "user_id": IDENTIFIER}
CREDENTIAL = {**dict.fromkeys(("id", "app_id", "app_name", "backend", "secret_ref", "policy"), TEXT),
              "id": IDENTIFIER, "models": NAMES, "created_at": NUMBER, "revoked_at": NUMBER, "available": BOOL}
FINDING = {"code": TEXT, "path": TEXT, "message": TEXT, "severity": TEXT}
REVIEW = {"recommendation": TEXT, "summary": LONG, "error": TEXT,
          **dict.fromkeys(("scope", "trigger_precision", "conflicts", "prompt_injection", "sensitive_data",
                           "examples"), MappingProxyType({"ok": BOOL, "notes": TEXT}))}
# dict.fromkeys shares one schema object between keys (and project() tells SKILL by identity), so the shared ones are
# read-only: a write through one operation would change every operation that shares it.
SKILL = MappingProxyType({
    **dict.fromkeys(("id", "slug", "title", "purpose", "activation_suggestion", "content_hash", "status",
                     "target_slug", "review_status"), TEXT),
    **dict.fromkeys(("version", "created_at", "updated_at", "installed_at", "example_count",
                     "reference_count"), NUMBER), "enabled": BOOL, "projects": NAMES,
    "static_findings": [FINDING], "review": REVIEW, "skill_md": LONG, "diff": LONG,
    "references": [{"path": TEXT, "content": LONG}],
    "examples": [{"prompt": LONG, "expected": LONG, "expected_behavior": LONG}],
    "source_session_id": TEXT, "id": IDENTIFIER, "slug": IDENTIFIER, "content_hash": HASH,
    "manifest": {**dict.fromkeys(("slug", "title", "purpose", "activation_suggestion",
                                  "content_hash", "validator_version"), TEXT)}})
RUNNER_UPDATE = {"ok": BOOL, "version": TEXT, "build_id": TEXT, "at": NUMBER, "message": TEXT,
                 "plist_changed": BOOL}
RUNNER = {"name": IDENTIFIER, "online": BOOL, "last_seen_seconds": NUMBER, "pending_requests": NUMBER,
          "update_supported": BOOL, "manual_update": TEXT, "last_update": RUNNER_UPDATE,
          "info": {"version": TEXT, "protocol": NUMBER, "platform": TEXT, "arch": TEXT},
          "compatibility": {"state": TEXT, "notice": TEXT, "supported": {"min": NUMBER, "max": NUMBER}}}
PROJECT = {"name": IDENTIFIER, "description": TEXT, "repo": BOOL, "homelab": BOOL, "target": TEXT, "managed": BOOL}
FOLDER = {**dict.fromkeys(("project", "name", "folder", "path", "status", "invalid", "reason", "message"), TEXT),
          **dict.fromkeys(("running", "trusted", "git", "managed", "owner_only", "trust_required"), BOOL),
          "pid": NUMBER, "started_at": NUMBER, "trust_prompt_open": BOOL, "project": IDENTIFIER}
CANDIDATE = {"id": IDENTIFIER, "suggested_slug": IDENTIFIER, "path": TEXT, "reason": TEXT, "markers": NAMES,
             **dict.fromkeys(("git", "requires_git", "launchable", "configured_duplicate", "promoted",
                             "trusted"), BOOL)}
SCAN = MappingProxyType({"id": IDENTIFIER, "revision": NUMBER, "status": TEXT, "visited": NUMBER,
                         "candidates": [CANDIDATE], "errors": [{"location": TEXT, "code": TEXT}], "truncated": BOOL,
                         "reason": TEXT, "expires_in": NUMBER})
# Audit metadata is deliberately narrower than free text or caller-defined objects.
AUDIT = {**dict.fromkeys(("actor_id", "actor_kind", "key_id", "source", "target_id", "target_kind", "action",
                          "outcome"), TEXT),
         "id": NUMBER, "ts": NUMBER, "at": NUMBER, "timestamp": NUMBER, "created_at": NUMBER, "detail": TEXT,
         "metadata": {"fields": NAMES, "reason": TEXT, "operation_id": TEXT, "category": TEXT,
                      **dict.fromkeys(("count", "removed", "kept", "expired", "new_max_running", "old_max_running",
                                      "new_max_queued", "old_max_queued", "new_disk_quota_bytes",
                                      "old_disk_quota_bytes"), NUMBER)}}
CLEANUP = {"at": NUMBER, "removed": NUMBER, "kept": NUMBER, "expired": NUMBER,
           "errors": NAMES, "warnings": NAMES, "ok": BOOL,
           **dict.fromkeys(("containers_removed", "workspaces_removed", "orphans_removed",
                           "apps_erased", "sessions_expired"), NUMBER)}
BACKUP = {"enabled": BOOL, "at": NUMBER, "ok_at": NUMBER, "path": TEXT, "bytes": NUMBER, "files": NUMBER,
          "app_stores": NUMBER, "removed": NAMES, "dir": TEXT,
          "transcript_archives": NUMBER, "warnings": NAMES, "error": TEXT, "ok": BOOL,
          "last_backup": {"at": NUMBER, "path": TEXT, "bytes": NUMBER, "transcript_archives": NUMBER,
                          "warnings": NAMES}}
RETENTION = {"enabled": BOOL, "keep_days": NUMBER, "count": NUMBER, "bytes": NUMBER,
             "confirmation": TEXT, "removed": NUMBER, "errors": NAMES}
MAINTENANCE = {**dict.fromkeys(("workspaces_mb", "checkpoints_mb", "free_gb", "total_gb", "quota_mb"), NUMBER),
               "last_cleanup": CLEANUP, "backup": BACKUP, "runners": [RUNNER],
               "containers": [{"name": TEXT, "state": TEXT, "size": TEXT}],
               "image_archive": {"enabled": BOOL, "ok": BOOL, "error": TEXT, "count": NUMBER, "bytes": NUMBER}}
RESULT = {"ok": BOOL, "removed": BOOL, "slug": TEXT, "already_added": BOOL, "message": TEXT,
          "version": TEXT, "updated": BOOL, "pid": NUMBER}
SCHEMAS = {
    "accounts": [ACCOUNT], "account": ACCOUNT, "account_add": ACCOUNT, "account_save": ACCOUNT,
    "github_reset": {"enabled": BOOL, "configured": BOOL},
    "google_invite": {"code": TEXT, "expires_at": NUMBER},
    "google_cancel": GOOGLE, "google_revoke": GOOGLE, "google_unlink": GOOGLE,
    "credentials": [CREDENTIAL], "credential_set": CREDENTIAL,
    "skills": {"enabled": BOOL, "proposals": [SKILL], "installed": [SKILL]}, "skills_enabled": [SKILL],
    **dict.fromkeys(("proposal", "skill_install", "skill_reject", "skill_reopen", "skill_review", "skill_enable",
                     "skill_disable", "skill_projects", "skill_rollback", "skill_export"), SKILL),
    "skill_review": {"job_id": TEXT, "usage_notice": TEXT, "reviewer_model": TEXT},
    "maintenance": MAINTENANCE, "cleanup": CLEANUP, "backup": BACKUP,
    "retention_preview": RETENTION, "retention_apply": RETENTION,
    "runners": [RUNNER], "runner_update": RUNNER_UPDATE, "projects": [PROJECT], "project_add": PROJECT,
    "remote": {"enabled": BOOL, "projects": [FOLDER], "discovery": {"supported": BOOL, "enabled": BOOL}},
    **dict.fromkeys(("scan_start", "scan", "scan_cancel"), SCAN),
    "account_audit": [AUDIT], "audit": {"items": [AUDIT], "next_before_id": NUMBER},
}


def project(value: Any, schema: Any, secrets: tuple[str, ...] = ()) -> Any:
    if value is None:
        return None
    if isinstance(schema, Mapping):
        if not isinstance(value, dict):
            raise ValueError("Invalid daemon object")
        shown = {key: project(value[key], shape, secrets) for key, shape in schema.items() if key in value}
        if schema is SKILL and "projects" in value:
            shown["projects_complete"] = isinstance(value["projects"], list) and value["projects"] == shown["projects"]
        return shown
    if isinstance(schema, list):
        if not isinstance(value, list):
            raise ValueError("Invalid daemon list")
        if isinstance(schema[0], Mapping) and any(not isinstance(row, dict) for row in value):
            raise ValueError("Invalid daemon list row")
        return [project(row, schema[0], secrets) for row in value[:1000]]
    if schema in (TEXT, LONG, IDENTIFIER, HASH):
        if not isinstance(value, str):
            raise ValueError("Invalid daemon text")
        shown = _app_text(value, 262144 if schema == LONG else 32768, secrets)
        if schema in (IDENTIFIER, HASH) and not any(secret and secret in value for secret in secrets):
            pattern = TARGET if schema == IDENTIFIER else re.compile(r"[a-fA-F0-9]{64}")
            return value if pattern.fullmatch(value) and shown == value else ""
        return shown
    if schema == BOOL:
        if _bool(value) is None:
            raise ValueError("Invalid daemon boolean")
        return value
    if _finite(value) is None:
        raise ValueError("Invalid daemon number")
    return _int(value) if type(value) is int else _finite(value)


def complete_skill_review(body: dict, shown: dict) -> bool:
    """An installable review contains every hash-covered byte, with no lossy projection."""
    fields = ("slug", "title", "purpose", "activation_suggestion", "skill_md", "references", "examples")
    return bool(shown.get("id") and shown.get("content_hash") and all(
        key in body and body[key] == shown.get(key) for key in fields
    ) and all(isinstance(body[key], str) for key in fields[:5])
        and isinstance(body["references"], list) and isinstance(body["examples"], list) and all(
        isinstance(row, dict) and set(row) == {"path", "content"}
        and isinstance(row["path"], str) and isinstance(row["content"], str)
        for row in body["references"]
    ) and all(
        isinstance(row, dict) and "prompt" in row and ("expected" in row or "expected_behavior" in row)
        and set(row) <= {"prompt", "expected", "expected_behavior"}
        and all(isinstance(value, str) for value in row.values()) for row in body["examples"]
    ))


def administration_view(operation: str, body: Any, secrets: tuple[str, ...] = ()) -> Any:
    schema = SCHEMAS.get(operation, RESULT)
    if not isinstance(body, list if isinstance(schema, list) else dict):
        raise ValueError("Invalid daemon response")
    shown = project(body, schema, secrets)
    if operation == "proposal":
        shown["review_complete"] = complete_skill_review(body, shown)
    if operation in ("scan_start", "scan", "scan_cancel"):
        for raw, candidate in zip(body.get("candidates", []), shown.get("candidates", []), strict=False):
            candidate["promotion_available"] = bool(candidate.get("id") and candidate.get("suggested_slug")
                                                    and all(raw.get(k) == candidate.get(k)
                                                            for k in ("path", "markers")))
    if operation == "google_invite":
        code = body.get("code")
        if (not isinstance(code, str) or not LINK_CODE.fullmatch(code) or shown.get("expires_at") is None
                or any(secret and secret in code for secret in secrets)):
            raise ValueError("Invalid invitation response")  # Never show a redaction marker as if it were the code.
        shown["code"] = code  # The owner's one-time code: redacting a lookalike substring would hide the only copy.
    return shown


# App pairing requests (#519). The Hub keeps its own copy of the elevated scopes so a daemon that forgets to flag one
# still cannot have it approved without the owner's separate acknowledgement.
PAIRING_STATES = ("pending", "armed", "claimed", "approved", "redeemed", "denied", "expired")
PAIRING_NEEDS = ("approve", "claim", "confirm", "redeem")
ELEVATED_SCOPES = frozenset({"sessions:all", "approvals", "remote_control", "memory_library", "homelab"})


def pairing_request_view(row: Any, secrets: tuple[str, ...] = ()) -> dict:
    row = row if isinstance(row, dict) else {}
    kind = row.get("kind") if row.get("kind") in ("app", "hub") else "unknown"
    state = row.get("state") if row.get("state") in PAIRING_STATES else "unknown"
    scopes = [_app_text(s, 64, secrets) for s in row.get("scopes", [])[:32] if isinstance(s, str)] if isinstance(
        row.get("scopes"), list) else []
    disclosures = []
    for item in row.get("disclosures", [])[:32] if isinstance(row.get("disclosures"), list) else []:
        if isinstance(item, dict) and isinstance(item.get("scope"), str):
            disclosures.append({"scope": _app_text(item["scope"], 64, secrets),
                                "tier": "elevated" if item.get("tier") == "elevated" else "standard",
                                "text": _app_text(item.get("text"), 4000, secrets)})
    flagged = set(_names(row.get("elevated"))) | {d["scope"] for d in disclosures if d["tier"] == "elevated"}
    elevated = [s for s in scopes if s in ELEVATED_SCOPES or s in flagged or s == "admin"]
    for item in disclosures:
        if item["scope"] in elevated:
            item["tier"] = "elevated"
    # Owner decision: codes come only from the App's screen, never from an owner listing (#519 step 2).
    needs = row.get("needs") if row.get("needs") in PAIRING_NEEDS else ""
    return {"id": _app_id(row.get("id")), "kind": kind, "state": state, "needs": needs,
            "name": _app_text(row.get("name"), 120, secrets),
            "catalog_app_id": _app_text(row.get("catalog_app_id"), 200, secrets),
            "origin": _app_text(row.get("origin"), 500, secrets),
            "browser": row.get("browser") is True, "armed": row.get("armed") is True,
            "scopes": scopes, "elevated": elevated, "disclosures": disclosures,
            "key_id": _app_id(row.get("key_id")),
            **{key: _finite(row.get(key)) for key in ("created_at", "expires_at", "approved_at", "finished_at")}}


def pairing_attention(body: Any, now: float) -> dict | None:
    """How many unexpired App requests wait on an owner decision. Counts only: no ids, names, origins or codes.

    Hub claims are decided on the daemon host, and `claim`/`redeem` are the App's next step, so neither is counted."""
    if not isinstance(body, list):
        return None
    counts = {"approve": 0, "confirm": 0}
    for raw in body:
        row = pairing_request_view(raw)
        if row["kind"] == "app" and row["needs"] in counts and (row["expires_at"] or 0) > now:
            counts[row["needs"]] += 1
    return counts


def pairing_view(body: Any, secrets: tuple[str, ...] = ()) -> dict:
    rows = [pairing_request_view(row, secrets) for row in body if isinstance(row, dict)] if isinstance(
        body, list) else []
    rows = [row for row in rows if row["id"]]
    rows.sort(key=lambda row: row["created_at"] or 0, reverse=True)
    return {"requests": rows, "now": time.time()}


# Marketplace (agent-harness #520): the daemon's validated local entries. Listing grants nothing. Only display fields
# are copied; the manifest's URLs, e-mail addresses and digests never reach the browser, and nothing from an entry is
# ever fetched. Until a slot is armed the owner reads each scope's disclosure from this copy of the daemon's own copy
# (agent-harness harness/pairing_requests.py DISCLOSURES, docs/marketplace-design.md section 5.2); once armed, the
# daemon's `disclosures` on the request are checked before hand-off. Drift withdraws the slot and replaces the
# consent copy with daemon text. tests/e2e compares the entire built-in tables.
SCOPE_DISCLOSURES = {
    "sessions": "This app can start agent tasks on your harness and read the tasks it created. It cannot see other "
                "apps' or your Control Center sessions.",
    "sessions:all": "This app can **read every session on this harness**, including work you started yourself and "
                    "work other apps started. It cannot create sessions unless it also has `sessions`.",
    "approvals": "This app can **approve or deny tool calls** in its own tasks, including shell and file changes the "
                 "agent requests. That is a substitute for you tapping Approve.",
    "images": "This app can generate images on your harness. Image jobs pause language-model work for a few minutes.",
    "inference": "This app can call your local model as a raw completion API, outside an agent session. That still "
                 "uses the same GPU.",
    "remote_control": "This app can **start Claude Remote Control** in a project folder. Those sessions run as native "
                      "Claude Code on your machine: no harness queue, sandbox, or harness approvals.",
    "models:warm": "This app can **load your local model** when you start chatting in it. Loading takes RAM and the "
                   "GPU for about a minute.",
    "memory_library": "This app's tasks can **read your memory library** and propose changes to it. You still approve "
                      "every change.",
    "homelab": "This app's tasks can **read your homelab services' logs, config and metrics** and ask to restart or "
               "rebuild them.",
}
CATALOG_APP_ID = re.compile(r"[a-z0-9](?:[a-z0-9.-]{0,118}[a-z0-9])?")
SCOPE_NAME = re.compile(r"[a-z][a-z0-9_:]{0,63}")
# Only an inline raster image can be an icon: never a URL, never SVG (which can carry script and external references).
INLINE_ICON = re.compile(r"data:image/(?:png|jpeg|gif|webp);base64,[A-Za-z0-9+/]{4,65536}={0,2}")
ENTRY_STATES = ("paired", "not_paired")
SLOT_STATES = ("armed", "claimed", "approved")


def _key_ids(value: Any) -> list[str]:
    return [_app_id(v) for v in value[:64] if _app_id(v)] if isinstance(value, list) else []


def entry_text(value: Any, limit: int = 200, secrets: tuple[str, ...] = ()) -> str:
    """Publisher display text cannot reorder surrounding labels or carry invisible controls."""
    if isinstance(value, str):
        value = "".join(char for char in value if unicodedata.category(char) not in {"Cc", "Cf"})
    return _app_text(value, limit, secrets)


def normalize_origin(value: str) -> str:
    """Mirror the daemon's apps.normalize_origin for entry binding comparisons."""
    parsed = urlsplit(value.strip())
    port = parsed.port
    if (parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or parsed.path not in ("", "/")):
        raise ValueError("origin must contain only a scheme, host, and optional port")
    host = parsed.hostname.lower()
    if parsed.scheme == "http" and host not in ("localhost", "127.0.0.1", "::1"):
        raise ValueError("browser origins must use https")
    if ":" in host:
        host = f"[{host}]"
    default_port = (parsed.scheme == "http" and port == 80) or (parsed.scheme == "https" and port == 443)
    return f"{parsed.scheme}://{host}{f':{port}' if port is not None and not default_port else ''}"


def entry_digest(entry: dict) -> str:
    """Bind consent to every displayed entry field; live slots and the digest itself are excluded."""
    shown = {key: value for key, value in entry.items() if key not in {"slots", "consent_digest"}}
    canonical = json.dumps(shown, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    return hashlib.sha256(canonical).hexdigest()


def scope_disclosures(value: Any, scopes: list[str], secrets: tuple[str, ...] = ()) -> list[dict] | None:
    """Only complete, lossless daemon disclosure rows may become the next consent screen."""
    if not isinstance(value, list) or len(value) != len(scopes):
        return None
    by_scope = {}
    for item in value:
        if not isinstance(item, dict) or not isinstance(item.get("scope"), str) or item.get("scope") not in scopes or (
                item.get("tier") not in ("standard", "elevated")) or not isinstance(item.get("text"), str):
            return None
        text = item["text"]
        if (not text.strip() or _app_text(text, 4000, secrets) != text or item["scope"] in by_scope
                or any(secret and secret in text for secret in secrets)):
            return None
        by_scope[item["scope"]] = {key: item[key] for key in ("scope", "tier", "text")}
    return [by_scope[scope] for scope in scopes] if set(by_scope) == set(scopes) else None


def entry_view(row: Any, secrets: tuple[str, ...] = ()) -> dict | None:
    """One entry as the daemon validated it, or None when it does not match the entries contract."""
    if not isinstance(row, dict) or row.get("verified") is not False or row.get("state") not in ENTRY_STATES:
        return None
    manifest = row.get("manifest") if isinstance(row.get("manifest"), dict) else {}
    app = manifest.get("app") if isinstance(manifest.get("app"), dict) else {}
    catalog_app_id = row.get("catalog_app_id")
    if not isinstance(catalog_app_id, str) or not CATALOG_APP_ID.fullmatch(catalog_app_id) or (
            app.get("app_id") != catalog_app_id) or not isinstance(app.get("display_name"), str):
        return None
    publisher = app.get("publisher") if isinstance(app.get("publisher"), dict) else {}
    declared = app.get("scopes")
    if not isinstance(declared, list) or len(declared) > 16:
        return None
    scopes = []
    for item in declared:
        scope = item.get("scope") if isinstance(item, dict) else None
        if not isinstance(scope, str) or not SCOPE_NAME.fullmatch(scope) or scope == "admin":
            return None
        tier = "elevated" if scope in ELEVATED_SCOPES or item.get("risk_tier") == "elevated" else "standard"
        scopes.append({"scope": scope, "tier": tier, "text": SCOPE_DISCLOSURES.get(scope, ""),
                       "justification": entry_text(item.get("justification"), 1000, secrets)})
    if not scopes:
        return None
    origins = app.get("browser_origins") if isinstance(app.get("browser_origins"), list) else []
    # Binding fields must survive the display projection exactly: never arm a prefix or a redacted value.
    if len(origins) > 8 or any(not isinstance(origin, str) or len(origin) > 300
                               or _app_text(origin, 300) != origin
                               or any(unicodedata.category(char) in {"Cc", "Cf"} for char in origin)
                               for origin in origins):
        return None
    try:
        for origin in origins:
            normalize_origin(origin)
    except ValueError:
        return None
    unverified = row.get("unverified_origin") if isinstance(row.get("unverified_origin"), dict) else {}
    icon = app.get("icon")
    shown = {"catalog_app_id": catalog_app_id, "name": entry_text(app.get("display_name"), 80, secrets),
            "version": entry_text(app.get("version"), 64, secrets),
            "summary": entry_text(app.get("summary"), 280, secrets),
            "publisher": {"name": entry_text(publisher.get("name"), 80, secrets),
                          "id": entry_text(publisher.get("id"), 64, secrets)},
            "icon": icon if isinstance(icon, str) and INLINE_ICON.fullmatch(icon) else "",
            "scopes": scopes, "elevated": [item["scope"] for item in scopes if item["tier"] == "elevated"],
            "browser_origins": list(origins),
            "verified": False, "state": row["state"], "paired": _key_ids(row.get("paired")),
            "pending_pairing": row.get("pending_pairing") is True,
            "unverified_origin": {"paired": _key_ids(unverified.get("paired")),
                                  "pending_pairing": unverified.get("pending_pairing") is True}}

    shown["consent_digest"] = entry_digest(shown)
    return shown


def entries_view(body: Any, secrets: tuple[str, ...] = ()) -> tuple[list[dict], int]:
    """The entries that match the contract, and how many did not (those are withheld, never shown raw)."""
    rows = body.get("entries") if isinstance(body, dict) and isinstance(body.get("entries"), list) else []
    shown = [entry_view(row, secrets) for row in rows]
    kept = [row for row in shown if row is not None]
    return kept, len(shown) - len(kept)


def marketplace_slots(requests: Any, secrets: tuple[str, ...] = ()) -> dict[str, list[dict]]:
    """Armed slots still in play, by catalog app id. App-initiated requests stay on the Pairing requests page."""
    slots: dict[str, list[dict]] = {}
    for row in requests if isinstance(requests, list) else []:
        if not isinstance(row, dict) or row.get("kind") != "app" or row.get("armed") is not True:
            continue
        shown = pairing_request_view(row, secrets)
        if shown["id"] and shown["state"] in SLOT_STATES and CATALOG_APP_ID.fullmatch(shown["catalog_app_id"]):
            slots.setdefault(shown["catalog_app_id"], []).append(shown)
    return slots
