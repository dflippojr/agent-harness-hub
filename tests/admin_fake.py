"""Synthetic fixtures following daemon admin.py and module handlers, not Hub schemas."""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path

from fastapi import Request
from fastapi.responses import JSONResponse, Response

ACCOUNT = {
    "user_id": "u-synthetic",
    "account_hint": "syntheti",
    "login": "member@example.test",
    "display_name": "Synthetic member",
    "enabled": True,
    "disk_quota_bytes": 1024,
    "disk_used_bytes": 128,
    "max_running": 1,
    "max_queued": 4,
    "running": 0,
    "queued": 0,
    "created_at": 1700000000,
    "updated_at": 1700000000,
    "last_activity_at": None,
    "google": {
        "linked": False,
        "email": "",
        "linked_at": None,
        "last_sign_in_at": None,
        "active_web_sessions": 0,
        "invitation_expires_at": None,
    },
}
SKILL = {
    "slug": "synthetic",
    "title": "Synthetic skill",
    "purpose": "Synthetic fixture",
    "version": 1,
    "content_hash": "a" * 64,
    "enabled": True,
    "projects": [],
    "installed_at": 1700000000,
}
PROPOSAL = {
    "id": "p-synthetic",
    "slug": "synthetic",
    "title": "Synthetic proposal",
    "purpose": "Fixture",
    "content_hash": "a" * 64,
    "status": "ready",
    "review_status": "reviewed",
    "review": None,
    "static_findings": [],
    "skill_md": "<script>alert('fixture')</script>",
    "references": [{"path": "notes.txt", "content": "Synthetic text"}],
    "examples": [],
    "manifest": {"credential": "provider-secret-sentinel"},
    "diff": "+Synthetic",
}
CREDENTIAL = {
    "id": "pc-synthetic",
    "app_id": "k-synthetic",
    "app_name": "Synthetic App",
    "backend": "claude",
    "secret_ref": "synthetic-name",
    "policy": "api_key",
    "models": [],
    "created_at": 1700000000,
    "revoked_at": None,
    "available": True,
}
AUDIT = {
    "id": 2,
    "actor_id": "k-synthetic",
    "actor_kind": "hub",
    "key_id": "k-synthetic",
    "source": "admin_api",
    "target_id": "u-synthetic",
    "target_kind": "account",
    "action": "create",
    "outcome": "ok",
    "at": 1700000000,
    "metadata": {"fields": ["max_running"], "new_max_running": 1, "credential": "provider-secret-sentinel"},
}
SCAN = {
    "id": "scan-synthetic",
    "revision": 1,
    "status": "done",
    "visited": 1,
    "errors": [],
    "truncated": False,
    "reason": "",
    "expires_in": 600,
    "candidates": [
        {
            "id": "c-synthetic",
            "path": "C:/synthetic/folder",
            "suggested_slug": "synthetic",
            "markers": [".git"],
            "git": True,
            "requires_git": True,
            "launchable": True,
            "reason": "",
            "configured_duplicate": False,
            "promoted": False,
            "trusted": False,
        }
    ],
}


