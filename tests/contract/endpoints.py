"""The daemon contract: every daemon endpoint the Hub calls, what the Hub sends and what the daemon answers.

Keys are "METHOD /path" with the Hub's own path parameter names (harness_hub/daemon.py). Each endpoint lists the
request body the Hub sends (None: no body) and, per status code, the response shape. Every error status answers
ERROR. A field is *required* when the daemon always sends it and the Hub reads it, *optional* when the Hub reads it
but the daemon sends it only sometimes (a module, a state, a later release). Fields neither reads are left out: extra
fields are allowed. Types follow what the real daemon sends, which the contract suite checks against a disposable
daemon at the pinned commit and nightly at agent-harness main (tests/contract/README.md).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .shapes import ANY, BOOL, INT, NUM, STR, Enum, ListOf, MapOf, Obj, Shape

ADMIN = "/api/admin/v1"

# Every refusal: FastAPI's own (`{"detail": "Not Found"}`) or the daemon's, which adds a machine-readable code.
ERROR = Obj({"detail": STR}, optional={"error": Obj({"code": STR}, optional={
    "message": STR, "retryable": BOOL, "details": MapOf(ANY), "keys": MapOf(Obj({"code": STR, "message": STR}))})})
NO_BODY = None  # a 204: the Hub reads no body
EMPTY = Obj()  # a write with nothing to say: the Hub sends {}

# Health and discovery ------------------------------------------------------------------------------------------------
RECOVERY = Obj(optional={"recovery": STR, "reason": STR | None, "warning": STR | None, "at": NUM | None})
HEALTH = Obj({
    "ok": BOOL, "release": STR, "build_id": STR,
    "protocols": MapOf(Obj({"min": INT, "max": INT})),
    "capabilities": Obj({"profile": STR, "required": MapOf(BOOL), "modules": MapOf(BOOL),
                         "hosted_backends": ListOf(STR), "config_registry": BOOL}),
    "config": Obj({"revision": INT, "confirmed": BOOL, "supervised_restart": BOOL, "recovery": RECOVERY | STR | None}),
}, optional={"profile": STR})
DISCOVERY = Obj({"api_version": STR, "hub": Obj({"claimed": BOOL})}, optional={"allowed_logins": ListOf(STR)})
HUB_RECORD = Obj({"key_id": STR, "name": STR, "kind": STR, "claimed_at": NUM})
HUB_CLAIM = Obj({"claimed": BOOL, "hub": HUB_RECORD | None})

# Hub claim (the Hub's own pairing request) ---------------------------------------------------------------------------
CLAIM_REQUEST = Obj({"name": STR, "kind": Enum("hub"), "code_challenge": STR, "code_challenge_method": Enum("S256")})
CLAIM_STARTED = Obj({"id": STR, "state": Enum("pending"), "match_code": STR, "expires_at": NUM})
REDEEM_REQUEST = Obj({"code_verifier": STR})
REDEEMED = Obj({"token": STR, "app": Obj({"id": STR, "role": STR, "kind": STR, "name": STR})},
               optional={"api_version": STR})

# Apps ----------------------------------------------------------------------------------------------------------------
USAGE = Obj({"requests": NUM, "prompt_tokens": NUM, "completion_tokens": NUM, "cost_usd": NUM})
STORE = Obj(optional={"sessions": MapOf(INT) | None, "usage": USAGE | None, "errors": INT | None,
                      "last_error": STR | None, "last_error_at": NUM | None})
KEY = Obj({
    "id": STR, "name": STR, "kind": STR, "role": STR, "scopes": STR, "origins": ListOf(STR), "catalog_app_id": STR,
    "created_at": NUM, "last_used_at": NUM | None, "revoked_at": NUM | None, "erase_after": NUM | None,
    "erased_at": NUM | None, "retention_days": NUM | None,
}, optional={"store": STORE})
INVENTORY_APP = Obj({
    "id": STR, "name": STR, "kind": STR, "role": STR, "scopes": STR, "origins": ListOf(STR), "catalog_app_id": STR,
    "created_at": NUM, "last_used_at": NUM | None, "revoked_at": NUM | None, "erase_after": NUM | None,
    "erased_at": NUM | None, "state": Enum("active", "idle", "stale", "never_used", "revoked", "erasure_pending"),
    "token_age_seconds": NUM, "errors": BOOL,
}, optional={"store": STORE})
SCOPE = Obj({"scope": STR, "risk_tier": Enum("standard", "elevated")}, optional={"justification": STR})
ENTRY = Obj({
    "catalog_app_id": STR, "verified": BOOL, "state": Enum("paired", "not_paired"), "paired": ListOf(STR),
    "pending_pairing": BOOL, "unverified_origin": Obj({"paired": ListOf(STR), "pending_pairing": BOOL}),
    "manifest": Obj({"app": Obj({"app_id": STR, "display_name": STR, "scopes": ListOf(SCOPE)}, optional={
        "version": STR, "summary": STR, "browser_origins": ListOf(STR), "icon": STR,
        "publisher": Obj(optional={"id": STR, "name": STR})})}),
})
MODULE = Obj({"name": STR, "state": Enum("present", "switched_off", "absent")}, optional={"status": MapOf(ANY)})
INVENTORY = Obj({"modules": ListOf(MODULE), "apps": ListOf(INVENTORY_APP), "entries": ListOf(ENTRY)})
ERASURE = Obj({"id": STR, "name": STR, "kind": STR, "revoked_at": NUM, "erase_after": NUM})
LIMITS = Obj({"app_id": STR, "configured": Obj({"max_running": INT | None, "max_queued": INT | None}),
              "effective": Obj({"max_running": INT, "max_queued": INT}),
              "running": INT, "queued": INT, "parked": INT})
LIMITS_REQUEST = Obj(optional={"max_running": INT | None, "max_queued": INT | None})
RETENTION_REQUEST = Obj({"retention_days": NUM | None})

# Pairing requests ----------------------------------------------------------------------------------------------------
PAIRING_STATES = Enum("pending", "armed", "claimed", "approved", "redeemed", "denied", "expired")
PAIRING = Obj({
    "id": STR, "kind": Enum("app", "hub"), "name": STR, "scopes": ListOf(STR), "catalog_app_id": STR, "origin": STR,
    "browser": BOOL, "armed": BOOL, "state": PAIRING_STATES, "needs": Enum("", "approve", "claim", "confirm", "redeem"),
    "created_at": NUM, "expires_at": NUM, "approved_at": NUM | None, "finished_at": NUM | None, "key_id": STR,
    "elevated": ListOf(STR),
    "disclosures": ListOf(Obj({"scope": STR, "tier": Enum("standard", "elevated"), "text": STR})),
})
ARM_REQUEST = Obj({"catalog_app_id": STR, "scopes": ListOf(STR), "name": STR},
                  optional={"origin": STR, "acknowledge_elevated": Enum(True)})
APPROVE_REQUEST = Obj({"match": STR}, optional={"acknowledge_elevated": Enum(True)})
CONFIRM_REQUEST = Obj({"match": STR})

# Settings ------------------------------------------------------------------------------------------------------------
SPEC = Obj({
    "key": STR, "label": STR, "help": STR, "category": STR, "type": STR, "scope": STR, "apply": STR,
    "sensitivity": STR, "readable": BOOL, "writable": BOOL, "available": BOOL, "file_only": BOOL,
    "minimum": NUM | None, "maximum": NUM | None, "enum": ListOf(STR) | None, "modules": ListOf(STR), "default": ANY,
}, optional={"guidance": STR})
SETTING = SPEC.extend({"effective": ANY, "configured": ANY, "inherited": ANY, "pending": ANY, "source": STR})
SCHEMA = Obj({"schema_version": INT, "supervised_restart": BOOL, "settings": ListOf(SPEC)})
CONFIG = Obj({"revision": INT, "pending_revision": INT | None, "confirmed": BOOL, "supervised_restart": BOOL,
              "restart_required": BOOL, "recovery": RECOVERY | STR | None, "warning": STR | None,
              "settings": ListOf(SETTING)})
CHANGE = Obj({"key": STR, "from": ANY, "to": ANY, "apply": STR, "action": STR})
PLAN = Obj({"revision": INT, "target_revision": INT, "restart_required": BOOL, "changes": ListOf(CHANGE),
            "errors": MapOf(Obj({"code": STR, "message": STR}))})
CHANGES_REQUEST = Obj({"revision": INT, "changes": MapOf(ANY)})
CONFIRMED_REQUEST = Obj({"revision": INT, "confirm": Enum(True)})
RESTARTING = Obj({"accepted": BOOL, "target_revision": INT, "status": STR})
SMART = Obj({"mode": STR, "enabled": BOOL, "configured": BOOL, "secret_ref": STR})
BACKEND = Obj({"name": STR, "model": STR, "effort": STR}, optional={
    "available": BOOL, "popular_models": ListOf(Obj({"id": STR, "label": STR})), "billing": STR,
    "today": MapOf(NUM), "week": MapOf(NUM)})
COMMIT = Obj(optional={"head": STR, "path": STR, "summary": STR, "at": NUM, "pushed": BOOL})
MEMORY = Obj({"enabled": BOOL, "profile": STR, "profile_max_chars": INT},
             optional={"last_commit": COMMIT | None, "refresh_error": STR | None})
PREFLIGHT = Obj({"ok": BOOL | None, "problems": ListOf(STR), "checked_at": NUM | None})
GITHUB = Obj({"configured": BOOL, "enabled": BOOL, "preflight": PREFLIGHT | None})
GOOGLE_SIGNIN = Obj({"enabled": BOOL, "ready": BOOL, "preflight": PREFLIGHT | None})

# Owner administration ------------------------------------------------------------------------------------------------
GOOGLE = Obj({"linked": BOOL, "email": STR, "linked_at": NUM | None, "last_sign_in_at": NUM | None,
              "active_web_sessions": INT, "invitation_expires_at": NUM | None})
ACCOUNT = Obj({
    "user_id": STR, "account_hint": STR, "login": STR, "display_name": STR, "enabled": BOOL,
    "disk_quota_bytes": INT, "disk_used_bytes": INT, "max_running": INT, "max_queued": INT, "running": INT,
    "queued": INT, "last_activity_at": NUM | None, "created_at": NUM, "updated_at": NUM, "google": GOOGLE,
})
ACCOUNT_ADD = Obj({"login": STR, "display_name": STR},
                  optional={"disk_quota_bytes": INT, "max_running": INT, "max_queued": INT})
ACCOUNT_SAVE = Obj(optional={"login": STR, "display_name": STR, "enabled": BOOL, "disk_quota_bytes": INT,
                             "max_running": INT, "max_queued": INT})
CONFIRM = Obj({"confirm": Enum(True)})
# The household audit rows (#88: the time is `ts`, epoch seconds).
ACCOUNT_AUDIT = Obj({"id": INT, "ts": NUM, "actor_id": STR, "target_id": STR, "action": STR, "outcome": STR,
                     "detail": STR})
AUDIT = ACCOUNT_AUDIT.extend({"actor_kind": STR, "key_id": STR, "source": STR, "target_kind": STR,
                              "metadata": MapOf(ANY)})
CREDENTIAL = Obj({"id": STR, "app_id": STR, "backend": STR, "secret_ref": STR, "policy": STR, "models": ListOf(STR),
                  "created_at": NUM, "revoked_at": NUM | None},
                 optional={"app_name": STR, "available": BOOL})
CREDENTIAL_SET = Obj({"app_id": STR, "backend": STR},
                     optional={"secret_ref": STR, "policy": STR, "models": ListOf(STR)})
SKILL = Obj({"slug": STR, "title": STR, "purpose": STR, "version": INT, "content_hash": STR, "enabled": BOOL,
             "projects": ListOf(STR)}, optional={"installed_at": NUM})
FINDING = Obj({"code": STR, "message": STR}, optional={"path": STR, "severity": STR})
PROPOSAL = Obj({
    "id": STR, "slug": STR, "title": STR, "purpose": STR, "content_hash": STR, "status": STR,
}, optional={
    "activation_suggestion": STR, "review_status": STR, "review": MapOf(ANY) | None, "static_findings": ListOf(FINDING),
    "skill_md": STR, "references": ListOf(Obj({"path": STR, "content": STR})),
    "examples": ListOf(Obj({"prompt": STR}, optional={"expected": STR, "expected_behavior": STR})),
    "source_session_id": STR, "diff": STR})
SKILLS = Obj({"enabled": BOOL, "proposals": ListOf(PROPOSAL), "installed": ListOf(SKILL)})
REVIEW_STARTED = Obj({"job_id": STR, "usage_notice": STR, "reviewer_model": STR})
CLEANUP = Obj({"at": NUM}, optional={
    "containers_removed": INT, "workspaces_removed": INT, "orphans_removed": INT, "kept": INT, "apps_erased": INT,
    "sessions_expired": INT, "removed": INT, "expired": INT, "errors": ListOf(STR), "warnings": ListOf(STR)})
BACKUP = Obj({"ok_at": NUM | None, "path": STR, "bytes": INT, "error": STR}, optional={
    "removed": ListOf(STR), "app_stores": INT, "transcript_archives": INT, "warnings": ListOf(STR), "files": INT})
RUNNER_UPDATE = Obj({"ok": BOOL}, optional={"version": STR, "build_id": STR, "at": NUM, "message": STR,
                                            "plist_changed": BOOL})
RUNNER = Obj({"name": STR, "online": BOOL, "last_seen_seconds": NUM | None, "pending_requests": INT},
             optional={"info": MapOf(ANY), "update_supported": BOOL, "manual_update": STR,
                       "last_update": RUNNER_UPDATE | None, "compatibility": MapOf(ANY)})
MAINTENANCE = Obj({
    "workspaces_mb": NUM, "checkpoints_mb": NUM, "free_gb": NUM, "total_gb": NUM, "quota_mb": NUM,
    "containers": ListOf(Obj(optional={"name": STR, "state": STR, "size": STR})), "runners": ListOf(RUNNER),
    "backup": Obj({"enabled": BOOL}), "image_archive": Obj({"enabled": BOOL}),
}, optional={"last_cleanup": MapOf(ANY) | None})
RETENTION_PREVIEW = Obj({"enabled": BOOL, "keep_days": NUM, "count": INT, "bytes": INT, "confirmation": STR})
RETENTION_APPLIED = Obj({"removed": INT, "bytes": INT, "errors": ListOf(STR)})
PROJECT = Obj({"name": STR, "description": STR, "repo": BOOL, "homelab": BOOL, "target": STR, "managed": BOOL})
PROJECT_ADD = Obj({"name": STR}, optional={"description": STR, "repo": STR, "target": STR})
FOLDER = Obj({"project": STR, "path": STR}, optional={
    "managed": BOOL, "trusted": BOOL, "git": BOOL, "running": BOOL, "status": STR, "reason": STR})
REMOTE = Obj({"enabled": BOOL, "projects": ListOf(FOLDER)},
             optional={"discovery": Obj({"supported": BOOL, "enabled": BOOL})})
CANDIDATE = Obj({"id": STR, "path": STR, "suggested_slug": STR, "markers": ListOf(STR)}, optional={
    "git": BOOL, "requires_git": BOOL, "launchable": BOOL, "reason": STR, "configured_duplicate": BOOL,
    "promoted": BOOL, "trusted": BOOL})
SCAN = Obj({"id": STR, "revision": INT, "status": STR, "visited": INT, "candidates": ListOf(CANDIDATE)},
           optional={"errors": ListOf(MapOf(ANY)), "truncated": BOOL, "reason": STR, "expires_in": NUM})
PROMOTE_REQUEST = Obj({"slug": STR, "confirmed_path": STR, "confirmed_markers": ListOf(STR)})


@dataclass(frozen=True)
class Endpoint:
    request: Shape | None
    responses: dict[int, Shape | None]
    params: Shape | None = None  # the query string, for the reads that take one
    notes: str = ""
    # Statuses any owner route answers without a case of its own: a missing or rejected key.
    common: tuple[int, ...] = field(default=(401,))


def _admin(responses: dict[int, Shape | None], request: Shape | None = None, **kw) -> Endpoint:
    return Endpoint(request, responses, **kw)


# Module routes the daemon serves only while their module runs answer FastAPI's 404 (GET) or 405 (writes, which fall
# through to a read-only route) while it is absent or switched off.
ABSENT = {404: ERROR, 405: ERROR}

ENDPOINTS: dict[str, Endpoint] = {
    # Public, and the Hub's own claim -----------------------------------------------------------------------------
    "GET /health": Endpoint(None, {200: HEALTH, 503: ERROR}, common=()),
    "POST /api/v1/pair/requests": Endpoint(CLAIM_REQUEST, {201: CLAIM_STARTED, 409: ERROR, 429: ERROR}, common=()),
    "POST /api/v1/pair/requests/{request_id}/token": Endpoint(
        REDEEM_REQUEST, {201: REDEEMED, 400: ERROR, 403: ERROR, 404: ERROR, 409: ERROR}, common=()),
    f"GET {ADMIN}": Endpoint(None, {200: DISCOVERY}),
    f"GET {ADMIN}/hub-claim": Endpoint(None, {200: HUB_CLAIM}),
    f"GET {ADMIN}/hub": Endpoint(None, {200: INVENTORY, 400: ERROR, 404: ERROR}),
    # Apps (daemon.APPS_ROUTES) -----------------------------------------------------------------------------------
    f"GET {ADMIN}/keys": Endpoint(None, {200: ListOf(KEY)}),
    f"DELETE {ADMIN}/keys/{{app_id}}": Endpoint(EMPTY, {204: NO_BODY, 404: ERROR, 409: ERROR}),
    f"GET {ADMIN}/apps/erasures": Endpoint(None, {200: ListOf(ERASURE)}),
    f"GET {ADMIN}/apps/{{app_id}}/limits": Endpoint(None, {200: LIMITS, 404: ERROR}),
    f"PUT {ADMIN}/apps/{{app_id}}/limits": Endpoint(LIMITS_REQUEST, {200: LIMITS, 400: ERROR, 404: ERROR}),
    f"PUT {ADMIN}/apps/{{app_id}}/retention": Endpoint(RETENTION_REQUEST, {200: KEY, 400: ERROR, 404: ERROR}),
    # Pairing requests (daemon.PAIRING_ROUTES) --------------------------------------------------------------------
    f"GET {ADMIN}/pairing-requests": Endpoint(None, {200: ListOf(PAIRING)}),
    f"POST {ADMIN}/pairing-requests": Endpoint(ARM_REQUEST, {201: PAIRING, 400: ERROR}),
    f"POST {ADMIN}/pairing-requests/{{rid}}/approve": Endpoint(
        APPROVE_REQUEST, {200: PAIRING, 400: ERROR, 403: ERROR, 404: ERROR, 409: ERROR}),
    f"POST {ADMIN}/pairing-requests/{{rid}}/confirm": Endpoint(
        CONFIRM_REQUEST, {200: PAIRING, 400: ERROR, 404: ERROR, 409: ERROR}),
    f"POST {ADMIN}/pairing-requests/{{rid}}/deny": Endpoint(
        None, {200: PAIRING, 403: ERROR, 404: ERROR, 409: ERROR}),
    # Settings (daemon.SETTINGS_ROUTES) ---------------------------------------------------------------------------
    f"GET {ADMIN}/config/schema": Endpoint(None, {200: SCHEMA}),
    f"GET {ADMIN}/config": Endpoint(None, {200: CONFIG}),
    f"POST {ADMIN}/config/validate": Endpoint(CHANGES_REQUEST, {200: PLAN, 400: ERROR, 409: ERROR}),
    f"PATCH {ADMIN}/config": Endpoint(CHANGES_REQUEST, {200: CONFIG.extend({"target_revision": INT}), 400: ERROR,
                                                        409: ERROR}),
    f"POST {ADMIN}/config/rollback": Endpoint(CONFIRMED_REQUEST, {200: CONFIG, 400: ERROR, 409: ERROR}),
    f"POST {ADMIN}/config/restart": Endpoint(CONFIRMED_REQUEST, {202: RESTARTING, 400: ERROR, 409: ERROR}),
    f"GET {ADMIN}/smart-approvals": Endpoint(None, {200: SMART}),
    f"PUT {ADMIN}/smart-approvals": Endpoint(Obj({"mode": STR}), {200: SMART, 400: ERROR}),
    f"GET {ADMIN}/backends": Endpoint(None, {200: ListOf(BACKEND)}),
    f"PUT {ADMIN}/backends/{{name}}": Endpoint(Obj(optional={"model": STR, "effort": STR}),
                                              {200: BACKEND, 400: ERROR, 404: ERROR}),
    f"GET {ADMIN}/memory": Endpoint(None, {200: MEMORY, **ABSENT}),
    f"PUT {ADMIN}/memory/profile": Endpoint(Obj({"content": STR}),
                                            {200: Obj({"profile": STR}, optional={"last_commit": COMMIT | None}),
                                             400: ERROR, **ABSENT}),
    f"GET {ADMIN}/github-member-auth": Endpoint(None, {200: GITHUB}),
    f"PUT {ADMIN}/github-member-auth": Endpoint(Obj({"enabled": BOOL}), {200: GITHUB, 400: ERROR, 409: ERROR}),
    f"GET {ADMIN}/google-signin": Endpoint(None, {200: GOOGLE_SIGNIN}),
    f"POST {ADMIN}/notify/test": Endpoint(EMPTY, {200: Obj({"sent": BOOL}), 400: ERROR, 502: ERROR, **ABSENT}),
    # Owner administration (daemon.ADMIN_ROUTES) ------------------------------------------------------------------
    f"GET {ADMIN}/accounts": _admin({200: ListOf(ACCOUNT)}),
    f"POST {ADMIN}/accounts": _admin({201: ACCOUNT, 400: ERROR, 409: ERROR}, ACCOUNT_ADD),
    f"GET {ADMIN}/accounts/audit": _admin({200: ListOf(ACCOUNT_AUDIT)}, params=Obj(optional={"limit": INT})),
    f"GET {ADMIN}/accounts/{{user_id}}": _admin({200: ACCOUNT, 404: ERROR}),
    f"PATCH {ADMIN}/accounts/{{user_id}}": _admin({200: ACCOUNT, 400: ERROR, 404: ERROR, 409: ERROR}, ACCOUNT_SAVE),
    f"POST {ADMIN}/accounts/{{user_id}}/github-connection/reset": _admin(
        {200: Obj({"configured": BOOL, "enabled": BOOL}), 400: ERROR, 404: ERROR, 409: ERROR}, CONFIRM),
    f"POST {ADMIN}/accounts/{{user_id}}/google/invitation": _admin(
        {201: Obj({"code": STR, "expires_at": NUM}), 404: ERROR, 409: ERROR}, EMPTY),
    f"DELETE {ADMIN}/accounts/{{user_id}}/google/invitation": _admin({200: GOOGLE, 404: ERROR}, EMPTY),
    f"POST {ADMIN}/accounts/{{user_id}}/google/revoke-sessions": _admin(
        {200: GOOGLE.extend({"revoked": INT}), 404: ERROR}, EMPTY),
    f"DELETE {ADMIN}/accounts/{{user_id}}/google": _admin({200: GOOGLE, 400: ERROR, 404: ERROR}, CONFIRM),
    f"GET {ADMIN}/provider-credentials": _admin({200: ListOf(CREDENTIAL)}),
    f"POST {ADMIN}/provider-credentials": _admin({201: CREDENTIAL, 400: ERROR, 404: ERROR}, CREDENTIAL_SET),
    f"DELETE {ADMIN}/provider-credentials/{{credential_id}}": _admin({204: NO_BODY, 404: ERROR}, EMPTY),
    f"GET {ADMIN}/skills": _admin({200: SKILLS, 400: ERROR}),
    f"GET {ADMIN}/skills/enabled": _admin({200: ListOf(SKILL), 400: ERROR}),
    f"GET {ADMIN}/skills/proposals/{{pid}}": _admin({200: PROPOSAL, 400: ERROR, 404: ERROR}),
    f"POST {ADMIN}/skills/proposals/{{pid}}/install": _admin({200: SKILL, 400: ERROR, 404: ERROR, 409: ERROR},
                                                             Obj({"content_hash": STR})),
    f"POST {ADMIN}/skills/proposals/{{pid}}/reject": _admin({200: PROPOSAL, 400: ERROR, 404: ERROR, 409: ERROR},
                                                            Obj(optional={"reason": STR})),
    f"POST {ADMIN}/skills/proposals/{{pid}}/reopen": _admin({200: PROPOSAL, 400: ERROR, 404: ERROR, 409: ERROR},
                                                            EMPTY),
    f"POST {ADMIN}/skills/proposals/{{pid}}/review": _admin({200: REVIEW_STARTED, 400: ERROR, 404: ERROR, 409: ERROR},
                                                            EMPTY),
    f"DELETE {ADMIN}/skills/proposals/{{pid}}": _admin({204: NO_BODY, 400: ERROR, 404: ERROR, 409: ERROR}, EMPTY),
    f"POST {ADMIN}/skills/{{slug}}/enable": _admin({200: SKILL, 400: ERROR, 404: ERROR}, EMPTY),
    f"POST {ADMIN}/skills/{{slug}}/disable": _admin({200: SKILL, 400: ERROR, 404: ERROR}, EMPTY),
    f"PUT {ADMIN}/skills/{{slug}}/projects": _admin({200: SKILL, 400: ERROR, 404: ERROR},
                                                    Obj({"projects": ListOf(STR)})),
    f"POST {ADMIN}/skills/{{slug}}/rollback": _admin({200: SKILL, 400: ERROR, 404: ERROR, 409: ERROR}, EMPTY),
    f"POST {ADMIN}/skills/{{slug}}/uninstall": _admin({200: Obj({"ok": BOOL}), 400: ERROR, 404: ERROR}, EMPTY),
    f"GET {ADMIN}/skills/{{slug}}/export": _admin({200: SKILL, 400: ERROR, 404: ERROR}),
    f"GET {ADMIN}/maintenance": _admin({200: MAINTENANCE}),
    f"POST {ADMIN}/maintenance/cleanup": _admin({200: CLEANUP, 409: ERROR}, EMPTY),
    f"POST {ADMIN}/maintenance/backup": _admin({200: BACKUP, 409: ERROR, 500: ERROR, **ABSENT}, EMPTY),
    f"POST {ADMIN}/maintenance/image-archive/retention/preview": _admin({200: RETENTION_PREVIEW, 400: ERROR,
                                                                         **ABSENT}, EMPTY),
    f"POST {ADMIN}/maintenance/image-archive/retention/apply": _admin({200: RETENTION_APPLIED, 400: ERROR,
                                                                       409: ERROR, **ABSENT},
                                                                      Obj({"confirmation": STR})),
    f"GET {ADMIN}/runners": _admin({200: ListOf(RUNNER)}),
    f"POST {ADMIN}/runners/{{name}}/update": _admin({200: RUNNER_UPDATE, 404: ERROR, 409: ERROR, 502: ERROR}, EMPTY),
    f"GET {ADMIN}/projects": _admin({200: ListOf(PROJECT)}),
    f"POST {ADMIN}/projects": _admin({201: PROJECT, 400: ERROR, 409: ERROR}, PROJECT_ADD),
    f"GET {ADMIN}/remote-control": _admin({200: REMOTE, **ABSENT}),
    f"POST {ADMIN}/remote-control/{{project}}/trust": _admin(
        {200: Obj({"message": STR}, optional={"pid": INT}), 400: ERROR, 409: ERROR, **ABSENT}, EMPTY),
    f"DELETE {ADMIN}/remote-control/folders/{{slug}}": _admin({200: Obj({"removed": BOOL}), **ABSENT}, EMPTY),
    f"POST {ADMIN}/remote-control/discovery/scans": _admin({200: SCAN, 201: SCAN, 409: ERROR, **ABSENT}, EMPTY),
    f"GET {ADMIN}/remote-control/discovery/scans/{{scan_id}}": _admin({200: SCAN, **ABSENT}),
    f"DELETE {ADMIN}/remote-control/discovery/scans/{{scan_id}}": _admin({200: SCAN, **ABSENT}, EMPTY),
    f"POST {ADMIN}/remote-control/discovery/scans/{{scan_id}}/candidates/{{candidate_id}}/promote": _admin(
        {200: Obj({"slug": STR, "already_added": BOOL}), 400: ERROR, 409: ERROR, **ABSENT}, PROMOTE_REQUEST),
    f"GET {ADMIN}/audit": _admin({200: Obj({"items": ListOf(AUDIT), "next_before_id": INT | None})},
                                 params=Obj(optional={"limit": INT, "before_id": INT, "actor_id": STR, "key_id": STR,
                                                      "target_id": STR, "action": STR, "outcome": STR,
                                                      "since": NUM, "until": NUM})),
}

# Fake routes that are not part of the contract because the Hub must never call them: each one fails the test that
# reaches it. The meta-test accepts these and nothing else without a contract entry.
TRIPWIRES = {
    f"POST {ADMIN}/apps/{{}}/restore": "An App's token is restored only on the daemon host; the Hub never asks for it.",
    "POST /api/v1/pair/requests/{}/claim": "Only the App claims its armed slot, with its own PKCE challenge.",
}
