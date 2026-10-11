"""Marketplace: local entries, the consent-gated arm, native confirm, withdraw, last good entries and secrecy."""
import json
import re
from urllib.parse import urlsplit

import pytest

from harness_hub.views import SCOPE_DISCLOSURES, entry_view

from .conftest import HUB_HOST, assert_no_secrets
from .marketplace_fake import manifest_entry

ORIGIN = {"Origin": "http://" + HUB_HOST, "X-Hub-Request": "1"}
ICON = ("data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5"
        "ErkJggg==")


@pytest.fixture
def entries(fake_daemon):
    fake_daemon.app_entries = [
        manifest_entry("test.example.notes", "Synthetic notes", origins=["https://notes.example.test"],
                       state="paired", paired=["k-notes"], icon=ICON),
        manifest_entry("test.example.native", "Synthetic native", icon="https://icons.example.invalid/native.png"),
        manifest_entry("test.example.ops", "Synthetic ops", scopes=("sessions", "homelab"),
                       origins=["https://ops.example.test", "https://ops2.example.test"]),
    ]
    fake_daemon.app_entries[1]["unverified_origin"] = {"paired": ["k-native"], "pending_pairing": False}
    return fake_daemon.app_entries


def market(claimed):
    reply = claimed.get("/api/marketplace")
    assert reply.status_code == 200, reply.text
    return reply.json()


def by_id(data):
    return {entry["catalog_app_id"]: entry for entry in data["entries"]}


def arm(claimed, body, headers=None):
    return claimed._keep(claimed.client.post("/api/marketplace/arm", json=body,
                                             headers=ORIGIN if headers is None else headers))


def test_entries_show_display_fields_state_and_unverified_origin_only(claimed, fake_daemon, entries):
    data = market(claimed)
    assert data["state"] == "ready" and data["error"] == "" and not data["stale"] and data["withheld"] == 0
    assert data["daemon_url"] == "https://daemon.test"
    shown = by_id(data)
    notes, native, ops = shown["test.example.notes"], shown["test.example.native"], shown["test.example.ops"]
    assert notes["name"] == "Synthetic notes" and notes["publisher"] == {"name": "Synthetic publisher",
                                                                       "id": "pub-0a1b2c3d4e5f"}
    assert notes["summary"] == "Synthetic local entry for Synthetic notes." and notes["version"] == "1.0.0"
    assert notes["verified"] is False and notes["state"] == "paired" and notes["paired"] == ["k-notes"]
    assert notes["browser_origins"] == ["https://notes.example.test"] and notes["icon"] == ICON
    assert native["state"] == "not_paired" and native["paired"] == [] and native["icon"] == ""
    assert native["unverified_origin"] == {"paired": ["k-native"], "pending_pairing": False}
    assert ops["elevated"] == ["homelab"]
    assert [(s["scope"], s["tier"]) for s in ops["scopes"]] == [("sessions", "standard"), ("homelab", "elevated")]
    assert ops["scopes"][1]["text"] == SCOPE_DISCLOSURES["homelab"]
    assert ops["scopes"][1]["justification"].startswith("Synthetic justification for homelab")
    assert set(notes) == {"catalog_app_id", "name", "version", "summary", "publisher", "icon", "scopes", "elevated",
                          "browser_origins", "verified", "state", "paired", "pending_pairing", "unverified_origin",
                          "slots"}
    body = claimed.bodies[-1]
    # The manifest's URLs, e-mail addresses and review evidence never reach the browser.
    assert "example.invalid" not in body and "@" not in body and "review" not in body
    assert_no_secrets(claimed.bodies, fake_daemon.hub_token)


def test_the_hub_never_requests_a_url_from_an_entry(claimed, fake_daemon, entries):
    market(claimed)
    rid = arm(claimed, {"catalog_app_id": "test.example.native", "scopes": ["sessions"]}).json()["request"]["id"]
    fake_daemon.claim_slot(rid)
    market(claimed)
    named = set(re.findall(r"https?://[^\"'\s]+", json.dumps(fake_daemon.app_entries)))
    assert named and fake_daemon.sent
    for url in fake_daemon.sent:
        assert urlsplit(url).netloc == "daemon.test", url
        assert url not in named
    paths = {urlsplit(url).path for url in fake_daemon.sent}
    own_claim = {p for p in paths if re.fullmatch(r"/api/v1/pair/requests/pr-[0-9a-f]+/token", p)}
    assert paths - own_claim <= {"/api/admin/v1", "/api/admin/v1/hub", "/api/admin/v1/hub-claim",
                                 "/api/admin/v1/pairing-requests", "/api/v1/pair/requests", "/health"}


