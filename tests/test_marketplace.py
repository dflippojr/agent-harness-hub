"""Marketplace: local entries, the consent-gated arm, native confirm, withdraw, last good entries and secrecy."""
import asyncio
import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit

import pytest

from harness_hub.views import SCOPE_DISCLOSURES, entry_digest, entry_view

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
    if isinstance(body, dict) and "consent_digest" not in body:
        listed = claimed.get("/api/marketplace")
        if listed.status_code == 200:
            entry = by_id(listed.json()).get(body.get("catalog_app_id")) if isinstance(
                body.get("catalog_app_id"), str) else None
            if entry:
                body = {**body, "consent_digest": entry["consent_digest"]}
    return claimed._keep(claimed.client.post("/api/marketplace/arm", json=body,
                                             headers=ORIGIN if headers is None else headers))


def test_entries_show_display_fields_state_and_unverified_origin_only(claimed, fake_daemon, entries):
    data = market(claimed)
    assert data["state"] == "ready" and data["error"] == "" and not data["stale"] and data["withheld"] == 0
    assert data["reachable"] is True
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
                          "slots", "consent_digest"}
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
    assert slot["origin"] == "https://notes.example.test" and "match_code" not in slot
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


@pytest.mark.parametrize("app_id,origin", [("test.example.native", ""),
                                         ("test.example.notes", "https://notes.example.test")])
def test_a_claim_waits_for_typed_confirmation_and_a_slot_is_withdrawn(claimed, fake_daemon, entries, app_id, origin):
    body = {"catalog_app_id": app_id, "scopes": ["sessions"], **({"origin": origin} if origin else {})}
    rid = arm(claimed, body).json()["request"]["id"]
    entry = by_id(market(claimed))[app_id]
    assert entry["pending_pairing"] is bool(origin)
    assert entry["unverified_origin"]["pending_pairing"] is not bool(origin)
    assert entry["slots"][0]["state"] == "armed"
    code = fake_daemon.claim_slot(rid, origin)["match_code"]  # from the App's screen, never from an owner view
    slot = by_id(market(claimed))[app_id]["slots"][0]
    assert slot["state"] == "claimed" and slot["needs"] == "confirm" and "match_code" not in slot
    assert code not in claimed.bodies[-1]
    confirmed = claimed._keep(claimed.client.post(f"/api/pairing/{rid}/confirm", json={"match": code},
                                                  headers=ORIGIN))
    assert confirmed.status_code == 200 and confirmed.json()["state"] == "approved", confirmed.text
    assert by_id(market(claimed))[app_id]["slots"][0]["needs"] == "redeem"

    other = arm(claimed, body).json()["request"]["id"]
    withdrawn = claimed._keep(claimed.client.post(f"/api/pairing/{other}/deny", json={"confirm": True},
                                                  headers=ORIGIN))
    assert withdrawn.status_code == 200 and withdrawn.json()["state"] == "denied"
    assert [s["id"] for s in by_id(market(claimed))[app_id]["slots"]] == [rid]
    assert_no_secrets(claimed.bodies, fake_daemon.hub_token)


@pytest.mark.parametrize("state", ["approved", "redeemed"])
@pytest.mark.parametrize("first_page", ["/api/marketplace", "/api/pairing"])
def test_browser_claim_skipping_confirmation_warns_on_both_pages(claimed, fake_daemon, entries, state, first_page):
    rid = arm(claimed, {"catalog_app_id": "test.example.notes", "scopes": ["sessions"],
                        "origin": "https://notes.example.test"}).json()["request"]["id"]
    # Simulate the legacy daemon: claim and possibly redeem before the next owner poll, without a confirm.
    row = fake_daemon.claim_slot(rid, "https://notes.example.test")
    code = row["match_code"]
    row.update(state=state, key_id="k-legacy" if state == "redeemed" else "")
    rejected = claimed._keep(claimed.client.post(f"/api/pairing/{rid}/confirm", json={"match": code}, headers=ORIGIN))
    assert rejected.status_code == 409  # a refused confirmation must not suppress the warning
    warning = "This daemon approved the claim without confirmation; update the daemon."
    for page in (first_page, "/api/marketplace", "/api/pairing", first_page):
        reply = claimed.get(page)
        assert reply.status_code == 200 and reply.json()["confirmation_warning"] == warning
        assert code not in reply.text and "match_code" not in reply.text


@pytest.mark.parametrize("origin,app_id", [("", "test.example.native"),
                                         ("https://notes.example.test", "test.example.notes")])
