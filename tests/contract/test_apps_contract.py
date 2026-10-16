"""Apps: the key list, the Hub inventory, session caps, retention, revoke and erasures (daemon.APPS_ROUTES)."""

from __future__ import annotations

UNKNOWN_APP = "k-00000000"


def test_the_key_list_has_the_app_and_this_hub(target):
    app_id = target.arrange_app_key("Contract keys App")
    keys = target.call("GET /api/admin/v1/keys", expect=200).body
    assert any(row["id"] == app_id and row["kind"] == "app" for row in keys)
    assert next(row for row in keys if row["id"] == target.key_id)["role"] == "hub"


def test_the_inventory_has_modules_apps_with_their_state_and_entries(target):
    app_id = target.arrange_app_key("Contract inventory App")
    inventory = target.call("GET /api/admin/v1/hub", expect=200).body
    assert next(row for row in inventory["modules"] if row["name"] == "hub")["state"] == "present"
    assert any(row["id"] == app_id for row in inventory["apps"])
    assert next(row for row in inventory["apps"] if row["id"] == target.key_id)["role"] == "hub"


def test_the_inventory_is_refused_while_its_switch_is_off(target):
    schema = target.call("GET /api/admin/v1/config/schema", expect=200).body
    switch = next(row["key"] for row in schema["settings"] if row["type"] == "bool" and row["key"].endswith(".enabled")
                  and "hub" in row["modules"])
    for enabled, status in ((False, 400), (True, 200)):
        revision = target.call("GET /api/admin/v1/config", expect=200).body["revision"]
        target.call("PATCH /api/admin/v1/config", body={"revision": revision, "changes": {switch: enabled}},
                    expect=200)
        target.call("GET /api/admin/v1/hub", expect=status)


def test_session_caps_read_and_save(target):
    app_id = target.arrange_app_key("Contract caps App")
    target.call("GET /api/admin/v1/apps/{app_id}/limits", {"app_id": app_id}, expect=200)
    saved = target.call("PUT /api/admin/v1/apps/{app_id}/limits", {"app_id": app_id},
                        {"max_running": 3, "max_queued": None}, expect=200).body
    assert saved["configured"] == {"max_running": 3, "max_queued": None} and saved["effective"]["max_running"] == 3
    target.call("GET /api/admin/v1/apps/{app_id}/limits", {"app_id": UNKNOWN_APP}, expect=404)
    target.call("PUT /api/admin/v1/apps/{app_id}/limits", {"app_id": UNKNOWN_APP}, {"max_running": 3}, expect=404)


def test_retention_answers_the_key_row(target):
    app_id = target.arrange_app_key("Contract retention App")
    row = target.call("PUT /api/admin/v1/apps/{app_id}/retention", {"app_id": app_id}, {"retention_days": 7.5},
                      expect=200).body
    assert row["id"] == app_id and row["retention_days"] == 7.5
    target.call("PUT /api/admin/v1/apps/{app_id}/retention", {"app_id": UNKNOWN_APP}, {"retention_days": 7.5},
                expect=404)


def test_revoke_schedules_the_erasure_and_never_revokes_the_hub_key(target):
    app_id = target.arrange_app_key("Contract revoke App")
    target.call("DELETE /api/admin/v1/keys/{app_id}", {"app_id": app_id}, expect=204)
    erasure = next(row for row in target.call("GET /api/admin/v1/apps/erasures", expect=200).body
                   if row["id"] == app_id)
    assert erasure["erase_after"] > erasure["revoked_at"]
    target.call("DELETE /api/admin/v1/keys/{app_id}", {"app_id": app_id}, expect=404)
    assert target.call("DELETE /api/admin/v1/keys/{app_id}", {"app_id": target.key_id}, expect=409).code == "hub_key"
