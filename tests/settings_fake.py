"""Synthetic Settings fixture matching the pinned daemon's SettingsService and owner routes."""
from __future__ import annotations

from copy import deepcopy

from fastapi import Request
from fastapi.responses import JSONResponse


def register_settings(app, fake, owner):
    rows = [
        ("queue.limit", "int", 2, "live", None),
        ("listen.idle", "float", 1.5, "daemon_restart", None),
        ("notify.enabled", "bool", False, "live", None),
        ("smart_approvals.mode", "enum", "off", "live", ["off", "shadow", "auto"]),
        ("discovery.roots", "discovery_root_list", ["/synthetic/repos"], "live", None),
        ("labels.names", "string_list", ["sample"], "live", None),
        ("provider.secret_ref", "string", "/synthetic/token-file", "live", None),
        ("provider.credential", "string", None, "installer_only", None),
    ]
    fake.schema = [{"key": key, "label": key, "help": "Synthetic setting", "category": "Runtime",
                    "type": type_, "default": default, "scope": "admin", "apply": apply,
                    "sensitivity": "hidden" if apply == "installer_only" else
                                   "secret_ref" if key.endswith("secret_ref") else "public",
                    "readable": apply != "installer_only", "writable": apply != "installer_only",
                    "available": True, "file_only": apply == "installer_only", "enum": enum,
                    "minimum": 0 if type_ in ("int", "float") else None, "maximum": None}
                   for key, type_, default, apply, enum in rows]
    fake.values = {key: default for key, _, default, _, _ in rows}
    fake.revision = 3
    fake.pending_revision = None
    fake.pending = {}
    fake.previous = None
    fake.recovery = None
    fake.restart_polls = 0
    fake.restart_requested = False
    fake.settings_calls = []
    fake.mode = "off"
    fake.github_enabled = False
    fake.backend = {"name": "sample", "model": "sample-model", "effort": "medium", "available": True,
                    "popular_models": [{"id": "sample-model", "label": "Sample"}]}
    fake.profile = "Synthetic profile"

    def config():
        return {"revision": fake.revision, "pending_revision": fake.pending_revision, "confirmed": True,
                "supervised_restart": True, "restart_required": fake.pending_revision is not None,
                "recovery": fake.recovery, "warning": None,
                "settings": [{**spec, "effective": fake.values[spec["key"]], "configured": None,
                              "pending": fake.pending.get(spec["key"]),
                              "inherited": spec["default"], "source": "managed"}
                             for spec in fake.schema]}

    def error(status, code, detail, **extra):
        return JSONResponse({"detail": detail, "error": {"code": code, **extra}}, status_code=status)

    @app.api_route("/api/admin/v1/{path:path}", methods=["GET", "POST", "PUT", "PATCH"])
    async def settings(path: str, request: Request):
        if not owner(request):
            return JSONResponse({"detail": "owner key required"}, status_code=401)
        body = await request.json() if request.method != "GET" else None
        fake.settings_calls.append((request.method, path, deepcopy(body)))
        if path in ("config", "config/validate", "config/rollback", "config/restart") and body is not None and (
                body.get("revision") != fake.revision):
            return error(409, "revision_conflict", "configuration revision is stale",
                         details={"expected_revision": body.get("revision"), "current_revision": fake.revision})
        if path == "config/schema":
            return {"schema_version": 1, "supervised_restart": True, "settings": fake.schema,
                    "credential": "provider-secret-sentinel"}
        if path == "config" and request.method == "GET":
            return config()
        if path in ("config", "config/validate"):
            changes = body.get("changes", {})
            errors = {}
            for key, value in changes.items():
                spec = next((s for s in fake.schema if s["key"] == key), None)
                if not spec:
                    errors[key] = {"code": "unknown_key", "message": "unknown setting"}
                elif not spec["writable"]:
                    errors[key] = {"code": "installer_only", "message": "managed in local configuration"}
                elif value is not None and spec["type"] == "int" and (type(value) is not int or value < 0):
                    errors[key] = {"code": "invalid_value", "message": "must be a non-negative integer"}
            if errors:
                return error(400, "validation_error", "configuration is invalid", keys=errors)
            plan = {"revision": fake.revision, "target_revision": fake.revision + 1,
                    "restart_required": any(s["apply"] == "daemon_restart" and s["key"] in changes
                                            for s in fake.schema), "errors": {},
                    "changes": [{"key": k, "from": fake.values.get(k), "to": v, "apply": "live", "action": "set"}
                                for k, v in changes.items()]}
            if path == "config" and request.method == "PATCH":
                fake.previous = deepcopy(fake.values)
                for key, value in changes.items():
                    spec = next(s for s in fake.schema if s["key"] == key)
                    value = spec["default"] if value is None else value
                    if spec["apply"] == "daemon_restart":
                        fake.pending[key] = value
                    else:
                        fake.values[key] = value
                fake.revision += 1
                if fake.pending:
                    fake.pending_revision = fake.revision
                return {**plan, **config()}
            return plan
        if path == "config/rollback":
            if body.get("confirm") is not True:
                return error(400, "confirmation_required", "rollback requires confirm")
            if fake.previous is None:
                return error(409, "nothing_to_rollback", "no previous generation")
            fake.values, fake.previous = fake.previous, deepcopy(fake.values)
            fake.pending = {}
            fake.pending_revision = None
            fake.revision += 1
            return config()
        if path == "config/restart":
            if body.get("confirm") is not True:
                return error(400, "confirmation_required", "restart requires confirm")
            fake.restart_requested = True
            fake.restart_polls = 2
            return JSONResponse({"accepted": True, "target_revision": fake.pending_revision or fake.revision,
                                 "status": "restarting"}, status_code=202)
        if path == "smart-approvals":
            if body is not None:
                fake.mode = body["mode"]
            return {"mode": fake.mode, "enabled": True, "configured": True,
                    "secret_ref": "/synthetic/token-file", "recent": [{"credential": "provider-secret-sentinel"}]}
        if path == "backends":
            return [{**fake.backend, "provider_credential": "provider-secret-sentinel", "prefix": fake.hub_token[:12]}]
        if path == "backends/sample":
            fake.backend.update(body)
            return fake.backend
        if path == "memory":
            return {"enabled": True, "profile": fake.profile, "profile_max_chars": 4000,
                    "last_commit": {"head": "sample-commit", "path": "profile.md", "summary": "Synthetic",
                                    "at": 1, "credential": "provider-secret-sentinel"}, "debug": fake.hub_token}
        if path == "memory/profile":
            fake.profile = body["content"]
            return {"profile": fake.profile, "last_commit": {"head": "sample-new-commit", "path": "profile.md",
                                                             "summary": "Synthetic", "at": 2, "pushed": True}}
        if path == "github-member-auth":
            if body is not None:
                fake.github_enabled = body["enabled"]
            return {"configured": True, "enabled": fake.github_enabled, "preflight": None,
                    "members": [{"credential": "provider-secret-sentinel"}]}
        if path == "google-signin":
            return {"enabled": True, "ready": True, "client_secret": "provider-secret-sentinel",
                    "preflight": {"ok": True, "problems": [], "checked_at": 1, "debug": fake.hub_token}}
        if path == "notify/test":
            return {"sent": True, "provider_credential": "provider-secret-sentinel"}
        return error(404, "not_found", "unknown route")