def test_successful_confirmation_between_polls_does_not_warn(claimed, fake_daemon, entries, origin, app_id):
    rid = arm(claimed, {"catalog_app_id": app_id, "scopes": ["sessions"], "origin": origin}).json()["request"]["id"]
    code = fake_daemon.claim_slot(rid, origin)["match_code"]
    # Pairing requests can confirm from another tab before Marketplace ever polls the claimed state.
    confirmed = claimed._keep(claimed.client.post(f"/api/pairing/{rid}/confirm", json={"match": code}, headers=ORIGIN))
    assert confirmed.status_code == 200
    fake_daemon.pairing_rows[rid].update(state="redeemed", key_id="k-confirmed")
    assert market(claimed)["confirmation_warning"] == ""
    assert claimed.get("/api/pairing").json()["confirmation_warning"] == ""


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
    assert down["state"] == "error" and down["entries"] and down["stale"] and down["reachable"] is False
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
    reply = arm(claimed, {"catalog_app_id": "test.example.native", "scopes": ["sessions"],
                          "consent_digest": entry_digest(entry_view(fake_daemon.app_entries[1]))})
    assert reply.status_code == 201, reply.text
    assert fake_daemon.armed[0]["name"] == "[withheld: token-shaped]"
    assert_no_secrets(claimed.bodies, fake_daemon.hub_token)


@pytest.mark.parametrize("field", ["display_name", "summary", "version", "publisher", "icon", "justification"])
def test_consent_rejects_changed_identity(claimed, fake_daemon, entries, field):
    shown = by_id(market(claimed))["test.example.native"]
    app = entries[1]["manifest"]["app"]
    if field == "publisher":
        app[field]["name"] = "Different publisher"
    elif field == "justification":
        app["scopes"][0][field] = "Different publisher reason"
    else:
        app[field] = ICON if field == "icon" else "Different identity"
    reply = arm(claimed, {"catalog_app_id": shown["catalog_app_id"], "scopes": ["sessions"],
                          "consent_digest": shown["consent_digest"]})
    assert reply.status_code == 409 and "entry changed" in reply.json()["detail"]
    assert fake_daemon.armed == []


def test_consent_digest_is_required_and_does_not_include_live_slots(claimed, fake_daemon, entries):
    shown = by_id(market(claimed))["test.example.native"]
    assert entry_digest({**shown, "slots": [{"id": "pr-other"}]}) == shown["consent_digest"]
    reply = claimed._keep(claimed.client.post("/api/marketplace/arm", headers=ORIGIN,
                                             json={"catalog_app_id": shown["catalog_app_id"], "scopes": ["sessions"]}))
    assert reply.status_code == 409 and fake_daemon.armed == []


@pytest.mark.parametrize("binding", ["token_origin", "long_origin", "many_origins", "many_scopes", "token_scope"])
def test_lossy_bindings_withhold_entire_entry(claimed, fake_daemon, entries, binding):
    app = entries[1]["manifest"]["app"]
    if binding == "token_origin":
        app["browser_origins"] = ["https://ha-notes.example", "https://ha-tasks.example"]
    elif binding == "long_origin":
        app["browser_origins"] = ["https://" + "a" * 290 + ".example.test"]
    elif binding == "many_origins":
        app["browser_origins"] = [f"https://app{i}.test" for i in range(9)]
    elif binding == "many_scopes":
        app["scopes"] = [{"scope": f"scope{i}"} for i in range(17)]
    else:
        app["scopes"] = [{"scope": "invalid-scope"}]
    data = market(claimed)
    assert data["withheld"] == 1 and "test.example.native" not in by_id(data)
    assert arm(claimed, {"catalog_app_id": "test.example.native", "scopes": ["sessions"]}).status_code == 404
    assert fake_daemon.armed == []


def test_raw_origins_are_normalized_like_the_daemon(claimed, fake_daemon, entries):
    entries[0]["manifest"]["app"]["browser_origins"] = ["https://NOTES.example.test:443/"]
    reply = arm(claimed, {"catalog_app_id": "test.example.notes", "scopes": ["sessions"],
                          "origin": "https://notes.example.test/"})
    assert reply.status_code == 201, reply.text
    assert fake_daemon.armed[0]["origin"] == "https://notes.example.test"


def test_publisher_controls_are_stripped_before_display_cap_and_daemon_name(claimed, fake_daemon, entries):
    app = entries[1]["manifest"]["app"]
    controls = "\u202e\u2066\u2067\u2068\u2069\u200b\x00\n\t"
    app.update(display_name=controls * 20 + "Visible name", summary=controls + "Summary", version=controls + "1.0")
    app["publisher"].update(name=controls + "Publisher", id=controls + "publisher-id")
    app["scopes"][0]["justification"] = controls + "Reason"
    shown = by_id(market(claimed))["test.example.native"]
    assert shown["name"] == "Visible name" and shown["summary"] == "Summary" and shown["version"] == "1.0"
    assert shown["publisher"] == {"name": "Publisher", "id": "publisher-id"}
    assert shown["scopes"][0]["justification"] == "Reason"
    reply = arm(claimed, {"catalog_app_id": "test.example.native", "scopes": ["sessions"]})
    assert reply.status_code == 201, reply.text
    assert fake_daemon.armed[0]["name"] == "Visible name"


