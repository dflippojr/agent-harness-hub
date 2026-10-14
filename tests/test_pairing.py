"""Pairing requests: listing, match-code decisions, elevated acknowledgement, Hub claims and response secrecy."""
import pytest

from harness_hub.views import pairing_request_view

from .conftest import HUB_HOST, assert_no_secrets

ORIGIN = {"Origin": "http://" + HUB_HOST, "X-Hub-Request": "1"}


def post(claimed, rid, action, body, headers=None):
    reply = claimed.client.post(f"/api/pairing/{rid}/{action}", json=body,
                                headers=ORIGIN if headers is None else headers)
    return claimed._keep(reply)


def rows(claimed):
    reply = claimed.get("/api/pairing")
    assert reply.status_code == 200, reply.text
    return {row["id"]: row for row in reply.json()["requests"]}


def sent(fake, operation):
    return [entry for entry in fake.pairing_bodies if entry[0] == operation]


def test_list_is_allowlisted_newest_first_without_codes_with_disclosures_and_hub_claims(claimed, fake_daemon):
    plain = fake_daemon.add_pairing("Synthetic notes", ["sessions"], catalog_app_id="test.example.notes")
    elevated = fake_daemon.add_pairing("Synthetic ops", ["sessions", "homelab"], origin="")
    armed = fake_daemon.add_pairing("Armed slot", ["sessions"], state="armed", catalog_app_id="test.example.armed")
    claimed_slot = fake_daemon.add_pairing("Claimed slot", ["sessions"], state="claimed", origin="")
    reply = claimed.get("/api/pairing")
    data = reply.json()
    assert isinstance(data["now"], float)
    ordered = [row["created_at"] for row in data["requests"]]
    assert ordered == sorted(ordered, reverse=True)
    shown = {row["id"]: row for row in data["requests"]}
    assert all("match_code" not in row for row in data["requests"])
    assert shown[plain]["needs"] == "approve" and shown[plain]["catalog_app_id"] == "test.example.notes"
    assert shown[plain]["origin"] == "https://app.test" and shown[plain]["browser"] is True
    assert shown[elevated]["origin"] == "" and shown[elevated]["elevated"] == ["homelab"]
    assert [d["tier"] for d in shown[elevated]["disclosures"]] == ["standard", "elevated"]
    assert shown[elevated]["disclosures"][1]["text"].startswith("This app's tasks can **read your homelab")
    assert shown[armed]["needs"] == "claim"
    assert shown[claimed_slot]["needs"] == "confirm"
    hub = [row for row in data["requests"] if row["kind"] == "hub"]
    assert hub and all("match_code" not in row for row in hub)
    assert set(shown[plain]) == {"id", "kind", "state", "needs", "name", "catalog_app_id", "origin", "browser",
                                 "armed", "scopes", "elevated", "disclosures", "key_id",
                                 "created_at", "expires_at", "approved_at", "finished_at"}
    assert_no_secrets(claimed.bodies, fake_daemon.hub_token)


def test_approve_with_the_typed_code_and_a_wrong_code_shows_the_daemons_400(claimed, fake_daemon):
    rid = fake_daemon.add_pairing("Synthetic notes", ["sessions"])
    code = fake_daemon.pairing_rows[rid]["match_code"]
    wrong = "000000" if code != "000000" else "111111"
    reply = post(claimed, rid, "approve", {"match": wrong})
    assert reply.status_code == 400 and reply.json()["detail"] == "the match code does not match the one the App shows"
    assert rows(claimed)[rid]["state"] == "pending"
    reply = post(claimed, rid, "approve", {"match": f" {code[:3]} {code[3:]} "})
    assert reply.status_code == 200, reply.text
    assert reply.json()["state"] == "approved" and reply.json()["needs"] == "redeem"
    assert "match_code" not in reply.json()
    assert sent(fake_daemon, "approve")[-1] == ("approve", rid, {"match": f"{code[:3]} {code[3:]}"})
    again = post(claimed, rid, "approve", {"match": code})
    assert again.status_code == 409 and "approved" in again.json()["detail"]
    assert_no_secrets(claimed.bodies, fake_daemon.hub_token)


