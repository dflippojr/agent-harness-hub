"""Administration parity, owner writes, exact confirmations and credential isolation."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from harness_hub.administration import OPERATIONS
from harness_hub.daemon import ADMIN_ROUTES

from .conftest import HUB_HOST, assert_no_secrets

ORIGIN = {"Origin": "http://" + HUB_HOST, "X-Hub-Request": "1"}
TARGETS = {
    "user_id": "u-synthetic",
    "pid": "p-synthetic",
    "slug": "synthetic",
    "name": "synthetic",
    "credential_id": "pc-synthetic",
    "project": "synthetic",
    "scan_id": "scan-synthetic",
    "candidate_id": "c-synthetic",
}
INPUTS = {
    "account_add": {"login": "new@example.test", "display_name": "New synthetic"},
    "account_save": {"enabled": False},
    "credential_set": {"app_id": "k-synthetic", "backend": "claude", "secret_ref": "synthetic-name"},
    "skill_install": {"content_hash": "a" * 64},
    "skill_projects": {"projects": ["synthetic"]},
    "retention_apply": {"confirmation": "DELETE 2 ARCHIVED IMAGES synthetic"},
    "project_add": {"name": "synthetic"},
    "scan_promote": {"slug": "synthetic", "confirmed_path": "C:/synthetic/folder", "confirmed_markers": [".git"]},
}


def call(hub, operation, body=None, headers=None, targets=None, params=None):
    method, path = ADMIN_ROUTES[operation]
    names = re.findall(r"{(\w+)}", path)
    query = {name: (targets or TARGETS)[name] for name in names} | (params or {})
    return hub._keep(
        hub.client.request(
            method,
            "/api/manage/" + operation,
            params=query,
            json=body if method != "GET" else None,
            headers=ORIGIN if headers is None else headers,
        )
    )


def test_checked_in_daemon_route_snapshot_has_no_gap(claimed):
    snapshot = json.loads((Path(__file__).parent / "fixtures" / "administration-routes.json").read_text())
    assert {tuple(row) for row in snapshot["routes"]} == set(ADMIN_ROUTES.values())
    catalogue = claimed.get("/api/manage/catalog").json()
    assert {row["id"] for row in catalogue["operations"]} == set(ADMIN_ROUTES) == set(OPERATIONS)
    assert all(row["cli"].startswith("harness ") for row in catalogue["operations"])
    assert len(catalogue["not_offered"]) == 2
    assert all("hub_key" in row["reason"] for row in catalogue["not_offered"])


@pytest.mark.parametrize("operation", list(OPERATIONS))
def test_each_group_dispatches_exact_owner_route_and_withholds_extra_fields(claimed, fake_daemon, operation):
    spec = OPERATIONS[operation]
    body = dict(INPUTS.get(operation, {}))
    if spec[5]:
        body["confirm"] = True
    response = call(claimed, operation, body)
    assert response.status_code in (200, 201, 204), response.text
    method, path = ADMIN_ROUTES[operation]
    assert fake_daemon.admin_calls[-1][:2] == (method, path.format(**TARGETS).lstrip("/"))
    assert_no_secrets(claimed.bodies, fake_daemon.hub_token, "provider-secret-sentinel", "private-subject")
    if response.content and isinstance(response.json(), dict):
        assert "credential" not in response.json().get("manifest", {})
    if operation != "google_invite":
        assert fake_daemon.invitation_code not in response.text


@pytest.mark.parametrize("operation", [name for name, spec in OPERATIONS.items() if spec[5]])
def test_destructive_operations_require_confirmation_before_any_daemon_call(claimed, fake_daemon, operation):
    body = dict(INPUTS.get(operation, {}))
    for value in (None, False, "true", 1):
        assert call(claimed, operation, {**body, "confirm": value}).status_code == 400
    assert not fake_daemon.admin_calls


def test_member_lifecycle_google_once_only_and_daemon_confirmation(claimed, fake_daemon):
    created = call(claimed, "account_add", INPUTS["account_add"]).json()
    targets = {"user_id": created["user_id"]}
    assert call(claimed, "account", targets=targets).json()["enabled"] is True
    assert (
        call(
            claimed,
            "account_save",
            {
                "display_name": "Renamed synthetic",
                "login": "rebound@example.test",
                "max_running": 3,
                "max_queued": 8,
                "disk_quota_bytes": 2048,
                "confirm": True,
            },
            targets=targets,
        ).status_code
        == 200
    )
    invitation = call(claimed, "google_invite", {}, targets=targets)
    assert invitation.json()["code"] == fake_daemon.invitation_code
    assert invitation.headers["cache-control"] == "no-store"
    for operation in ("account", "google_cancel", "google_revoke", "google_unlink", "github_reset"):
        response = call(claimed, operation, {"confirm": True} if OPERATIONS[operation][5] else {}, targets=targets)
        assert response.status_code == 200
        assert fake_daemon.invitation_code not in response.text
        if operation in ("google_unlink", "github_reset"):
            assert fake_daemon.admin_calls[-1][2] == {"confirm": True}
    assert (
        call(claimed, "account_save", {"enabled": False, "confirm": True}, targets=targets).json()["enabled"] is False
    )
    assert call(claimed, "google_invite", {}, targets=targets).status_code == 409
    assert call(claimed, "account_save", {"enabled": True, "confirm": True}, targets=targets).json()["enabled"] is True
    later = len(claimed.bodies)
    for operation in ("accounts", "account_audit", "audit"):
        call(claimed, operation)
    assert_no_secrets(claimed.bodies[later:], fake_daemon.hub_token, fake_daemon.invitation_code)
    for path in claimed.state_dir.iterdir():
        if path.is_file():
            assert fake_daemon.invitation_code.encode() not in path.read_bytes()


def test_name_only_provider_assignment_and_input_rejection(claimed, fake_daemon):
    for name in ("../synthetic", "C:/synthetic/key", "value with spaces"):
        assert (
            call(
                claimed, "credential_set", {**INPUTS["credential_set"], "secret_ref": name, "confirm": True}
            ).status_code
            == 400
        )
    for field in ("key", "api_key", "token", "credential"):
        assert (
            call(claimed, "credential_set", {**INPUTS["credential_set"], field: "secret", "confirm": True}).status_code
            == 400
        )
    assert not fake_daemon.admin_calls
    reply = call(
        claimed, "credential_set", {**INPUTS["credential_set"], "models": ["synthetic-model"], "confirm": True}
    )
    assert reply.json()["secret_ref"] == "synthetic-name"
    assert fake_daemon.admin_calls[-1][2] == {**INPUTS["credential_set"], "models": ["synthetic-model"]}
    assert call(claimed, "credential_revoke", {"confirm": True}).status_code == 204
    assert call(claimed, "credentials").json() == []


def test_hash_and_phrase_are_forwarded_exactly_with_stale_refusal(claimed, fake_daemon):
    reviewed = call(claimed, "proposal").json()
    fake_daemon.proposal["content_hash"] = "b" * 64
    assert (
        call(claimed, "skill_install", {"content_hash": reviewed["content_hash"], "confirm": True}).status_code == 409
    )
    assert call(claimed, "skill_install", {"content_hash": "b" * 64, "confirm": True}).status_code == 200
    assert call(claimed, "skill_install", {"content_hash": "bad", "confirm": True}).status_code == 400
    preview = call(claimed, "retention_preview", {}).json()
    phrase = preview["confirmation"] + " "
    assert call(claimed, "retention_apply", {"confirmation": phrase, "confirm": True}).status_code == 400
    assert fake_daemon.admin_calls[-1][2] == {"confirmation": phrase}
    assert (
        call(claimed, "retention_apply", {"confirmation": preview["confirmation"], "confirm": True}).json()["removed"]
        == 2
    )


def test_audit_rows_keep_the_daemons_time_and_detail(claimed, fake_daemon):
    audit = call(claimed, "audit").json()["items"][0]
    household = call(claimed, "account_audit").json()[0]
    for row in (audit, household):
        assert row["ts"] == 1700000000.5 and row["detail"] == "added member"
    assert set(audit["metadata"]) == {"fields", "new_max_running"}


def test_audit_cursor_filters_and_bounded_queries(claimed, fake_daemon):
    filters = {
        "limit": 10,
        "actor_id": "k-synthetic",
        "key_id": "k-synthetic",
        "target_id": "u-synthetic",
        "action": "create",
        "outcome": "ok",
        "since": 1.5,
        "until": 2.5,
    }
    data = call(claimed, "audit", params=filters).json()
    assert data["next_before_id"] == 2 and "credential" not in data["items"][0]["metadata"]
    assert fake_daemon.admin_calls[-1][3] == {key: str(value) for key, value in filters.items()}
    assert call(claimed, "audit", params={**filters, "before_id": 2}).json() == {"items": [], "next_before_id": None}
    before = len(fake_daemon.admin_calls)
    for params in ({"limit": "NaN"}, {"limit": 0}, {"since": "Infinity"}, {"url": "https://other.test"}):
        assert call(claimed, "audit", params=params).status_code == 400
    assert len(fake_daemon.admin_calls) == before


@pytest.mark.parametrize("operation", list(OPERATIONS))
def test_all_operations_require_signed_in_owner_and_secure_writes(claimed, fake_daemon, operation):
    if ADMIN_ROUTES[operation][0] != "GET":
        for headers in ({}, {"X-Hub-Request": "1"}, {**ORIGIN, "Origin": "https://other.test"}):
            assert call(claimed, operation, {}, headers).status_code == 403
    claimed.delete("/api/session")
    assert call(claimed, operation, {}).status_code == 401
    assert not fake_daemon.admin_calls


def test_malformed_fields_targets_payloads_and_no_arbitrary_operations(claimed, fake_daemon):
    for target in ("..", "../keys", "synthetic/keys", "https://other.test", "x?foo=1"):
        assert call(claimed, "account", targets={"user_id": target}).status_code == 400
    for body in ({"max_running": True}, {"max_queued": 0}, {"enabled": "false"}, {"disk_quota_bytes": -1}, {}):
        assert call(claimed, "account_save", {**body, "confirm": True}).status_code == 400
    assert claimed.get("/api/manage/accounts?user_id=ignored").status_code == 400
    assert claimed.get("/api/manage/audit?limit=2&limit=3").status_code == 400
    assert claimed.get("/api/manage/owner_key").status_code == 404
    assert claimed.post("/api/manage/accounts", headers=ORIGIN, json={}).status_code == 404
    assert claimed.post("/api/manage/account_add", content="x" * 65537, headers=ORIGIN).status_code == 413
    assert not fake_daemon.admin_calls


def test_invalid_views_failure_and_release_are_visible(claimed, fake_daemon):
    fake_daemon.admin_overrides["accounts"] = {"items": []}
    assert call(claimed, "accounts").status_code == 502
    fake_daemon.admin_overrides["accounts"] = [None]
    assert call(claimed, "accounts").status_code == 502
    fake_daemon.admin_overrides.clear()
    fake_daemon.admin_error = (
        409,
        {"detail": "conflict", "error": {"code": "stale"}, "token": "provider-secret-sentinel"},
    )
    assert call(claimed, "maintenance").json()["error"]["code"] == "stale"
    fake_daemon.admin_error = (401, {})
    assert call(claimed, "maintenance").status_code == 409
    assert claimed.get("/api/claim").json()["state"] == "claimed"
    fake_daemon.down = True
    assert call(claimed, "maintenance").status_code == 503
    fake_daemon.down = False
    fake_daemon.release()
    assert call(claimed, "maintenance").status_code == 409
    assert claimed.get("/api/claim").json()["state"] == "released"
    assert_no_secrets(claimed.bodies, fake_daemon.hub_token, "provider-secret-sentinel")


def test_structured_review_and_runner_history_are_allowlisted(claimed):
    proposal = call(claimed, "proposal").json()
    assert proposal["review"]["recommendation"] == "approve"
    assert proposal["review"]["scope"] == {"ok": True, "notes": "Synthetic finding"}
    runner = call(claimed, "runners").json()[0]
    assert runner["last_update"]["ok"] is True
    assert runner["last_update"]["version"] == "1"
    assert "token" not in runner["info"]


def test_proposal_and_export_preserve_examples_and_all_hash_covered_fields(claimed, fake_daemon):
    for operation in ("proposal", "skill_export"):
        reply = call(claimed, operation)
        assert reply.status_code == 200
        shown = reply.json()
        for key in ("slug", "title", "purpose", "activation_suggestion", "skill_md", "references", "examples"):
            assert shown[key] == fake_daemon.proposal[key]
        assert shown["source_session_id"] == "s-synthetic"
        if operation == "proposal":
            assert shown["review_complete"] is True


@pytest.mark.parametrize("field,value", [
    ("examples", [{"prompt": "shown", "expected": "shown", "hidden_instruction": "not shown"}]),
    ("examples", [{"prompt": "x" * 262145, "expected": "shown"}]),
    ("examples", [{"prompt": "shown", "expected": "ho-hidden-instruction"}]),
    ("examples", [{"expected": "missing prompt"}]),
    ("examples", None),
    ("references", [{"path": "notes.md", "content": "shown", "extra": "hidden"}]),
    ("references", [{"path": "notes.md", "content": "x" * 262145}]),
    ("references", [{"path": "notes.md", "content": "hp-hidden-instruction"}]),
    ("references", None),
    ("skill_md", "x" * 262145),
    ("title", "x" * 32769),
    ("purpose", "ha-hidden-instruction"),
    ("content_hash", "invalid"),
], ids=lambda value: type(value).__name__)
def test_partial_or_redacted_proposals_never_authorize_install(claimed, fake_daemon, field, value):
    fake_daemon.proposal[field] = value
    response = call(claimed, "proposal")
    assert response.status_code == 200
    assert response.json()["review_complete"] is False
    assert call(claimed, "skill_install", {**INPUTS["skill_install"], "confirm": True}).status_code == 409
    assert not any(path.endswith("/install") for _, path, _, _ in fake_daemon.admin_calls)


def test_missing_hash_covered_field_disables_install_and_alias_examples_survive(claimed, fake_daemon):
    fake_daemon.proposal["examples"] = [{"prompt": "Question", "expected_behavior": "Answer"}]
    assert call(claimed, "proposal").json()["examples"] == fake_daemon.proposal["examples"]
    assert call(claimed, "proposal").json()["review_complete"] is True
    del fake_daemon.proposal["activation_suggestion"]
    assert call(claimed, "proposal").json()["review_complete"] is False
    assert call(claimed, "skill_install", {**INPUTS["skill_install"], "confirm": True}).status_code == 409


@pytest.mark.parametrize("operation,path,body", [
    ("skills", "skills", {"enabled": True, "proposals": [{"id": "p-safe", "slug": "ha-tools", "title": "ho-hum notes"}],
                          "installed": [{"slug": "safe-skill", "title": "Safe skill"}]}),
    ("remote", "remote-control", {"projects": [{"project": "safe-folder", "path": "/srv/ha-config"}]}),
    ("scan", "remote-control/discovery/scans/scan-synthetic", {"id": "scan-synthetic", "candidates": [
        {"id": "bad", "suggested_slug": "bad", "path": "/srv/ha-config", "markers": [".git"]},
        {"id": "good", "suggested_slug": "good", "path": "/srv/safe", "markers": [".git"]}]}),
    ("audit", "audit", {"items": [{"id": 2, "target_id": "hp-printer"}], "next_before_id": 2}),
    ("account_audit", "accounts/audit", [{"id": 2, "target_id": "hp-printer"}]),
    ("runners", "runners", [{"name": "ha-runner"}, {"name": "safe-runner"}]),
    ("projects", "projects", [{"name": "ho-project", "description": "hp-printer helper"}]),
    ("maintenance", "maintenance", {"backup": {"path": "/srv/ha-backup"}}),
    ("accounts", "accounts", [{"user_id": "safe-member", "display_name": "ho-hum member"}]),
])
def test_token_shaped_text_cannot_blank_administration_areas(claimed, fake_daemon, operation, path, body):
    fake_daemon.admin_overrides[path] = body
    reply = call(claimed, operation)
    assert reply.status_code == 200, reply.text
    assert_no_secrets([reply.text], fake_daemon.hub_token)
    if operation == "skills":
        assert reply.json()["proposals"][0]["slug"] == ""
        assert reply.json()["installed"][0]["slug"] == "safe-skill"
    if operation == "scan":
        assert [row["promotion_available"] for row in reply.json()["candidates"]] == [False, True]
    if operation == "audit":
        assert reply.json()["next_before_id"] == 2


@pytest.mark.parametrize("field", ["title", "slug", "skill_md", "examples", "references"])
def test_exact_secrets_still_withhold_the_entire_proposal_even_beyond_limits(claimed, fake_daemon, field):
    secret = fake_daemon.hub_token
    text = "x" * 262145 + secret
    value = [{"prompt": text, "expected": "shown"}] if field == "examples" else (
        [{"path": "notes.md", "content": text}] if field == "references" else text)
    fake_daemon.proposal[field] = value
    response = call(claimed, "proposal")
    assert response.status_code == 500
    assert secret not in response.text


@pytest.mark.parametrize("operation,path,key", [
    ("accounts", "accounts", "user_id"), ("runners", "runners", "name"),
    ("credentials", "provider-credentials", "id"),
])
def test_invalid_action_identifiers_are_not_truncated_into_valid_targets(claimed, fake_daemon, operation, path, key):
    fake_daemon.admin_overrides[path] = [{key: "../unsafe"}, {key: "x" * 81}, {key: "safe-target"}]
    assert [row[key] for row in call(claimed, operation).json()] == ["", "", "safe-target"]


def test_project_allowlist_requires_confirmation_and_forwards_empty_scope(claimed, fake_daemon):
    assert call(claimed, "skill_projects", {"projects": ["new-project"]}).status_code == 400
    assert not fake_daemon.admin_calls
    response = call(claimed, "skill_projects", {"projects": [], "confirm": True})
    assert response.status_code == 200
    assert response.json()["projects"] == []
    assert response.json()["projects_complete"] is True
    assert fake_daemon.admin_calls[-1][2] == {"projects": []}
    fake_daemon.skill["projects"] = ["ha-tools"]
    row = call(claimed, "skills").json()["installed"][0]
    assert row["projects_complete"] is False
    assert row["projects"] == ["[withheld: token-shaped]"]
