"""App pairing requests: the owner's list, approve, arm, confirm and deny (daemon.PAIRING_ROUTES).

The real daemon limits App-initiated requests per source (3 pending, 10 per 10 minutes); the owner's armed slots
don't count. These cases make three App requests between them and deny what they leave pending.
"""

from __future__ import annotations

LIST = "GET /api/admin/v1/pairing-requests"
APPROVE = "POST /api/admin/v1/pairing-requests/{rid}/approve"
CONFIRM = "POST /api/admin/v1/pairing-requests/{rid}/confirm"
DENY = "POST /api/admin/v1/pairing-requests/{rid}/deny"
ARM = "POST /api/admin/v1/pairing-requests"
UNKNOWN = "pr-000000000000000000000000"


def listed(target, rid):
    return next(row for row in target.call(LIST, expect=200).body if row["id"] == rid)


def wrong(code: str) -> str:
    return "000000" if code != "000000" else "111111"


def test_an_app_request_lists_with_its_disclosures_and_the_hub_claim_beside_it(target):
    rid, _ = target.arrange_app_request("Contract listed App", ["sessions"])
    row = listed(target, rid)
    assert row["kind"] == "app" and row["state"] == "pending" and row["needs"] == "approve"
    assert row["disclosures"][0]["scope"] == "sessions" and row["disclosures"][0]["text"]
    assert any(row["kind"] == "hub" for row in target.call(LIST, expect=200).body)
    target.call(DENY, {"rid": rid}, expect=200)


def test_approve_takes_the_apps_code_and_happens_once(target):
    rid, code = target.arrange_app_request("Contract approved App", ["sessions"])
    refused = target.call(APPROVE, {"rid": rid}, {"match": wrong(code)}, expect=400)
    assert "match code" in refused.detail
    approved = target.call(APPROVE, {"rid": rid}, {"match": code}, expect=200).body
    assert approved["state"] == "approved" and approved["needs"] == "redeem"
    target.call(APPROVE, {"rid": rid}, {"match": code}, expect=409)
    withdrawn = target.call(DENY, {"rid": rid}, expect=200).body
    assert withdrawn["state"] == "denied"


def test_elevated_scopes_need_the_acknowledgement(target):
    rid, code = target.arrange_app_request("Contract elevated App", ["sessions", "approvals"])
    assert listed(target, rid)["elevated"] == ["approvals"]
    refused = target.call(APPROVE, {"rid": rid}, {"match": code}, expect=400)
    assert refused.code == "acknowledgement_required"
    approved = target.call(APPROVE, {"rid": rid}, {"match": code, "acknowledge_elevated": True}, expect=200).body
    assert approved["state"] == "approved"
    target.call(DENY, {"rid": rid}, expect=200)


def test_an_armed_slot_is_confirmed_with_the_code_the_app_shows(target):
    slot = target.call(ARM, body={"catalog_app_id": "test.example.slot", "scopes": ["sessions"],
                                  "name": "Contract slot"}, expect=201).body
    assert slot["armed"] is True and slot["state"] == "armed" and slot["needs"] == "claim"
    assert [row["scope"] for row in slot["disclosures"]] == ["sessions"]
    code = target.arrange_app_claims(slot["id"])
    assert listed(target, slot["id"])["needs"] == "confirm"
    target.call(CONFIRM, {"rid": slot["id"]}, {"match": wrong(code)}, expect=400)
    confirmed = target.call(CONFIRM, {"rid": slot["id"]}, {"match": code}, expect=200).body
    assert confirmed["state"] == "approved"
    target.call(CONFIRM, {"rid": slot["id"]}, {"match": code}, expect=409)
    target.call(DENY, {"rid": slot["id"]}, expect=200)


def test_arming_elevated_scopes_needs_the_acknowledgement(target):
    refused = target.call(ARM, body={"catalog_app_id": "test.example.slot", "scopes": ["sessions", "approvals"],
                                     "name": "Contract elevated slot"}, expect=400)
    # Unlike approve, the arm refusal's code is the generic one; only the detail names the acknowledgement.
    assert refused.code == "invalid_request" and "acknowledge_elevated" in refused.detail
    slot = target.call(ARM, body={"catalog_app_id": "test.example.slot", "scopes": ["sessions", "approvals"],
                                  "name": "Contract elevated slot", "acknowledge_elevated": True}, expect=201).body
    assert slot["elevated"] == ["approvals"]
    target.call(DENY, {"rid": slot["id"]}, expect=200)


def test_unknown_requests_are_404_and_hub_claims_are_decided_only_on_the_host(target):
    for key, body in ((APPROVE, {"match": "000000"}), (CONFIRM, {"match": "000000"}), (DENY, None)):
        target.call(key, {"rid": UNKNOWN}, body, expect=404)
    hub_rid = next(row["id"] for row in target.call(LIST, expect=200).body if row["kind"] == "hub")
    assert target.call(DENY, {"rid": hub_rid}, expect=403).code == "host_proof_required"
    assert target.call(APPROVE, {"rid": hub_rid}, {"match": "000000"}, expect=403).code == "host_proof_required"
