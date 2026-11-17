"""Browser projection for the administration (/api/manage) pages: schemas and project()."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

from ..administration import CONTENT_HASH, TARGET
from ..guard import LINK_CODE
from .text import _bool, _finite, _int, display_text

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
FOLDER = {**dict.fromkeys(("project", "path", "status", "invalid", "reason"), TEXT),
          **dict.fromkeys(("running", "trusted", "git", "managed", "owner_only"), BOOL),
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
         "id": NUMBER, "ts": NUMBER, "detail": TEXT,
         "metadata": {"fields": NAMES, "reason": TEXT, "operation_id": TEXT, "category": TEXT,
                      **dict.fromkeys(("count", "removed", "kept", "expired", "new_max_running", "old_max_running",
                                      "new_max_queued", "old_max_queued", "new_disk_quota_bytes",
                                      "old_disk_quota_bytes"), NUMBER)}}
CLEANUP = {"at": NUMBER, "removed": NUMBER, "kept": NUMBER, "expired": NUMBER,
           "errors": NAMES, "warnings": NAMES,
           **dict.fromkeys(("containers_removed", "workspaces_removed", "orphans_removed",
                           "apps_erased", "sessions_expired"), NUMBER)}
BACKUP = {"enabled": BOOL, "ok_at": NUMBER, "path": TEXT, "bytes": NUMBER, "files": NUMBER,
          "app_stores": NUMBER, "removed": NAMES, "dir": TEXT,
          "transcript_archives": NUMBER, "warnings": NAMES, "error": TEXT}
RETENTION = {"enabled": BOOL, "keep_days": NUMBER, "count": NUMBER, "bytes": NUMBER,
             "confirmation": TEXT, "removed": NUMBER, "errors": NAMES}
MAINTENANCE = {**dict.fromkeys(("workspaces_mb", "checkpoints_mb", "free_gb", "total_gb", "quota_mb"), NUMBER),
               "last_cleanup": CLEANUP, "backup": BACKUP, "runners": [RUNNER],
               "containers": [{"name": TEXT, "state": TEXT, "size": TEXT}],
               "image_archive": {"enabled": BOOL, "bytes": NUMBER}}
RESULT = {"ok": BOOL, "removed": BOOL, "slug": TEXT, "already_added": BOOL, "message": TEXT,
          "version": TEXT, "updated": BOOL, "pid": NUMBER}
SCHEMAS = {
    "accounts": [ACCOUNT], "account": ACCOUNT, "account_add": ACCOUNT, "account_save": ACCOUNT,
    "github_reset": {"enabled": BOOL, "configured": BOOL},
    "google_invite": {"code": TEXT, "expires_at": NUMBER},
    "google_cancel": GOOGLE, "google_revoke": GOOGLE, "google_unlink": GOOGLE,
    "credentials": [CREDENTIAL], "credential_set": CREDENTIAL,
    "skills": {"enabled": BOOL, "proposals": [SKILL], "installed": [SKILL]}, "skills_enabled": [SKILL],
    **dict.fromkeys(("proposal", "skill_install", "skill_reject", "skill_reopen", "skill_enable", "skill_disable",
                     "skill_projects", "skill_rollback", "skill_export"), SKILL),
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
        shown = display_text(value, 262144 if schema == LONG else 32768, secrets=secrets)
        if schema in (IDENTIFIER, HASH) and not any(secret and secret in value for secret in secrets):
            pattern = TARGET if schema == IDENTIFIER else CONTENT_HASH
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