@pytest.mark.parametrize("scope", list(SCOPE_DISCLOSURES))
def test_any_changed_disclosure_is_denied_and_requires_fresh_consent(claimed, fake_daemon, entries, scope):
    entries[1] = manifest_entry("test.example.native", "Synthetic native", scopes=(scope,))
    original = by_id(market(claimed))["test.example.native"]
    body = {"catalog_app_id": original["catalog_app_id"], "scopes": [scope],
            "consent_digest": original["consent_digest"], "acknowledge_elevated": True}
    fake_daemon.arm_disclosures[scope] += " More daemon disclosure."
    reply = arm(claimed, body)
    assert reply.status_code == 409, reply.text
    assert "describes these scopes differently" in reply.json()["detail"]
    assert "request" not in reply.json() and "pr-" not in reply.text
    rid = next(iter(fake_daemon.pairing_rows))
    assert fake_daemon.pairing_rows[rid]["state"] == "denied"
    assert fake_daemon.pairing_bodies == [("deny", rid, None)]
    reviewed = reply.json()["consent_entry"]
    assert reviewed["scopes"][0]["text"] == fake_daemon.arm_disclosures[scope]
    assert reviewed["consent_digest"] != original["consent_digest"]
    assert by_id(market(claimed))[original["catalog_app_id"]]["scopes"] == reviewed["scopes"]
    # A stale or client-forged digest cannot approve the newly discovered daemon wording.
    assert arm(claimed, body).status_code == 409
    assert len(fake_daemon.armed) == 1
    fresh = by_id(market(claimed))[original["catalog_app_id"]]
    body["consent_digest"] = fresh["consent_digest"]
    success = arm(claimed, body)
    assert success.status_code == 201, success.text
    assert success.json()["request"]["disclosures"][0]["text"] == fake_daemon.arm_disclosures[scope]


def test_addon_scope_requires_ack_and_never_arms_against_blank_hub_text(claimed, fake_daemon, entries):
    entries[1] = manifest_entry("test.example.native", "Synthetic native", scopes=("files:write",))
    fake_daemon.arm_disclosures["files:write"] = "Read and write any file in your projects."
    original = by_id(market(claimed))["test.example.native"]
    assert original["scopes"][0]["text"] == ""
    body = {"catalog_app_id": original["catalog_app_id"], "scopes": ["files:write"],
            "consent_digest": original["consent_digest"]}
    assert arm(claimed, body).status_code == 400 and fake_daemon.armed == []
    reply = arm(claimed, {**body, "acknowledge_elevated": True})
    assert reply.status_code == 409 and "pr-" not in reply.text
    assert all(row["state"] == "denied" for row in fake_daemon.pairing_rows.values())
    reviewed = by_id(market(claimed))[original["catalog_app_id"]]
    assert reviewed["scopes"][0]["text"] == fake_daemon.arm_disclosures["files:write"]
    success = arm(claimed, {**body, "consent_digest": reviewed["consent_digest"]})
    assert success.status_code == 201, success.text


@pytest.mark.parametrize("malformed", [None, [], [{"scope": "sessions", "tier": "standard", "text": ""}],
    [{"scope": "sessions", "tier": "unknown", "text": "Different"}],
    [{"scope": "approvals", "tier": "elevated", "text": "Different"}],
    [{"scope": "sessions", "tier": "standard", "text": "ha-" + "x" * 20}],
    [{"scope": "sessions", "tier": "standard", "text": "x" * 4001}],
    [{"scope": ["sessions"], "tier": "standard", "text": "Different"}],
    [{"scope": "sessions", "tier": "standard", "text": "Different"}] * 2])
def test_unsafe_disclosures_are_denied_and_never_offered_for_consent(claimed, fake_daemon, entries, malformed):
    original_view = fake_daemon.pairing_view
    fake_daemon.pairing_view = lambda row: {**original_view(row), "disclosures": malformed}
    reply = arm(claimed, {"catalog_app_id": "test.example.native", "scopes": ["sessions"]})
    assert reply.status_code == 409 and "consent_entry" not in reply.json(), reply.text
    assert all(row["state"] == "denied" for row in fake_daemon.pairing_rows.values())
    assert "pr-" not in reply.text
    assert_no_secrets(claimed.bodies, fake_daemon.hub_token)