def test_nothing_is_armed_until_approve_and_the_arm_body_is_built_from_the_entry(claimed, fake_daemon, entries):
    market(claimed)
    assert fake_daemon.armed == []
    reply = arm(claimed, {"catalog_app_id": "test.example.notes", "scopes": ["sessions"],
                          "origin": "https://notes.example.test"})
    assert reply.status_code == 201, reply.text
    data = reply.json()
    assert data["daemon_url"] == "https://daemon.test" and data["name"] == "Synthetic notes"
    slot = data["request"]
    assert slot["state"] == "armed" and slot["needs"] == "claim" and slot["armed"] is True
    assert slot["origin"] == "https://notes.example.test" and slot["match_code"] == ""
    assert slot["disclosures"][0]["scope"] == "sessions"
    assert fake_daemon.armed == [{"catalog_app_id": "test.example.notes", "scopes": ["sessions"],
                                  "name": "Synthetic notes", "origin": "https://notes.example.test"}]
    notes = by_id(market(claimed))["test.example.notes"]
    assert notes["pending_pairing"] is True and [s["id"] for s in notes["slots"]] == [slot["id"]]
    assert_no_secrets(claimed.bodies, fake_daemon.hub_token)


def test_elevated_scopes_need_the_acknowledgement_before_anything_is_armed(claimed, fake_daemon, entries):
    body = {"catalog_app_id": "test.example.ops", "scopes": ["sessions", "homelab"],
            "origin": "https://ops2.example.test"}
    for extra in ({}, {"acknowledge_elevated": False}):
        reply = arm(claimed, {**body, **extra})
        assert reply.status_code == 400 and reply.json()["error"]["code"] == "acknowledgement_required"
    assert arm(claimed, {**body, "acknowledge_elevated": "yes"}).status_code == 400
    assert fake_daemon.armed == []
    reply = arm(claimed, {**body, "acknowledge_elevated": True})
    assert reply.status_code == 201, reply.text
    assert fake_daemon.armed == [{"catalog_app_id": "test.example.ops", "scopes": ["sessions", "homelab"],
                                  "name": "Synthetic ops", "origin": "https://ops2.example.test",
                                  "acknowledge_elevated": True}]
    assert reply.json()["request"]["elevated"] == ["homelab"]


@pytest.mark.parametrize(("body", "status"), [
    ({"catalog_app_id": "test.example.unlisted", "scopes": ["sessions"]}, 404),
    ({"catalog_app_id": "test.example.native", "scopes": ["sessions", "approvals"],
      "acknowledge_elevated": True}, 409),
    ({"catalog_app_id": "test.example.native", "scopes": []}, 409),
    ({"catalog_app_id": "test.example.native", "scopes": ["sessions"], "origin": "https://evil.example.test"}, 400),
    ({"catalog_app_id": "test.example.notes", "scopes": ["sessions"]}, 400),
    ({"catalog_app_id": "test.example.notes", "scopes": ["sessions"], "origin": "https://evil.example.test"}, 400),
    ({"catalog_app_id": "test.example.native", "scopes": ["sessions"], "name": "Other"}, 400),
    ({"catalog_app_id": "test.example.native", "scopes": "sessions"}, 400),
    ({"catalog_app_id": ["test.example.native"], "scopes": ["sessions"]}, 400),
    ([], 400),
])
def test_arm_never_widens_or_redirects_what_the_entry_declares(claimed, fake_daemon, entries, body, status):
    reply = arm(claimed, body)
    assert reply.status_code == status, reply.text
    assert fake_daemon.armed == []


def test_arm_needs_a_same_origin_write(claimed, fake_daemon, entries):
    body = {"catalog_app_id": "test.example.native", "scopes": ["sessions"]}
    assert arm(claimed, body, headers={"X-Hub-Request": "1"}).status_code == 403
    assert arm(claimed, body, headers={"Origin": "http://" + HUB_HOST}).status_code == 403
    assert arm(claimed, body, headers={"Origin": "https://evil.example.test", "X-Hub-Request": "1"}).status_code == 403
    assert claimed._keep(claimed.client.post("/api/marketplace/arm", content=b"x" * 5000,
                                             headers=ORIGIN)).status_code == 413
    assert fake_daemon.armed == []


def test_a_native_claim_is_confirmed_with_its_match_code_and_a_slot_is_withdrawn(claimed, fake_daemon, entries):
    rid = arm(claimed, {"catalog_app_id": "test.example.native", "scopes": ["sessions"]}).json()["request"]["id"]
    native = by_id(market(claimed))["test.example.native"]
    assert native["pending_pairing"] is False and native["unverified_origin"]["pending_pairing"] is True
    assert native["slots"][0]["state"] == "armed" and native["slots"][0]["match_code"] == ""
    code = fake_daemon.claim_slot(rid)["match_code"]
    slot = by_id(market(claimed))["test.example.native"]["slots"][0]
    assert slot["state"] == "claimed" and slot["needs"] == "confirm" and slot["match_code"] == code
    confirmed = claimed._keep(claimed.client.post(f"/api/pairing/{rid}/confirm", json={"match": code},
                                                  headers=ORIGIN))
    assert confirmed.status_code == 200 and confirmed.json()["state"] == "approved", confirmed.text
    assert by_id(market(claimed))["test.example.native"]["slots"][0]["needs"] == "redeem"

    other = arm(claimed, {"catalog_app_id": "test.example.native", "scopes": ["sessions"]}).json()["request"]["id"]
    withdrawn = claimed._keep(claimed.client.post(f"/api/pairing/{other}/deny", json={"confirm": True},
                                                  headers=ORIGIN))
    assert withdrawn.status_code == 200 and withdrawn.json()["state"] == "denied"
    assert [s["id"] for s in by_id(market(claimed))["test.example.native"]["slots"]] == [rid]
    assert_no_secrets(claimed.bodies, fake_daemon.hub_token)


