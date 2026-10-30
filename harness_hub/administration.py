"""Closed browser operation catalogue and request validation for owner administration."""

from __future__ import annotations

import math
import re
from typing import Any, NamedTuple


class Operation(NamedTuple):
    group: str
    label: str
    cli: str
    fields: dict[str, str]
    required: tuple[str, ...]
    confirmation: str


# name: group, label, CLI, input fields (type), required fields, confirmation text.
# Path targets are separate from daemon request bodies; no browser-supplied URL is accepted.
OPERATIONS: dict[str, Operation] = {
    "accounts": Operation("accounts", "List members", "harness accounts list", {}, (), ""),
    "account": Operation("accounts", "Member detail", "harness accounts show <user_id>", {}, (), ""),
    "account_add": Operation(
        "accounts",
        "Add member",
        "harness accounts add <login> <display_name>",
        {
            "login": "text",
            "display_name": "text",
            "disk_quota_bytes": "integer",
            "max_running": "integer",
            "max_queued": "integer",
        },
        ("login", "display_name"),
        "",
    ),
    "account_save": Operation(
        "accounts",
        "Update member",
        "harness accounts update <user_id>",
        {
            "login": "text",
            "display_name": "text",
            "enabled": "boolean",
            "disk_quota_bytes": "integer",
            "max_running": "integer",
            "max_queued": "integer",
        },
        (),
        "Disabling cancels this member's work. Rebinding invalidates their old login immediately. Confirm the changes.",
    ),
    "github_reset": Operation(
        "accounts",
        "Erase GitHub credential",
        "harness accounts github-reset <user_id> --confirm",
        {},
        (),
        "Erase this member's GitHub credential?",
    ),
    "google_invite": Operation(
        "accounts", "Create Google link code", "harness accounts google-invite <user_id>", {}, (), ""
    ),
    "google_cancel": Operation(
        "accounts",
        "Cancel Google link code",
        "harness accounts google-cancel-invite <user_id>",
        {},
        (),
        "Cancel this member's pending Google link code?",
    ),
    "google_revoke": Operation(
        "accounts",
        "Revoke Google sessions",
        "harness accounts google-revoke-sessions <user_id>",
        {},
        (),
        "End all Google Web sessions for this member?",
    ),
    "google_unlink": Operation(
        "accounts",
        "Unlink Google",
        "harness accounts google-unlink <user_id> --confirm",
        {},
        (),
        "Unlink Google and end this member's Google sessions?",
    ),
    "account_audit": Operation("accounts", "Household audit", "harness accounts audit", {"limit": "integer"}, (), ""),
    "credentials": Operation(
        "credentials", "List provider assignments", "harness provider-credentials list", {}, (), ""
    ),
    "credential_set": Operation(
        "credentials",
        "Assign provider credential name",
        "harness provider-credentials set <app_id> <backend>",
        {"app_id": "text", "backend": "text", "secret_ref": "name", "policy": "text", "models": "list"},
        ("app_id", "backend"),
        "Assign this credential policy to the App? Existing policy for this backend will be replaced.",
    ),
    "credential_revoke": Operation(
        "credentials",
        "Revoke provider assignment",
        "harness provider-credentials revoke <credential_id>",
        {},
        (),
        "Revoke this App's provider assignment?",
    ),
    "skills": Operation("skills", "Skills and proposals", "harness skills list", {}, (), ""),
    "skills_enabled": Operation("skills", "Enabled skills", "harness skills enabled", {}, (), ""),
    "proposal": Operation("skills", "Read proposal", "harness skills proposal <pid>", {}, (), ""),
    "skill_install": Operation(
        "skills",
        "Install reviewed proposal",
        "harness skills install <pid> <content_hash>",
        {"content_hash": "hash"},
        ("content_hash",),
        "Install the proposal at this exact reviewed hash?",
    ),
    "skill_reject": Operation("skills", "Reject proposal", "harness skills reject <pid>", {"reason": "text"}, (), ""),
    "skill_reopen": Operation("skills", "Reopen proposal", "harness skills reopen <pid>", {}, (), ""),
    "skill_review": Operation("skills", "Run hosted review", "harness skills review <pid>", {}, (), ""),
    "proposal_delete": Operation(
        "skills",
        "Delete proposal",
        "harness skills delete-proposal <pid>",
        {},
        (),
        "Permanently delete this proposal?",
    ),
    "skill_enable": Operation("skills", "Enable skill", "harness skills enable <slug>", {}, (), ""),
    "skill_disable": Operation("skills", "Disable skill", "harness skills disable <slug>", {}, (), ""),
    "skill_projects": Operation(
        "skills",
        "Set project allowlist",
        "harness skills projects <slug> <project>",
        {"projects": "list"},
        ("projects",),
        "Confirm the before and after project lists. Added projects will receive this skill automatically.",
    ),
    "skill_rollback": Operation(
        "skills",
        "Roll back skill",
        "harness skills rollback <slug>",
        {},
        (),
        "Roll back this skill to its previous version?",
    ),
    "skill_uninstall": Operation(
        "skills",
        "Uninstall skill",
        "harness skills uninstall <slug>",
        {},
        (),
        "Uninstall this skill?",
    ),
    "skill_export": Operation("skills", "Export skill", "harness skills export <slug>", {}, (), ""),
    "maintenance": Operation("maintenance", "Maintenance status", "harness maintenance status", {}, (), ""),
    "cleanup": Operation(
        "maintenance",
        "Clean up",
        "harness maintenance cleanup",
        {},
        (),
        "Cleanup can erase retained workspaces and sessions under daemon policy. Continue?",
    ),
    "backup": Operation("maintenance", "Back up", "harness maintenance backup", {}, (), ""),
    "retention_preview": Operation(
        "maintenance",
        "Preview image archive retention",
        "harness maintenance image-retention-preview",
        {},
        (),
        "",
    ),
    "retention_apply": Operation(
        "maintenance",
        "Apply image archive retention",
        "harness maintenance image-retention-apply <confirmation>",
        {"confirmation": "text"},
        ("confirmation",),
        "Permanently erase the images in the preview? Type the daemon's exact confirmation phrase.",
    ),
    "runners": Operation("runners", "List runners", "harness runner list", {}, (), ""),
    "runner_update": Operation(
        "runners",
        "Update runner",
        "harness runner update <name>",
        {},
        (),
        "Update this runner? It may restart.",
    ),
    "projects": Operation("runners", "List projects", "harness projects list", {}, (), ""),
    "project_add": Operation(
        "runners",
        "Add project",
        "harness projects create <name>",
        {"name": "text", "description": "text", "repo": "text", "target": "text"},
        ("name",),
        "",
    ),
    "remote": Operation("runners", "Remote Control folders", "harness remote-control list", {}, (), ""),
    "folder_trust": Operation(
        "runners",
        "Open folder trust prompt",
        "harness remote-control trust <project>",
        {},
        (),
        "Open the daemon host's interactive trust prompt for this folder? "
        "Approve there only after checking the folder.",
    ),
    "folder_forget": Operation(
        "runners",
        "Forget discovered folder",
        "harness remote-control forget <slug>",
        {},
        (),
        "Forget this discovered folder?",
    ),
    "scan_start": Operation("runners", "Scan for folders", "harness remote-control scan", {}, (), ""),
    "scan": Operation("runners", "Scan progress", "harness remote-control scan-show <scan_id>", {}, (), ""),
    "scan_cancel": Operation(
        "runners",
        "Cancel scan",
        "harness remote-control scan-cancel <scan_id>",
        {},
        (),
        "Cancel this discovery scan?",
    ),
    "scan_promote": Operation(
        "runners",
        "Add scanned folder",
        "harness remote-control promote <scan_id> <candidate_id> <slug> <confirmed_path> <marker>",
        {"slug": "name", "confirmed_path": "text", "confirmed_markers": "list"},
        ("slug", "confirmed_path", "confirmed_markers"),
        "Add this scanned folder with the exact path and markers you checked?",
    ),
    "audit": Operation(
        "audit",
        "Read retained audit",
        "harness audit list",
        {
            "limit": "integer",
            "before_id": "integer",
            "actor_id": "text",
            "key_id": "text",
            "target_id": "text",
            "action": "text",
            "outcome": "text",
            "since": "number",
            "until": "number",
        },
        (),
        "",
    ),
}

