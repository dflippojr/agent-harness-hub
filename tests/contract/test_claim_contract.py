"""Health, the Hub's own claim, the owner API's discovery root and the Hub record (harness_hub/claim.py, daemon.py)."""

from __future__ import annotations

from .targets import HUB_NAME, pkce


def test_health_reports_release_protocols_capabilities_and_config(target):
    health = target.call("GET /health", auth=False, expect=200).body
    assert health["ok"] is True and health["capabilities"]["modules"]["hub"] is True


def test_the_claim_redeems_a_hub_key_and_the_daemon_records_it(target):
    # `target` claimed with the same three calls claim.py makes (start, the owner's approval, redeem).
    discovery = target.call("GET /api/admin/v1", expect=200).body
    assert discovery["hub"]["claimed"] is True
    record = target.call("GET /api/admin/v1/hub-claim", expect=200).body
    assert record["claimed"] is True and record["hub"]["key_id"] == target.key_id
    assert record["hub"]["name"] == HUB_NAME


def test_a_second_claim_is_refused_with_hub_claimed(target):
    _, challenge = pkce()
    refused = target.call("POST /api/v1/pair/requests", body={
        "name": "Another Hub", "kind": "hub", "code_challenge": challenge, "code_challenge_method": "S256"},
        expect=409)
    assert refused.code == "hub_claimed"


def test_redeeming_an_unknown_request_is_404(target):
    verifier, _ = pkce()
    target.call("POST /api/v1/pair/requests/{request_id}/token", {"request_id": "pr-000000000000000000000000"},
                {"code_verifier": verifier}, expect=404)


def test_owner_routes_refuse_a_missing_key_with_401(target):
    target.call("GET /api/admin/v1", auth=False, expect=401)
    target.call("GET /api/admin/v1/keys", auth=False, expect=401)