def test_runtime_compares_raw_tier_and_does_not_mask_daemon_tier_drift(claimed, fake_daemon, entries):
    original_view = fake_daemon.pairing_view
    def changed_tier(row):
        result = original_view(row)
        result["disclosures"][0]["tier"] = "elevated"
        return result
    fake_daemon.pairing_view = changed_tier
    reply = arm(claimed, {"catalog_app_id": "test.example.native", "scopes": ["sessions"]})
    assert reply.status_code == 409, reply.text
    reviewed = reply.json()["consent_entry"]
    assert reviewed["scopes"][0]["tier"] == "elevated" and reviewed["elevated"] == ["sessions"]
    assert arm(claimed, {"catalog_app_id": "test.example.native", "scopes": ["sessions"]}).status_code == 400
    assert arm(claimed, {"catalog_app_id": "test.example.native", "scopes": ["sessions"],
                         "acknowledge_elevated": True}).status_code == 201


def test_daemon_disclosure_copy_is_discarded_when_the_entry_identity_changes(claimed, fake_daemon, entries):
    fake_daemon.arm_disclosures["sessions"] += " More daemon disclosure."
    reply = arm(claimed, {"catalog_app_id": "test.example.native", "scopes": ["sessions"]})
    assert reply.status_code == 409 and "consent_entry" in reply.json()
    entries[1]["manifest"]["app"]["publisher"]["name"] = "New publisher"
    shown = by_id(market(claimed))["test.example.native"]
    assert shown["scopes"][0]["text"] == SCOPE_DISCLOSURES["sessions"]
    assert arm(claimed, {"catalog_app_id": shown["catalog_app_id"], "scopes": ["sessions"]}).status_code == 409


@pytest.mark.parametrize("failed", ["refused", "unreachable"])
def test_failed_withdrawal_never_returns_a_slot_or_replacement_consent(claimed, fake_daemon, entries, failed):
    fake_daemon.arm_disclosures["sessions"] += " Changed."
    fake_daemon.pairing_deny_error = failed
    reply = arm(claimed, {"catalog_app_id": "test.example.native", "scopes": ["sessions"]})
    assert reply.status_code == (502 if failed == "refused" else 503), reply.text
    assert "consent_entry" not in reply.json() and "request" not in reply.json() and "pr-" not in reply.text


def test_new_daemon_elevated_scope_can_be_reviewed_instead_of_leaving_owner_stuck(
        claimed, fake_daemon, entries, monkeypatch):
    from . import marketplace_fake, pairing_fake
    monkeypatch.setattr(marketplace_fake, "ELEVATED", marketplace_fake.ELEVATED | {"sessions"})
    monkeypatch.setattr(pairing_fake, "ELEVATED", pairing_fake.ELEVATED | {"sessions"})
    reply = arm(claimed, {"catalog_app_id": "test.example.native", "scopes": ["sessions"]})
    assert reply.status_code == 409, reply.text
    assert reply.json()["consent_entry"]["acknowledgement_required"] is True
    assert fake_daemon.pairing_rows == {}
    reply = arm(claimed, {"catalog_app_id": "test.example.native", "scopes": ["sessions"],
                          "acknowledge_elevated": True})
    assert reply.status_code == 409 and reply.json()["consent_entry"]["elevated"] == ["sessions"]
    assert all(row["state"] == "denied" for row in fake_daemon.pairing_rows.values())
    success = arm(claimed, {"catalog_app_id": "test.example.native", "scopes": ["sessions"],
                            "acknowledge_elevated": True})
    assert success.status_code == 201, success.text


def test_concurrent_tabs_cannot_read_a_slot_until_disclosure_review_finishes(claimed, fake_daemon, entries):
    shown = by_id(market(claimed))["test.example.native"]
    fake_daemon.arm_disclosures["sessions"] += " New daemon text."
    denying, release = threading.Event(), threading.Event()
    async def pause_deny():
        denying.set()
        assert await asyncio.to_thread(release.wait, 5), "test did not release the withdrawal"
    fake_daemon.before_pairing_deny = pause_deny
    with ThreadPoolExecutor(max_workers=3) as executor:
        armed = executor.submit(arm, claimed, {"catalog_app_id": shown["catalog_app_id"], "scopes": ["sessions"],
                                              "consent_digest": shown["consent_digest"]})
        try:
            assert denying.wait(5), "the new slot did not reach disclosure withdrawal"
            rid = next(iter(fake_daemon.pairing_rows))
            assert fake_daemon.pairing_rows[rid]["state"] == "armed"
            polls = [executor.submit(claimed.get, path) for path in ("/api/marketplace", "/api/pairing")]
            for poll in polls:
                with pytest.raises(TimeoutError):
                    poll.result(timeout=0.1)
        finally:
            release.set()
        response = armed.result(timeout=5)
        assert response.status_code == 409 and rid not in response.text
        marketplace, pairing = [poll.result(timeout=5).json() for poll in polls]
        assert by_id(marketplace)[shown["catalog_app_id"]]["slots"] == []
        assert next(row for row in pairing["requests"] if row["id"] == rid)["state"] == "denied"