def test_elevated_scopes_are_never_approved_without_the_acknowledgement(claimed, fake_daemon):
    rid = fake_daemon.add_pairing("Synthetic ops", ["sessions", "approvals"])
    code = fake_daemon.pairing_rows[rid]["match_code"]
    for body in ({"match": code}, {"match": code, "acknowledge_elevated": False}):
        reply = post(claimed, rid, "approve", body)
        assert reply.status_code == 400 and reply.json()["error"]["code"] == "acknowledgement_required"
    assert sent(fake_daemon, "approve") == []
    assert post(claimed, rid, "approve", {"match": code, "acknowledge_elevated": "yes"}).status_code == 400
    reply = post(claimed, rid, "approve", {"match": code, "acknowledge_elevated": True})
    assert reply.status_code == 200 and reply.json()["state"] == "approved"
    assert sent(fake_daemon, "approve") == [("approve", rid, {"match": code, "acknowledge_elevated": True})]


def test_the_hub_flags_elevated_scopes_even_when_the_daemon_does_not():
    shown = pairing_request_view({"id": "pr-1", "kind": "app", "state": "pending", "scopes": ["sessions", "homelab"],
                                  "elevated": [], "match_code": "123456",
                                  "disclosures": [{"scope": "homelab", "tier": "standard", "text": "t"}]})
    assert shown["elevated"] == ["homelab"] and shown["disclosures"][0]["tier"] == "elevated"
    hub = pairing_request_view({"id": "pr-2", "kind": "hub", "state": "pending", "match_code": "123456"})
    assert "match_code" not in hub
    odd = pairing_request_view({"id": "pr-3", "kind": "app", "state": "approved", "match_code": "<b>1</b>",
                                "needs": "anything", "disclosures": "x", "scopes": "sessions"})
    assert "match_code" not in odd and odd["needs"] == "" and odd["disclosures"] == [] and odd["scopes"] == []


@pytest.mark.parametrize("origin", ["", "https://app.test"])
def test_confirm_an_app_claim_with_its_code(claimed, fake_daemon, origin):
    rid = fake_daemon.add_pairing("Claimed slot", ["sessions"], state="claimed", origin=origin)
    pending = fake_daemon.add_pairing("Synthetic notes", ["sessions"])
    code = fake_daemon.pairing_rows[rid]["match_code"]
    assert rows(claimed)[rid]["needs"] == "confirm"
    for match in ("", "   ", "999999" if code != "999999" else "888888"):
        refused = post(claimed, rid, "confirm", {"match": match})
        assert refused.status_code == 400 and code not in refused.text
        assert rows(claimed)[rid]["state"] == "claimed"
    assert refused.json()["error"]["code"] == "match_mismatch"
    assert post(claimed, pending, "confirm", {"match": fake_daemon.pairing_rows[pending]["match_code"]}
                ).status_code == 409
    reply = post(claimed, rid, "confirm", {"match": code})
    assert reply.status_code == 200 and reply.json()["state"] == "approved"
    assert sent(fake_daemon, "confirm")[-1] == ("confirm", rid, {"match": code})


def test_deny_needs_page_confirmation_and_wrong_state_or_unknown_ids_answer_409_and_404(claimed, fake_daemon):
    rid = fake_daemon.add_pairing("Synthetic notes", ["sessions"])
    for body in ({}, {"confirm": False}, {"confirm": "true"}):
        assert post(claimed, rid, "deny", body).status_code == 400
    assert sent(fake_daemon, "deny") == []
    reply = post(claimed, rid, "deny", {"confirm": True})
    assert reply.status_code == 200 and reply.json()["state"] == "denied"
    assert sent(fake_daemon, "deny") == [("deny", rid, None)]
    assert post(claimed, rid, "deny", {"confirm": True}).status_code == 409
    assert post(claimed, "pr-unknown", "deny", {"confirm": True}).status_code == 404
    armed = fake_daemon.add_pairing("Armed slot", ["sessions"], state="armed")
    assert post(claimed, armed, "deny", {"confirm": True}).json()["state"] == "denied"


def test_hub_claims_are_refused_by_the_daemon_and_carry_no_code(claimed, fake_daemon):
    hub_rid = next(iter(fake_daemon.requests))
    assert post(claimed, hub_rid, "approve", {"match": "123456"}).json()["error"]["code"] == "acknowledgement_required"
    for action, body in (("approve", {"match": "123456", "acknowledge_elevated": True}),
                         ("confirm", {"match": "123456"}), ("deny", {"confirm": True})):
        reply = post(claimed, hub_rid, action, body)
        assert reply.status_code == 403 and "daemon host" in reply.json()["detail"]
    assert "match_code" not in rows(claimed)[hub_rid]


