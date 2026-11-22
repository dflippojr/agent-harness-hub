"""Browser projection for the Settings page: the registry, recovery, preflight, backends and controls."""

from __future__ import annotations

from typing import Any

from .text import GUARD, WORD, _bool, _dict, _int, _list, _names, _num, display_text


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
    return {key: {"code": display_text(row.get("code"), 64, redact=GUARD),
                  "message": display_text(row.get("message"), 2000, redact=WORD)}
            for key, row in value.items() if isinstance(key, str) and isinstance(row, dict)}


def settings_error(body: Any, status: int) -> dict:
    body = _dict(body)
    error = _dict(body.get("error"))
    details = _dict(error.get("details"))
    return {"detail": display_text(body.get("detail"), 2000, redact=WORD) or f"The daemon answered HTTP {status}",
            "error": {"code": display_text(error.get("code"), 64, redact=GUARD),
                      "keys": field_errors(error.get("keys")),
                      "details": {"expected_revision": _num(details.get("expected_revision")),
                                  "current_revision": _num(details.get("current_revision")),
                                  "manual": display_text(details.get("manual"), 1000, redact=WORD)}}}


def registry_entry(body: Any) -> dict:
    body = _dict(body)
    out = {key: display_text(body.get(key), 4000, redact=GUARD) for key in
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
    body = _dict(body)
    rows = _list(body.get("settings"))
    changes = _list(body.get("changes"))
    return {
        **{key: _int(body.get(key)) for key in
           ("schema_version", "revision", "pending_revision", "target_revision")},
        **{key: _bool(body.get(key)) for key in
           ("confirmed", "supervised_restart", "restart_required", "accepted")},
        "status": display_text(body.get("status"), 64, redact=GUARD),
        "recovery": recovery_view(body.get("recovery")),
        "warning": display_text(body.get("warning"), 2000, redact=WORD),
        "settings": [registry_entry(row) for row in rows],
        "changes": [{"key": display_text(row.get("key"), 200, redact=GUARD), "from": setting_value(row.get("from")),
                     "to": setting_value(row.get("to")), "apply": display_text(row.get("apply"), 32, redact=GUARD),
                     "action": display_text(row.get("action"), 32, redact=GUARD)}
                    for row in changes if isinstance(row, dict)],
        "errors": field_errors(body.get("errors")),
    }


def recovery_view(value: Any) -> dict | None:
    if not isinstance(value, dict):
        return {"recovery": display_text(value, 64, redact=GUARD)} if isinstance(value, str) and value else None
    return {"recovery": display_text(value.get("recovery"), 64, redact=GUARD),
            "reason": display_text(value.get("reason"), 2000, redact=WORD),
            "warning": display_text(value.get("warning"), 2000, redact=WORD), "at": _num(value.get("at"))}


def preflight_view(value: Any) -> dict | None:
    if not isinstance(value, dict):
        return None
    problems = _list(value.get("problems"))
    shown = [display_text(v, 64, redact=WORD) for v in problems[:32] if isinstance(v, str)]
    if not problems and value.get("ok") is False:
        # GitHub member sign-in reports one code and its message, not Google's list (github_auth.py, #269).
        detail = value.get("message") or value.get("error")
        shown = [display_text(detail, 2000, redact=WORD)] if isinstance(detail, str) and detail else []
    return {"ok": _bool(value.get("ok")), "problems": shown, "checked_at": _num(value.get("checked_at"))}


def backend_view(value: Any) -> dict:
    value = _dict(value)
    models = _list(value.get("popular_models"))
    return {"name": display_text(value.get("name"), 80, redact=GUARD),
            "model": display_text(value.get("model"), 200, redact=GUARD),
            "effort": display_text(value.get("effort"), 32, redact=GUARD), "available": _bool(value.get("available")),
            "popular_models": [{"id": display_text(row.get("id"), 200, redact=GUARD),
                                "label": display_text(row.get("label"), 200, redact=GUARD)}
                               for row in models if isinstance(row, dict)]}


def memory_route_absent(body: Any) -> bool:
    """The daemon serves `/memory` only while its memory_library module is installed and switched on; otherwise the
    route is absent and the daemon answers its generic 404, which the page shows as a module state, not a fault."""
    return isinstance(body, dict) and body.get("detail") == "Not Found"


def control_view(operation: str, body: Any, installed: bool = True) -> Any:
    if operation == "backends":
        return [backend_view(row) for row in _list(body)]
    if operation == "backend_save":
        return backend_view(body)
    body = _dict(body)
    if operation in ("smart", "smart_save"):
        return {"mode": display_text(body.get("mode"), 32, redact=GUARD), "enabled": _bool(body.get("enabled")),
                "configured": _bool(body.get("configured")),
                "secret_ref": display_text(body.get("secret_ref"), 4000, redact=WORD)}
    if operation in ("memory", "memory_save"):
        commit = _dict(body.get("last_commit"))
        return {"installed": installed, "enabled": _bool(body.get("enabled")),
                "profile": body.get("profile") if isinstance(body.get("profile"), str) else "",
                "profile_max_chars": _num(body.get("profile_max_chars")),
                "last_commit": {"head": display_text(commit.get("head"), 64, redact=GUARD),
                                "path": display_text(commit.get("path"), 4000, redact=GUARD),
                                "summary": display_text(commit.get("summary"), 200, redact=GUARD),
                                "at": _num(commit.get("at")),
                                "pushed": _bool(commit.get("pushed"))},
                "refresh_error": display_text(body.get("refresh_error"), 2000, redact=WORD)}
    if operation in ("github", "github_save"):
        return {"enabled": _bool(body.get("enabled")), "configured": _bool(body.get("configured")),
                "preflight": preflight_view(body.get("preflight"))}
    if operation == "google":
        return {"enabled": _bool(body.get("enabled")), "ready": _bool(body.get("ready")),
                "preflight": preflight_view(body.get("preflight"))}
    return {"sent": _bool(body.get("sent"))}
