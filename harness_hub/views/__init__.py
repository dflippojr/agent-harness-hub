"""What the browser may see: every daemon answer is copied field by field into these shapes, never passed through."""

from __future__ import annotations

import hashlib
import json
import re
import time
import unicodedata
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any
from urllib.parse import urlsplit

from ..administration import CONTENT_HASH, TARGET
from ..guard import LINK_CODE
from .apps import (
    APP_STATES,
    SESSION_STATES,
    SPEND_NEAR,
    USAGE_FIELDS,
    app_view,
    apps_view,
    cap_use,
    erasures_view,
    limits_view,
    spend_view,
    usage_tally,
    valid_limits,
)
from .modules import (
    module_view,
)
from .overview import (
    MIN_OWNER_API,
    TAILNET_OWNER_API,
    _flags,
    _protocols,
    claim_view,
    health_view,
    overview,
    owner_api_compatibility,
    ownership_view,
)
from .settings import (
    backend_view,
    control_view,
    field_errors,
    memory_route_absent,
    preflight_view,
    recovery_view,
    registry_entry,
    registry_view,
    setting_value,
    settings_error,
)
from .text import (
    FIELD,
    GUARD,
    ID_SEGMENT,
    MESSAGES,
    REDACT_MODES,
    REDACTED,
    WORD,
    _app_id,
    _bool,
    _dict,
    _finite,
    _int,
    _list,
    _names,
    _num,
    _prefix,
    display_text,
)

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


# App pairing requests (#519). The Hub keeps its own copy of the elevated scopes so a daemon that forgets to flag one
# still cannot have it approved without the owner's separate acknowledgement.
PAIRING_STATES = ("pending", "armed", "claimed", "approved", "redeemed", "denied", "expired")
PAIRING_NEEDS = ("approve", "claim", "confirm", "redeem")
ELEVATED_SCOPES = frozenset({"sessions:all", "approvals", "remote_control", "memory_library", "homelab"})


