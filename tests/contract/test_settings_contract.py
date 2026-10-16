"""Settings: the registry with optimistic revisions, rollback, restart and the moved controls
(daemon.SETTINGS_ROUTES)."""

from __future__ import annotations

import pytest

from .conftest import daemon_cannot


def live_int(config: dict) -> dict:
    """A writable live integer setting with room above its value: what a save can change and roll back."""
    return next(row for row in config["settings"] if row["type"] == "int" and row["writable"]
                and row["apply"] == "live" and isinstance(row["effective"], int)
                and (row["maximum"] is None or row["effective"] < row["maximum"]))


def test_the_schema_and_the_config_describe_every_setting(target):
    schema = target.call("GET /api/admin/v1/config/schema", expect=200).body
    config = target.call("GET /api/admin/v1/config", expect=200).body
    assert schema["settings"] and {row["key"] for row in config["settings"]} >= {row["key"] for row in
                                                                                 schema["settings"]}


def test_validate_plans_a_change_and_names_each_bad_key(target):
    config = target.call("GET /api/admin/v1/config", expect=200).body
    row = live_int(config)
    plan = target.call("POST /api/admin/v1/config/validate",
                       body={"revision": config["revision"], "changes": {row["key"]: row["effective"] + 1}},
                       expect=200).body
    assert plan["target_revision"] > plan["revision"] == config["revision"]
    assert plan["changes"][0]["key"] == row["key"] and plan["changes"][0]["to"] == row["effective"] + 1
    refused = target.call("POST /api/admin/v1/config/validate",
                          body={"revision": config["revision"], "changes": {"contract.no_such_key": 1}}, expect=400)
    assert refused.code == "validation_error" and refused.body["error"]["keys"]["contract.no_such_key"]["code"]


def test_a_stale_revision_is_a_409_naming_both_revisions(target):
    config = target.call("GET /api/admin/v1/config", expect=200).body
    row = live_int(config)
    stale = config["revision"] + 5
    for key in ("POST /api/admin/v1/config/validate", "PATCH /api/admin/v1/config"):
        refused = target.call(key, body={"revision": stale, "changes": {row["key"]: row["effective"]}}, expect=409)
        assert refused.code == "revision_conflict"
        assert refused.body["error"]["details"] == {"expected_revision": stale, "current_revision": config["revision"]}


def test_save_then_roll_back_restores_the_previous_generation(target):
    config = target.call("GET /api/admin/v1/config", expect=200).body
    row = live_int(config)
    saved = target.call("PATCH /api/admin/v1/config",
                        body={"revision": config["revision"], "changes": {row["key"]: row["effective"] + 1}},
                        expect=200).body
    assert saved["revision"] > config["revision"]
    assert next(s for s in saved["settings"] if s["key"] == row["key"])["effective"] == row["effective"] + 1
    unconfirmed = target.call("POST /api/admin/v1/config/rollback", body={"revision": saved["revision"]},
                              expect=400, conform=False)
    assert unconfirmed.code == "confirmation_required"
    restored = target.call("POST /api/admin/v1/config/rollback",
                           body={"revision": saved["revision"], "confirm": True}, expect=200).body
    assert next(s for s in restored["settings"] if s["key"] == row["key"])["effective"] == row["effective"]


def test_restart_is_accepted_only_by_a_supervised_daemon(target):
    supervised = target.call("GET /health", auth=False, expect=200).body["config"]["supervised_restart"]
    revision = target.call("GET /api/admin/v1/config", expect=200).body["revision"]
    if supervised:
        daemon_cannot(target, "a supervised restart ends the shared daemon (tests/e2e restarts a supervised one)")
        started = target.call("POST /api/admin/v1/config/restart", body={"revision": revision, "confirm": True},
                              expect=202).body
        assert started["accepted"] is True
    else:
        refused = target.call("POST /api/admin/v1/config/restart", body={"revision": revision, "confirm": True},
                              expect=409)
        assert refused.code == "restart_not_supervised" and refused.body["error"]["details"]["manual"]


def test_restart_without_confirm_is_refused(target):
    revision = target.call("GET /api/admin/v1/config", expect=200).body["revision"]
    refused = target.call("POST /api/admin/v1/config/restart", body={"revision": revision}, expect=400,
                          conform=False)
    assert refused.code == "confirmation_required"


def test_smart_approvals_read_and_save(target):
    mode = target.call("GET /api/admin/v1/smart-approvals", expect=200).body["mode"]
    assert target.call("PUT /api/admin/v1/smart-approvals", body={"mode": mode}, expect=200).body["mode"] == mode


def test_backends_read_and_save(target):
    backend = target.call("GET /api/admin/v1/backends", expect=200).body[0]
    saved = target.call("PUT /api/admin/v1/backends/{name}", {"name": backend["name"]}, {"model": backend["model"]},
                        expect=200).body
    assert saved["name"] == backend["name"] and saved["model"] == backend["model"]


def test_the_memory_profile_reads_and_saves(target):
    memory = target.call("GET /api/admin/v1/memory", expect=200).body
    assert memory["enabled"] is True
    saved = target.call("PUT /api/admin/v1/memory/profile", body={"content": "Contract profile."}, expect=200).body
    assert saved["profile"] == "Contract profile."


@pytest.mark.parametrize("enabled", [True, False])
def test_github_member_auth_reads_and_saves(target, enabled):
    current = target.call("GET /api/admin/v1/github-member-auth", expect=200).body
    if enabled and not current["configured"]:
        refused = target.call("PUT /api/admin/v1/github-member-auth", body={"enabled": True})
        assert refused.status in (400, 409)
        return
    saved = target.call("PUT /api/admin/v1/github-member-auth", body={"enabled": enabled}, expect=200).body
    assert saved["enabled"] is enabled


def test_google_signin_reads_its_preflight(target):
    target.call("GET /api/admin/v1/google-signin", expect=200)


def test_a_test_notification_answers_whether_it_was_sent(target):
    assert target.call("POST /api/admin/v1/notify/test", body={}, expect=200).body["sent"] is True
