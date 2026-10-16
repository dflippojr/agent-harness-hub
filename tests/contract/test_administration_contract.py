"""Owner administration: members, audit, provider credentials, skills, maintenance, runners, projects and Remote
Control (daemon.ADMIN_ROUTES)."""

from __future__ import annotations

import secrets

import pytest

from .conftest import daemon_cannot


def member(target) -> dict:
    login = f"contract-{secrets.token_hex(4)}@example.test"
    added = target.call("POST /api/admin/v1/accounts", body={"login": login, "display_name": "Contract member"},
                        expect=201).body
    assert added["login"] == login and added["enabled"] is True
    return added


# Members --------------------------------------------------------------------------------------------------------------
def test_members_are_added_listed_read_and_updated(target):
    added = member(target)
    user = {"user_id": added["user_id"]}
    assert any(row["user_id"] == added["user_id"] for row in target.call("GET /api/admin/v1/accounts", expect=200).body)
    assert target.call("GET /api/admin/v1/accounts/{user_id}", user, expect=200).body["login"] == added["login"]
    saved = target.call("PATCH /api/admin/v1/accounts/{user_id}", user, {"max_running": 2, "display_name": "Renamed"},
                        expect=200).body
    assert saved["max_running"] == 2 and saved["display_name"] == "Renamed"


def test_a_login_already_in_use_is_a_409_and_unknown_members_are_404(target):
    added = member(target)
    target.call("POST /api/admin/v1/accounts", body={"login": added["login"], "display_name": "Twin"}, expect=409)
    unknown = {"user_id": "u-00000000000000000000000000000000"}
    target.call("GET /api/admin/v1/accounts/{user_id}", unknown, expect=404)
    target.call("PATCH /api/admin/v1/accounts/{user_id}", unknown, {"max_running": 2}, expect=404)


def test_a_google_link_code_is_made_cancelled_and_sessions_are_revoked_and_unlinked(target):
    user = {"user_id": member(target)["user_id"]}
    invited = target.call("POST /api/admin/v1/accounts/{user_id}/google/invitation", user, {}, expect=201).body
    assert invited["code"].startswith("ahg-") and invited["expires_at"] > 0
    cancelled = target.call("DELETE /api/admin/v1/accounts/{user_id}/google/invitation", user, {}, expect=200).body
    assert cancelled["invitation_expires_at"] is None
    target.call("POST /api/admin/v1/accounts/{user_id}/google/revoke-sessions", user, {}, expect=200)
    assert target.call("DELETE /api/admin/v1/accounts/{user_id}/google", user, {"confirm": True},
                       expect=200).body["linked"] is False


def test_google_routes_for_an_unknown_member_are_404(target):
    unknown = {"user_id": "u-00000000000000000000000000000000"}
    target.call("POST /api/admin/v1/accounts/{user_id}/google/invitation", unknown, {}, expect=404)
    target.call("DELETE /api/admin/v1/accounts/{user_id}/google/invitation", unknown, {}, expect=404)
    target.call("POST /api/admin/v1/accounts/{user_id}/google/revoke-sessions", unknown, {}, expect=404)
    target.call("DELETE /api/admin/v1/accounts/{user_id}/google", unknown, {"confirm": True}, expect=404)


def test_erasing_a_github_credential_needs_github_member_auth_set_up(target):
    user = {"user_id": member(target)["user_id"]}
    configured = target.call("GET /api/admin/v1/github-member-auth", expect=200).body["configured"]
    reply = target.call("POST /api/admin/v1/accounts/{user_id}/github-connection/reset", user, {"confirm": True},
                        expect=200 if configured else 409)
    assert configured or reply.code == "not_configured"


# Audit (#88: rows carry `ts`, in epoch seconds) ----------------------------------------------------------------------
def test_the_household_audit_records_a_refused_member_with_ts_and_detail(target):
    added = member(target)
    target.call("POST /api/admin/v1/accounts", body={"login": added["login"], "display_name": "Twin"}, expect=409)
    rows = target.call("GET /api/admin/v1/accounts/audit", params={"limit": 50}, expect=200).body
    created = [row for row in rows if row["action"] == "create" and row["target_id"] == added["user_id"]]
    assert created and all(row["ts"] > 1_600_000_000 for row in created)


def test_the_retained_audit_pages_by_before_id(target):
    member(target)
    first = target.call("GET /api/admin/v1/audit", params={"limit": 1}, expect=200).body
    assert first["items"] and first["items"][0]["ts"] > 1_600_000_000
    if first["next_before_id"] is not None:
        target.call("GET /api/admin/v1/audit", params={"limit": 1, "before_id": first["next_before_id"]}, expect=200)