def register_administration(app, fake, owner):
    fake.admin_calls = []
    fake.admin_error = None
    fake.admin_overrides = {}
    fake.accounts = {ACCOUNT["user_id"]: copy.deepcopy(ACCOUNT)}
    fake.proposal = copy.deepcopy(PROPOSAL)
    fake.skill = copy.deepcopy(SKILL)
    fake.credentials = [copy.deepcopy(CREDENTIAL)]
    fake.retention_phrase = "DELETE 2 ARCHIVED IMAGES synthetic"
    fake.invitation_code = "ahg-synthetic-link-code"

    def refusal(status, message):
        return JSONResponse({"detail": message}, status_code=status)

    async def action(request: Request):
        path = request.url.path.removeprefix("/api/admin/v1/")
        if not owner(request):
            return refusal(401, "invalid owner token")
        method = request.method
        body = await request.json() if method != "GET" else None
        fake.admin_calls.append((method, path, body, dict(request.query_params)))
        if fake.admin_error:
            status, reply = fake.admin_error
            return JSONResponse(reply, status_code=status)
        if path in fake.admin_overrides:
            return fake.admin_overrides[path]
        reply, status = None, 200
        if path == "accounts/audit":
            reply = [AUDIT]
        elif path == "accounts":
            if method == "GET":
                reply = list(fake.accounts.values())
            else:
                row = {**copy.deepcopy(ACCOUNT), **body, "user_id": "u-created"}
                if any(account["login"] == row["login"] for account in fake.accounts.values()):
                    return refusal(409, "login already a member")
                fake.accounts[row["user_id"]] = row
                reply, status = row, 201
        elif path.startswith("accounts/"):
            parts = path.split("/")
            row = fake.accounts.get(parts[1])
            if row is None:
                return refusal(404, "no household account matches that id")
            if len(parts) == 2:
                if method == "PATCH":
                    row.update(body)
                reply = row
            elif path.endswith("github-connection/reset"):
                if body.get("confirm") is not True:
                    return refusal(400, "confirm reset")
                reply = {"configured": True, "enabled": True, "token": "provider-secret-sentinel"}
            elif path.endswith("google/invitation") and method == "POST":
                if not row["enabled"]:
                    return refusal(409, "account disabled")
                row["google"]["invitation_expires_at"] = 4102444800
                reply, status = {"code": fake.invitation_code, "expires_at": 4102444800}, 201
            else:
                if path.endswith("google") and body.get("confirm") is not True:
                    return refusal(400, "confirm unlink")
                row["google"]["invitation_expires_at"] = None
                reply = {**row["google"], "revoked": 1, "code": fake.invitation_code, "sub": "private-subject"}
        elif path == "provider-credentials":
            if method == "GET":
                reply = fake.credentials
            else:
                reply, status = {**copy.deepcopy(CREDENTIAL), **body}, 201
                fake.credentials = [reply]
        elif path.startswith("provider-credentials/"):
            fake.credentials = []
            return Response(status_code=204)
        elif path == "skills":
            reply = {"enabled": True, "proposals": [fake.proposal], "installed": [fake.skill]}
        elif path == "skills/enabled":
            reply = [fake.skill] if fake.skill["enabled"] else []
        elif path.startswith("skills/proposals/"):
            if path.endswith("/install"):
                if body["content_hash"] != fake.proposal["content_hash"]:
                    return refusal(409, "proposal changed; review again")
                reply = fake.skill
            elif path.endswith("/review"):
                reply = {
                    "job_id": "j-synthetic",
                    "usage_notice": "Synthetic hosted quota notice",
                    "reviewer_model": "synthetic-model",
                }
            elif method == "DELETE":
                return Response(status_code=204)
            else:
                fake.proposal["status"] = "rejected" if path.endswith("/reject") else "ready"
                reply = fake.proposal
        elif path.startswith("skills/"):
            action = path.rsplit("/", 1)[-1]
            if action in ("enable", "disable"):
                fake.skill["enabled"] = action == "enable"
            if action == "projects":
                fake.skill["projects"] = body["projects"]
            reply = (
                {"ok": True} if action == "uninstall" else {**fake.skill, **(PROPOSAL if action == "export" else {})}
            )
        elif path == "maintenance":
            reply = {
                "free_gb": 10.0,
                "total_gb": 20.0,
                "quota_mb": 100,
                "workspaces_mb": 2,
                "checkpoints_mb": 0,
                "last_cleanup": None,
                "backup": {"enabled": False},
                "image_archive": {"enabled": False},
                "runners": [],
                "containers": [],
            }
        elif path == "maintenance/cleanup":
            reply = {
                "at": 1700000000,
                "containers_removed": 1,
                "workspaces_removed": 2,
                "orphans_removed": 0,
                "kept": 0,
                "apps_erased": 0,
                "sessions_expired": 0,
            }
        elif path == "maintenance/backup":
            reply = {
                "ok_at": 1700000000,
                "path": "/synthetic/backup",
                "bytes": 1024,
                "removed": [],
                "error": "",
                "app_stores": 1,
                "transcript_archives": 1,
                "warnings": [],
            }
        elif path.endswith("retention/preview"):
            reply = {"enabled": True, "keep_days": 30, "count": 2, "bytes": 1024, "confirmation": fake.retention_phrase}
        elif path.endswith("retention/apply"):
            if body["confirmation"] != fake.retention_phrase:
                return refusal(400, "retention preview missing or stale")
            reply = {"removed": 2, "bytes": 1024, "errors": []}
        elif path == "runners":
            reply = [
                {
                    "name": "synthetic",
                    "online": True,
                    "last_seen_seconds": 0,
                    "pending_requests": 0,
                    "info": {"version": "1", "protocol": 2, "token": "provider-secret-sentinel"},
                    "update_supported": True,
                    "manual_update": "harness update",
                    "last_update": None,
                }
            ]
        elif re.fullmatch(r"runners/[^/]+/update", path):
            reply = {"ok": True, "version": "2"}
        elif path == "projects":
            reply = (
                {
                    "name": body["name"],
                    "description": body.get("description", ""),
                    "repo": bool(body.get("repo")),
                    "homelab": False,
                    "target": "tower",
                    "managed": True,
                }
                if method == "POST"
                else []
            )
        elif path == "remote-control":
            reply = {
                "enabled": True,
                "projects": [
                    {
                        "project": "synthetic",
                        "path": "C:/synthetic/folder",
                        "managed": True,
                        "trusted": False,
                        "git": True,
                        "running": False,
                    }
                ],
                "discovery": {"supported": True, "enabled": True},
            }
        elif path.startswith("remote-control/discovery/scans"):
            reply = {"slug": body["slug"], "already_added": False} if path.endswith("promote") else SCAN
        elif path.endswith("/trust"):
            reply = {"pid": 123, "message": "Trust prompt opened on daemon host"}
        elif path.startswith("remote-control/folders/"):
            reply = {"removed": True}
        elif path == "audit":
            reply = {
                "items": [AUDIT] if "before_id" not in request.query_params else [],
                "next_before_id": 2 if "before_id" not in request.query_params else None,
            }
        else:
            return refusal(404, "unknown owner route")
        # Extra fields simulate private nested daemon metadata and must never survive the browser projection.
        if isinstance(reply, dict):
            reply = {**reply, "token": fake.hub_token, "credential": "provider-secret-sentinel"}
        return JSONResponse(reply, status_code=status)

    routes = json.loads((Path(__file__).parent / "fixtures" / "administration-routes.json").read_text())["routes"]
    for method, path in routes:
        app.add_api_route("/api/admin/v1" + path, action, methods=[method])