def test_bad_input_never_reaches_the_daemon(claimed, fake_daemon):
    rid = fake_daemon.add_pairing("Synthetic notes", ["sessions"])
    fake_daemon.pairing_calls.clear()
    for match in ("", "   ", "abc123", "1" * 33, 123456, None, "12<b>"):
        assert post(claimed, rid, "approve", {"match": match}).status_code == 400
    for body in ({"match": "123456", "token": "x"}, {"match": "123456", "code_verifier": "x"}, ["123456"]):
        assert post(claimed, rid, "approve", body).status_code == 400
    assert post(claimed, rid, "confirm", {"match": "123456", "acknowledge_elevated": True}).status_code == 400
    for path in (f"{rid}/token", f"{rid}/claim", f"{rid}/redeem", "pr.bad/approve", f"{rid}/approve/extra"):
        reply = claimed._keep(claimed.client.post("/api/pairing/" + path, json={"match": "1"}, headers=ORIGIN))
        assert reply.status_code in (404, 405)
    reply = claimed._keep(claimed.client.post(f"/api/pairing/{rid}/approve", content=b"x" * 1025, headers=ORIGIN))
    assert reply.status_code == 413
    reply = claimed._keep(claimed.client.post(f"/api/pairing/{rid}/approve", content=b"{bad", headers=ORIGIN))
    assert reply.status_code == 400
    assert not fake_daemon.pairing_calls


def test_responses_never_carry_credentials(claimed, fake_daemon):
    fake_daemon.add_pairing("ha-synthetic-looking-name", ["sessions"], origin="https://hp-x.example")
    fake_daemon.add_pairing("Markup <img src=x onerror=alert(1)>", ["sessions"],
                            text={"sessions": "<script>alert(1)</script> **bold**"})
    data = rows(claimed)
    names = {row["name"] for row in data.values()}
    assert "[withheld: token-shaped]" in names and "Markup <img src=x onerror=alert(1)>" in names
    markup = next(row for row in data.values() if row["name"].startswith("Markup"))
    assert markup["disclosures"][0]["text"] == "<script>alert(1)</script> **bold**"
    assert_no_secrets(claimed.bodies, fake_daemon.hub_token)
    fake_daemon.add_pairing(fake_daemon.hub_token, ["sessions"])
    assert claimed.get("/api/pairing").status_code == 500
    assert_no_secrets(claimed.bodies, fake_daemon.hub_token)


def test_an_unreadable_list_fails_closed_without_approving(claimed, fake_daemon):
    rid = fake_daemon.add_pairing("Synthetic ops", ["approvals"])
    fake_daemon.pairing_list_error = (200, {"not": "a list"})
    assert claimed.get("/api/pairing").status_code == 502
    # The elevated check fails closed: no approval is sent when the list cannot be read.
    assert post(claimed, rid, "approve", {"match": "123456"}).status_code == 502
    assert sent(fake_daemon, "approve") == []


@pytest.mark.parametrize("kind", ["app", "hub", "unknown"])
@pytest.mark.parametrize("state", ["pending", "armed", "claimed", "approved", "redeemed", "denied", "expired"])
def test_no_owner_projection_contains_the_request_match_code(kind, state):
    shown = pairing_request_view({"id": "pr-code", "kind": kind, "state": state, "match_code": "482913"})
    assert "match_code" not in shown
    assert "482913" not in str(shown)


@pytest.mark.parametrize("field,value", [("name", "ha-synthetic-looking-name"),
                                        ("origin", "https://hp-printer.example"),
                                        ("catalog_app_id", "com.ha-tools.app")])
@pytest.mark.parametrize("state", ["pending", "claimed"])
def test_withheld_pairing_identity_fields_are_marked_for_host_review(claimed, fake_daemon, field, value, state):
    rid = fake_daemon.add_pairing("Synthetic notes", ["sessions"], state=state)
    fake_daemon.pairing_rows[rid][field] = value
    shown = rows(claimed)[rid]
    assert shown[field] == "[withheld: token-shaped]"
    assert "match_code" not in shown
    assert post(claimed, rid, "deny", {"confirm": True}).json()["state"] == "denied"