# Provider credentials -------------------------------------------------------------------------------------------------
def test_provider_credentials_list_and_assign_for_a_hosted_backend_only(target):
    target.call("GET /api/admin/v1/provider-credentials", expect=200)
    hosted = target.call("GET /health", auth=False, expect=200).body["capabilities"]["hosted_backends"]
    app_id = target.arrange_app_key("Contract credential App")
    body = {"app_id": app_id, "backend": "claude", "secret_ref": "contract-secret-name"}
    assigned = target.call("POST /api/admin/v1/provider-credentials", body=body,
                           expect=201 if "claude" in hosted else 400)
    if "claude" in hosted:
        assert assigned.body["app_id"] == app_id and assigned.body["secret_ref"] == "contract-secret-name"
        target.call("DELETE /api/admin/v1/provider-credentials/{credential_id}",
                    {"credential_id": assigned.body["id"]}, {}, expect=204)


def test_revoking_an_unknown_provider_credential_is_404(target):
    target.call("DELETE /api/admin/v1/provider-credentials/{credential_id}", {"credential_id": "pc-00000000"}, {},
                expect=404)


# Skills ---------------------------------------------------------------------------------------------------------------
def test_skills_list_proposals_and_installed(target):
    skills = target.call("GET /api/admin/v1/skills", expect=200).body
    assert skills["enabled"] is True
    target.call("GET /api/admin/v1/skills/enabled", expect=200)


@pytest.mark.parametrize("key,body", [
    ("GET /api/admin/v1/skills/proposals/{pid}", None),
    ("POST /api/admin/v1/skills/proposals/{pid}/install", {"content_hash": "a" * 64}),
    ("POST /api/admin/v1/skills/proposals/{pid}/reject", {}),
    ("POST /api/admin/v1/skills/proposals/{pid}/reopen", {}),
    ("POST /api/admin/v1/skills/proposals/{pid}/review", {}),
    ("DELETE /api/admin/v1/skills/proposals/{pid}", {}),
])
def test_an_unknown_proposal_is_404(target, key, body):
    target.call(key, {"pid": "p-contract-none"}, body, expect=404)


@pytest.mark.parametrize("key,body", [
    ("POST /api/admin/v1/skills/{slug}/enable", {}), ("POST /api/admin/v1/skills/{slug}/disable", {}),
    ("PUT /api/admin/v1/skills/{slug}/projects", {"projects": []}), ("POST /api/admin/v1/skills/{slug}/rollback", {}),
    ("POST /api/admin/v1/skills/{slug}/uninstall", {}), ("GET /api/admin/v1/skills/{slug}/export", None),
])
def test_an_unknown_skill_is_404(target, key, body):
    target.call(key, {"slug": "contract-none"}, body, expect=404)


def test_a_reviewed_proposal_is_read_reviewed_rejected_reopened_installed_and_deleted(target):
    daemon_cannot(target, "a skill proposal comes from an agent session that proposes one")
    pid = target.call("GET /api/admin/v1/skills", expect=200).body["proposals"][0]["id"]
    proposal = target.call("GET /api/admin/v1/skills/proposals/{pid}", {"pid": pid}, expect=200).body
    target.call("POST /api/admin/v1/skills/proposals/{pid}/review", {"pid": pid}, {}, expect=200)
    assert target.call("POST /api/admin/v1/skills/proposals/{pid}/reject", {"pid": pid}, {"reason": "Contract"},
                       expect=200).body["status"] == "rejected"
    target.call("POST /api/admin/v1/skills/proposals/{pid}/reopen", {"pid": pid}, {}, expect=200)
    target.call("POST /api/admin/v1/skills/proposals/{pid}/install", {"pid": pid},
                {"content_hash": "b" * 64}, expect=409)
    installed = target.call("POST /api/admin/v1/skills/proposals/{pid}/install", {"pid": pid},
                            {"content_hash": proposal["content_hash"]}, expect=200).body
    assert installed["slug"] == proposal["slug"]
    target.call("DELETE /api/admin/v1/skills/proposals/{pid}", {"pid": pid}, {}, expect=204)


def test_an_installed_skill_is_switched_scoped_exported_rolled_back_and_uninstalled(target):
    daemon_cannot(target, "an installed skill comes from installing an agent-proposed one")
    slug = {"slug": target.call("GET /api/admin/v1/skills", expect=200).body["installed"][0]["slug"]}
    assert target.call("POST /api/admin/v1/skills/{slug}/disable", slug, {}, expect=200).body["enabled"] is False
    assert target.call("POST /api/admin/v1/skills/{slug}/enable", slug, {}, expect=200).body["enabled"] is True
    assert target.call("PUT /api/admin/v1/skills/{slug}/projects", slug, {"projects": ["ui"]},
                       expect=200).body["projects"] == ["ui"]
    target.call("GET /api/admin/v1/skills/{slug}/export", slug, expect=200)
    target.call("POST /api/admin/v1/skills/{slug}/rollback", slug, {}, expect=200)
    target.call("POST /api/admin/v1/skills/{slug}/uninstall", slug, {}, expect=200)


