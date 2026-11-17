"""Browser projection for the Apps page: apps, erasures, limits and Spend."""

from __future__ import annotations

from typing import Any

from .text import GUARD, _app_id, _dict, _finite, _int, _list, _names, display_text

APP_STATES = {"active", "idle", "stale", "never_used", "revoked", "erasure_pending"}
SESSION_STATES = ("queued", "running", "waiting_approval", "waiting_target", "waiting_app", "waiting_limit",
                  "done", "failed", "cancelled")


def app_view(row: Any, key_id: str = "", secrets: tuple[str, ...] = ()) -> dict:
    row = _dict(row)
    store = _dict(row.get("store"))
    usage = store.get("usage")
    sessions = _dict(store.get("sessions"))
    out = {key: display_text(row.get(key), 200, redact=GUARD)
           for key in ("id", "name", "kind", "role", "catalog_app_id")}
    out["id"] = _app_id(row.get("id"))
    for field in ("name", "catalog_app_id"):
        out[field] = display_text(row.get(field), secrets=secrets)
    out.update({key: _finite(row.get(key)) for key in
                ("created_at", "last_used_at", "token_age_seconds", "revoked_at", "erase_after", "erased_at",
                 "retention_days")})
    out["scopes"] = display_text(row.get("scopes"), 2000, secrets=secrets)
    out["origins"] = [display_text(origin, 2000, secrets=secrets) for origin in _list(row.get("origins"))[:32]
                      if isinstance(origin, str)]
    state = row.get("state")
    out["state"] = state if isinstance(state, str) and state in APP_STATES else "unknown"
    out["errors"] = row.get("errors") is True
    out["this_hub"] = bool(key_id and row.get("id") == key_id)
    out["token_age_warning"] = out["token_age_seconds"] is not None and out["token_age_seconds"] >= 90 * 86400
    out["store"] = {
        "sessions": {key: _int(sessions[key]) for key in SESSION_STATES if key in sessions},
        "usage": usage_tally(usage),
        "errors": _int(store.get("errors")), "last_error": display_text(store.get("last_error"), 200, secrets=secrets),
        "last_error_at": _finite(store.get("last_error_at")),
    }
    return out


def apps_view(body: Any, key_id: str, secrets: tuple[str, ...] = ()) -> dict:
    body = _dict(body)
    entries = []
    for row in body.get("entries", []):
        if not isinstance(row, dict):
            continue
        app = _dict(_dict(row.get("manifest")).get("app"))
        entries.append({"catalog_app_id": display_text(row.get("catalog_app_id"), 200, secrets=secrets),
                        "name": display_text(app.get("display_name"), 200, secrets=secrets),
                        "state": display_text(row.get("state"), 32, redact=GUARD), "paired": _names(row.get("paired")),
                        "verified": row.get("verified") is True})
    return {"apps": [app_view(row, key_id, secrets) for row in body.get("apps", []) if isinstance(row, dict)],
            "entries": entries}


def erasures_view(body: Any, secrets: tuple[str, ...] = ()) -> list[dict]:
    return [{"id": _app_id(row.get("id")),
             **{key: display_text(row.get(key), 200, secrets=secrets) for key in ("name", "kind")},
             **{key: _finite(row.get(key)) for key in ("revoked_at", "erase_after")}}
            for row in _list(body) if isinstance(row, dict) and _app_id(row.get("id"))]


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
    body = _dict(body)
    out = {"app_id": display_text(body.get("app_id"), 200, redact=GUARD)}
    for group in ("configured", "effective"):
        values = _dict(body.get(group))
        out[group] = {key: _int(values.get(key)) for key in ("max_running", "max_queued")}
    out.update({key: _int(body.get(key)) for key in ("running", "queued", "parked")})
    return out


USAGE_FIELDS = ("requests", "prompt_tokens", "completion_tokens", "cost_usd")
SPEND_NEAR = 0.8  # an App whose session-cap use reaches this share of a cap is flagged as near its limit


def usage_tally(value: Any, local: bool = False) -> dict:
    """One usage tally; a local-model tally shows tokens only, so its cost is None (the owner's choice in #61)."""
    value = _dict(value)
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
    rows = _dict(inventory).get("apps", [])
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
    for row in _list(backends):
        if not isinstance(row, dict):
            continue
        billing = display_text(row.get("billing"), 32, redact=GUARD)
        local = billing == "local"
        out_backends.append({"name": display_text(row.get("name"), 80, secrets=secrets), "billing": billing,
                             "local": local,
                             "today": usage_tally(row.get("today"), local),
                             "week": usage_tally(row.get("week"), local)})
    return {"apps": apps, "backends": out_backends}