TARGET = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}")
CONTENT_HASH = re.compile(r"[a-fA-F0-9]{64}")
DAEMON_CONFIRM = {"github_reset", "google_unlink"}


def validate_fields(operation: str, body: Any) -> dict:
    spec = OPERATIONS[operation]
    fields, required, confirmation = spec.fields, spec.required, spec.confirmation
    if not isinstance(body, dict) or set(body) - (set(fields) | ({"confirm"} if confirmation else set())):
        raise ValueError("Unexpected administration fields")
    if confirmation and body.get("confirm") is not True:
        raise ValueError("Confirm this action in the page first")
    out = {key: value for key, value in body.items() if key != "confirm"}
    for key in required:
        if key not in out or out[key] is None or isinstance(out[key], str) and not out[key].strip():
            raise ValueError(f"{key} is required")
    if operation == "account_save" and not out:
        raise ValueError("Choose at least one account change")
    for key, value in out.items():
        kind = fields[key]
        valid = False
        if kind in ("text", "name", "hash"):
            valid = isinstance(value, str) and len(value) <= (32768 if key == "confirmed_path" else 4000)
            if kind == "name":
                valid = valid and (bool(TARGET.fullmatch(value)) or key == "secret_ref" and value == "")
            if kind == "hash":
                valid = valid and bool(CONTENT_HASH.fullmatch(value))
        elif kind == "boolean":
            valid = type(value) is bool
        elif kind == "integer":
            valid = type(value) is int and 1 <= value <= 2**53 - 1
        elif kind == "number":
            valid = type(value) in (int, float) and math.isfinite(value)
        elif kind == "list":
            valid = (
                isinstance(value, list)
                and len(value) <= 500
                and all(isinstance(item, str) and len(item) <= 4000 for item in value)
            )
        if not valid:
            raise ValueError(f"Invalid {key}")
    if operation == "credential_set" and out.get("policy", "api_key") not in (
        "api_key",
        "subscription",
        "subscription_then_api_key",
    ):
        raise ValueError("Invalid provider policy")
    if operation in DAEMON_CONFIRM:
        out["confirm"] = body["confirm"]
    return out