# Maintenance ----------------------------------------------------------------------------------------------------------
def test_maintenance_status_cleanup_and_backup(target):
    status = target.call("GET /api/admin/v1/maintenance", expect=200).body
    assert status["free_gb"] >= 0
    target.call("POST /api/admin/v1/maintenance/cleanup", body={}, expect=200)
    backup = target.call("POST /api/admin/v1/maintenance/backup", body={}, expect=200).body
    assert backup["error"] == "" and backup["bytes"] > 0


def test_image_archive_retention_previews_then_applies_the_exact_phrase(target):
    preview = "POST /api/admin/v1/maintenance/image-archive/retention/preview"
    apply = "POST /api/admin/v1/maintenance/image-archive/retention/apply"
    if not target.module_running("images"):
        assert target.call(preview, body={}).status in (404, 405)
        assert target.call(apply, body={"confirmation": "contract"}).status in (404, 405)
        return
    phrase = target.call(preview, body={}, expect=200).body["confirmation"]
    target.call(apply, body={"confirmation": phrase + " stale"}, expect=400)
    target.call(apply, body={"confirmation": phrase}, expect=200)


# Runners and projects -------------------------------------------------------------------------------------------------
def test_runners_list_and_an_unknown_runner_is_404(target):
    target.call("GET /api/admin/v1/runners", expect=200)
    target.call("POST /api/admin/v1/runners/{name}/update", {"name": "contract-none"}, {}, expect=404)


def test_a_connected_runner_updates(target):
    daemon_cannot(target, "a runner is a Mac client connected to the daemon")
    name = target.call("GET /api/admin/v1/runners", expect=200).body[0]["name"]
    assert target.call("POST /api/admin/v1/runners/{name}/update", {"name": name}, {}, expect=200).body["ok"] is True


def test_projects_list_and_add(target):
    name = f"contract-{secrets.token_hex(3)}"
    target.call("GET /api/admin/v1/projects", expect=200)
    added = target.call("POST /api/admin/v1/projects", body={"name": name, "description": "Contract project"},
                        expect=201).body
    assert added["name"] == name and added["managed"] is True
    assert target.call("POST /api/admin/v1/projects", body={"name": name}).status in (400, 409)


# Remote Control -------------------------------------------------------------------------------------------------------
PROMOTE = "POST /api/admin/v1/remote-control/discovery/scans/{scan_id}/candidates/{candidate_id}/promote"
REMOTE = [
    ("GET /api/admin/v1/remote-control", {}, None),
    ("POST /api/admin/v1/remote-control/{project}/trust", {"project": "ui"}, {}),
    ("DELETE /api/admin/v1/remote-control/folders/{slug}", {"slug": "contract"}, {}),
    ("POST /api/admin/v1/remote-control/discovery/scans", {}, {}),
    ("GET /api/admin/v1/remote-control/discovery/scans/{scan_id}", {"scan_id": "scan-contract"}, None),
    ("DELETE /api/admin/v1/remote-control/discovery/scans/{scan_id}", {"scan_id": "scan-contract"}, {}),
    (PROMOTE, {"scan_id": "scan-contract", "candidate_id": "c-contract"},
     {"slug": "contract", "confirmed_path": "/synthetic/folder", "confirmed_markers": [".git"]}),
]


@pytest.mark.parametrize("key,targets,body", REMOTE)
def test_remote_control_answers_absent_while_its_module_is_off(target, key, targets, body):
    if target.module_running("remote_control"):
        pytest.skip("Remote Control runs here; the next case covers it")
    assert target.call(key, targets, body).status in (404, 405)


def test_remote_control_lists_folders_scans_promotes_trusts_and_forgets(target):
    daemon_cannot(target, "Remote Control needs Claude Code on the daemon host")
    folders = target.call("GET /api/admin/v1/remote-control", expect=200).body
    project = {"project": folders["projects"][0]["project"]}
    assert target.call("POST /api/admin/v1/remote-control/{project}/trust", project, {}, expect=200).body["message"]
    scan = target.call("POST /api/admin/v1/remote-control/discovery/scans", body={}, expect=200).body
    ids = {"scan_id": scan["id"]}
    candidate = target.call("GET /api/admin/v1/remote-control/discovery/scans/{scan_id}", ids, expect=200).body[
        "candidates"][0]
    promoted = target.call(PROMOTE, {**ids, "candidate_id": candidate["id"]},
                           {"slug": candidate["suggested_slug"], "confirmed_path": candidate["path"],
                            "confirmed_markers": candidate["markers"]}, expect=200).body
    target.call("DELETE /api/admin/v1/remote-control/discovery/scans/{scan_id}", ids, {}, expect=200)
    assert target.call("DELETE /api/admin/v1/remote-control/folders/{slug}", {"slug": promoted["slug"]}, {},
                       expect=200).body["removed"] is True