def test_an_invalid_entries_file_shows_the_named_error_with_the_last_good_entries(claimed, fake_daemon, entries):
    good = market(claimed)
    fake_daemon.inventory_error = "hub.entries: invalid JSON"
    data = market(claimed)
    assert data["state"] == "error" and data["error"] == "hub.entries: invalid JSON"
    assert data["stale"] is True and data["fetched_at"] == good["fetched_at"]
    assert [e["catalog_app_id"] for e in data["entries"]] == [e["catalog_app_id"] for e in good["entries"]]
    refused = arm(claimed, {"catalog_app_id": "test.example.native", "scopes": ["sessions"]})
    assert refused.status_code == 409 and "hub.entries: invalid JSON" in refused.json()["detail"]
    assert fake_daemon.armed == []


def test_without_a_last_good_inventory_the_error_shows_no_entries_at_all(hub, fake_daemon, entries):
    hub.sign_in()
    fake_daemon.inventory_error = "entries[0].app.app_id: duplicate id"
    fake_daemon.approve(hub.post("/api/claim").json()["request_id"])
    hub.wait_for("claimed")
    data = market(hub)
    assert data["state"] == "error" and data["error"] == "entries[0].app.app_id: duplicate id"
    assert data["entries"] is None and data["stale"] is False


def test_no_unvalidated_entry_is_ever_shown(claimed, fake_daemon, entries):
    good = manifest_entry("test.example.good", "Synthetic good")
    bad = []
    for change in (lambda e: e.update(verified=True), lambda e: e.update(state="trusted"),
                   lambda e: e.update(catalog_app_id="test.example.other"),
                   lambda e: e["manifest"]["app"].update(scopes=[]),
                   lambda e: e["manifest"]["app"].update(scopes=[{"scope": "admin", "risk_tier": "elevated"}]),
                   lambda e: e["manifest"]["app"].update(browser_origins=["http://plain.example.test"]),
                   lambda e: e.pop("manifest"), lambda e: e.update(catalog_app_id="Bad Id")):
        row = manifest_entry("test.example.bad", "Synthetic bad")
        change(row)
        bad.append(row)
    fake_daemon.app_entries = [good, *bad, "not an object"]
    data = market(claimed)
    assert [e["catalog_app_id"] for e in data["entries"]] == ["test.example.good"]
    assert data["withheld"] == len(bad) + 1
    assert "Synthetic bad" not in claimed.bodies[-1]
    assert arm(claimed, {"catalog_app_id": "test.example.bad", "scopes": ["sessions"]}).status_code == 404


def test_token_shaped_entry_text_and_daemon_failures(claimed, fake_daemon, entries):
    fake_daemon.app_entries[0]["manifest"]["app"]["summary"] = "ha-" + "x" * 20
    fake_daemon.pairing_list_error = (500, {"detail": "boom"})
    data = market(claimed)
    notes = by_id(data)["test.example.notes"]
    assert notes["summary"] == "[withheld: token-shaped]" and notes["slots"] == []
    assert data["slots_error"] == "Armed slots could not be read (HTTP 500)."
    fake_daemon.down = True
    down = market(claimed)
    assert down["state"] == "error" and down["entries"] and down["stale"]
    assert arm(claimed, {"catalog_app_id": "test.example.native", "scopes": ["sessions"]}).status_code == 503
    assert_no_secrets(claimed.bodies, fake_daemon.hub_token)


def test_only_an_inline_raster_icon_is_kept():
    def icon(value):
        return entry_view(manifest_entry("test.example.icon", "Icon", icon=value))["icon"]
    assert icon(ICON) == ICON
    for value in ("https://icons.example.invalid/a.png", "data:image/svg+xml;base64,PHN2Zz4=",
                  "data:text/html;base64,PGI+", "javascript:alert(1)", "//icons.example.invalid/a.png", 7,
                  "data:image/png;base64," + "A" * 70000):
        assert icon(value) == ""


def test_marketplace_needs_a_claimed_hub_and_a_signed_in_owner(hub, fake_daemon, entries):
    assert hub.get("/api/marketplace").status_code == 401
    hub.sign_in()
    assert hub.get("/api/marketplace").status_code == 409
    assert arm(hub, {"catalog_app_id": "test.example.native", "scopes": ["sessions"]}).status_code == 409


def test_a_token_shaped_entry_name_never_becomes_the_slot_name(claimed, fake_daemon, entries):
    fake_daemon.app_entries[1]["manifest"]["app"]["display_name"] = fake_daemon.hub_token
    reply = arm(claimed, {"catalog_app_id": "test.example.native", "scopes": ["sessions"]})
    assert reply.status_code == 201, reply.text
    assert fake_daemon.armed[0]["name"] == "[withheld: token-shaped]"
    assert_no_secrets(claimed.bodies, fake_daemon.hub_token)