def pairing_request_view(row: Any, secrets: tuple[str, ...] = ()) -> dict:
    row = _dict(row)
    kind = row.get("kind") if row.get("kind") in ("app", "hub") else "unknown"
    state = row.get("state") if row.get("state") in PAIRING_STATES else "unknown"
    scopes = [display_text(s, 64, secrets=secrets) for s in _list(row.get("scopes"))[:32] if isinstance(s, str)]
    disclosures = []
    for item in _list(row.get("disclosures"))[:32]:
        if isinstance(item, dict) and isinstance(item.get("scope"), str):
            disclosures.append({"scope": display_text(item["scope"], 64, secrets=secrets),
                                "tier": "elevated" if item.get("tier") == "elevated" else "standard",
                                "text": display_text(item.get("text"), 4000, secrets=secrets)})
    flagged = set(_names(row.get("elevated"))) | {d["scope"] for d in disclosures if d["tier"] == "elevated"}
    elevated = [s for s in scopes if s in ELEVATED_SCOPES or s in flagged or s == "admin"]
    for item in disclosures:
        if item["scope"] in elevated:
            item["tier"] = "elevated"
    # Owner decision: codes come only from the App's screen, never from an owner listing (#519 step 2).
    needs = row.get("needs") if row.get("needs") in PAIRING_NEEDS else ""
    return {"id": _app_id(row.get("id")), "kind": kind, "state": state, "needs": needs,
            "name": display_text(row.get("name"), 120, secrets=secrets),
            "catalog_app_id": display_text(row.get("catalog_app_id"), 200, secrets=secrets),
            "origin": display_text(row.get("origin"), 500, secrets=secrets),
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
    rows = [pairing_request_view(row, secrets) for row in _list(body) if isinstance(row, dict)]
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
    return [_app_id(v) for v in _list(value)[:64] if _app_id(v)]


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
        if (not text.strip() or display_text(text, 4000, secrets=secrets) != text or item["scope"] in by_scope
                or any(secret and secret in text for secret in secrets)):
            return None
        by_scope[item["scope"]] = {key: item[key] for key in ("scope", "tier", "text")}
    return [by_scope[scope] for scope in scopes] if set(by_scope) == set(scopes) else None


def entry_view(row: Any, secrets: tuple[str, ...] = (), guarded: tuple[str, ...] = ()) -> dict | None:
    """One entry as the daemon validated it, or None when it does not match the entries contract."""
    publisher_text = {"secrets": secrets, "guarded": guarded, "strip_controls": True}
    if not isinstance(row, dict) or row.get("verified") is not False or row.get("state") not in ENTRY_STATES:
        return None
    app = _dict(_dict(row.get("manifest")).get("app"))
    catalog_app_id = row.get("catalog_app_id")
    if not isinstance(catalog_app_id, str) or not CATALOG_APP_ID.fullmatch(catalog_app_id) or (
            app.get("app_id") != catalog_app_id) or not isinstance(app.get("display_name"), str):
        return None
    publisher = _dict(app.get("publisher"))
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
                       "justification": display_text(item.get("justification"), 1000, **publisher_text)})
    if not scopes:
        return None
    origins = _list(app.get("browser_origins"))
    # Binding fields must survive the display projection exactly: never arm a prefix or a redacted value.
    if len(origins) > 8 or any(not isinstance(origin, str) or len(origin) > 300
                               or display_text(origin, 300) != origin
                               or any(unicodedata.category(char) in {"Cc", "Cf"} for char in origin)
                               for origin in origins):
        return None
    try:
        for origin in origins:
            normalize_origin(origin)
    except ValueError:
        return None
    unverified = _dict(row.get("unverified_origin"))
    icon = app.get("icon")
    shown = {"catalog_app_id": catalog_app_id,
            "name": display_text(app.get("display_name"), 80, **publisher_text),
            "version": display_text(app.get("version"), 64, **publisher_text),
            "summary": display_text(app.get("summary"), 280, **publisher_text),
            "publisher": {"name": display_text(publisher.get("name"), 80, **publisher_text),
                          "id": display_text(publisher.get("id"), 64, **publisher_text)},
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
    rows = _list(_dict(body).get("entries"))
    shown = [entry_view(row, secrets) for row in rows]
    kept = [row for row in shown if row is not None]
    return kept, len(shown) - len(kept)


def marketplace_slots(requests: Any, secrets: tuple[str, ...] = ()) -> dict[str, list[dict]]:
    """Armed slots still in play, by catalog app id. App-initiated requests stay on the Pairing requests page."""
    slots: dict[str, list[dict]] = {}
    for row in _list(requests):
        if not isinstance(row, dict) or row.get("kind") != "app" or row.get("armed") is not True:
            continue
        shown = pairing_request_view(row, secrets)
        if shown["id"] and shown["state"] in SLOT_STATES and CATALOG_APP_ID.fullmatch(shown["catalog_app_id"]):
            slots.setdefault(shown["catalog_app_id"], []).append(shown)
    return slots

__all__ = [
    "MESSAGES",
    "REDACTED",
    "FIELD",
    "WORD",
    "GUARD",
    "REDACT_MODES",
    "_prefix",
    "display_text",
    "_bool",
    "_num",
    "_int",
    "_dict",
    "_list",
    "_flags",
    "_names",
    "_protocols",
    "claim_view",
    "health_view",
    "MIN_OWNER_API",
    "TAILNET_OWNER_API",
    "owner_api_compatibility",
    "ownership_view",
    "overview",
    "setting_value",
    "field_errors",
    "settings_error",
    "registry_entry",
    "registry_view",
    "recovery_view",
    "preflight_view",
    "backend_view",
    "memory_route_absent",
    "control_view",
    "module_view",
    "APP_STATES",
    "SESSION_STATES",
    "_finite",
    "ID_SEGMENT",
    "_app_id",
    "app_view",
    "apps_view",
    "erasures_view",
    "valid_limits",
    "limits_view",
    "USAGE_FIELDS",
    "SPEND_NEAR",
    "usage_tally",
    "cap_use",
    "spend_view",
    "TEXT",
    "NUMBER",
    "BOOL",
    "LONG",
    "IDENTIFIER",
    "HASH",
    "NAMES",
    "GOOGLE",
    "ACCOUNT",
    "CREDENTIAL",
    "FINDING",
    "REVIEW",
    "SKILL",
    "RUNNER_UPDATE",
    "RUNNER",
    "PROJECT",
    "FOLDER",
    "CANDIDATE",
    "SCAN",
    "AUDIT",
    "CLEANUP",
    "BACKUP",
    "RETENTION",
    "MAINTENANCE",
    "RESULT",
    "SCHEMAS",
    "project",
    "complete_skill_review",
    "administration_view",
    "PAIRING_STATES",
    "PAIRING_NEEDS",
    "ELEVATED_SCOPES",
    "pairing_request_view",
    "pairing_attention",
    "pairing_view",
    "SCOPE_DISCLOSURES",
    "CATALOG_APP_ID",
    "SCOPE_NAME",
    "INLINE_ICON",
    "ENTRY_STATES",
    "SLOT_STATES",
    "_key_ids",
    "normalize_origin",
    "entry_digest",
    "scope_disclosures",
    "entry_view",
    "entries_view",
    "marketplace_slots",
]
