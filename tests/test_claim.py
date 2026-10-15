"""The first-run claim flow against the fake daemon: start, match code, host approval, redeem, and every ending."""

from __future__ import annotations

import json
import os
import time

import pytest

from harness_hub.state import STATE_FILE

from .conftest import assert_no_secrets


def state_file(hub) -> dict:
    return json.loads((hub.state_dir / STATE_FILE).read_text(encoding="utf-8"))


def test_full_flow_claims_the_daemon_and_never_shows_a_secret(hub, fake_daemon):
    hub.sign_in()
    started = hub.post("/api/claim")
    assert started.status_code == 201
    claim = started.json()
    row = fake_daemon.latest()
    assert claim["state"] == "pending"
    assert claim["request_id"] == row["id"] and claim["match_code"] == row["match_code"]
    assert claim["approve_command"] == f"harness hub approve {row['id']} --match {row['match_code']}"

    # Starting again while one waits returns the same claim instead of spending the daemon's rate limit.
    assert hub.post("/api/claim").json()["request_id"] == row["id"]
    assert len(fake_daemon.requests) == 1

    fake_daemon.approve(row["id"])
    claimed = hub.wait_for("claimed")
    assert claimed["key_id"] == fake_daemon.hub["key_id"]

    overview = hub.get("/api/overview").json()
    assert overview["reachable"] is True and overview["error"] == ""
    assert overview["ownership"] == {"api_version": "1.26", "compatibility": "supported",
                                     "daemon_has_hub": True, "this_hub": True,
                                     "record": {"name": "Hub", "kind": "native", "key_id": claimed["key_id"],
                                                "claimed_at": fake_daemon.hub["claimed_at"]}}
    assert overview["daemon"]["release"] == "0.9.0"
    assert overview["capabilities"]["modules"] == {"web": False, "hub": True}
    assert overview["health"]["config_revision"] == 3

    saved = state_file(hub)
    assert saved["key"]["token"] == fake_daemon.hub_token
    assert "claim" not in saved
    hub.get("/")
    hub.get("/static/app.mjs")
    hub.get("/api/session")
    assert_no_secrets(hub.bodies, fake_daemon.hub_token, *fake_daemon.minted)


def test_verifier_stays_on_the_server(hub, fake_daemon):
    hub.sign_in()
    hub.post("/api/claim")
    verifier = state_file(hub)["claim"]["verifier"]
    assert 43 <= len(verifier) <= 128
    hub.get("/api/claim")
    hub.get("/api/overview")
    assert_no_secrets(hub.bodies, verifier)


def test_already_claimed_shows_409_and_the_release_command(hub, fake_daemon):
    fake_daemon.claim_elsewhere()
    hub.sign_in()
    refused = hub.post("/api/claim")
    assert refused.status_code == 409
    claim = refused.json()["claim"]
    assert claim["state"] == "refused"
    assert claim["release_command"] == "harness hub release --confirm"
    assert "already has a Hub" in claim["message"]
    assert hub.get("/api/overview").json()["claim"]["state"] == "refused"
    # After `harness hub release --confirm` on the host, a new claim starts.
    fake_daemon.hub = None
    assert hub.post("/api/claim").json()["state"] == "pending"


def test_hub_claimed_after_approval_is_refused(hub, fake_daemon):
    hub.sign_in()
    rid = hub.post("/api/claim").json()["request_id"]
    fake_daemon.approve(rid)
    fake_daemon.claim_elsewhere()
    assert hub.wait_for("refused")["release_command"] == "harness hub release --confirm"
    assert "key" not in state_file(hub)


def test_expired_claim_says_to_start_again(hub, fake_daemon):
    hub.sign_in()
    rid = hub.post("/api/claim").json()["request_id"]
    fake_daemon.expire(rid)
    claim = hub.wait_for("expired")
    assert "expired" in claim["message"] and "Start a new claim" in claim["message"]
    assert hub.post("/api/claim").json()["request_id"] != rid


def test_denied_claim(hub, fake_daemon):
    hub.sign_in()
    rid = hub.post("/api/claim").json()["request_id"]
    fake_daemon.deny(rid)
    assert "denied" in hub.wait_for("denied")["message"]


def test_forget_a_waiting_claim(hub, fake_daemon):
    hub.sign_in()
    hub.post("/api/claim")
    assert hub.delete("/api/claim").json()["state"] == "unclaimed"
    assert "claim" not in state_file(hub)


def test_a_key_that_is_not_a_hub_key_is_discarded(hub, fake_daemon):
    fake_daemon.redeem_role = "app"
    hub.sign_in()
    fake_daemon.approve(hub.post("/api/claim").json()["request_id"])
    assert hub.wait_for("failed")
    assert "key" not in state_file(hub)
    assert_no_secrets(hub.bodies, *fake_daemon.minted)


def test_a_lost_redeem_reply_ends_lost_with_the_release_command(hub, fake_daemon):
    # The daemon commits the key, but its reply times out; the retry is told the request was already redeemed.
    fake_daemon.lose_redeem_replies = 1
    hub.sign_in()
    fake_daemon.approve(hub.post("/api/claim").json()["request_id"])
    claim = hub.wait_for("lost")
    assert claim["message"] == ("The daemon issued this Hub's key, but the reply never arrived. On the daemon host "
                                "run harness hub release --confirm, then start a new claim here.")
    assert claim["release_command"] == "harness hub release --confirm"
    assert hub.get("/api/overview").json()["claim"]["state"] == "lost"
    assert len(fake_daemon.minted) == 1 and fake_daemon.hub is not None
    # The outcome is shown before it is saved; wait for the save rather than race it on the file.
    store = hub.client.app.fastapi.state.store
    deadline = time.monotonic() + 5
    while store.get("outcome") is None and time.monotonic() < deadline:
        time.sleep(0.02)
    assert store.get("outcome")["state"] == "lost"
    assert "key" not in state_file(hub) and state_file(hub)["outcome"]["state"] == "lost"
    # A new claim is refused until the owner releases the daemon's record of the lost key.
    assert hub.post("/api/claim").json()["claim"]["state"] == "refused"
    fake_daemon.release()
    rid = hub.post("/api/claim").json()["request_id"]
    fake_daemon.approve(rid)
    assert hub.wait_for("claimed")["key_id"] == fake_daemon.hub["key_id"]
    assert_no_secrets(hub.bodies, *fake_daemon.minted)


@pytest.mark.parametrize("unreachable", [False, True])
def test_already_redeemed_without_an_unanswered_attempt_fails(hub, fake_daemon, unreachable):
    # A connection that never opened cannot have redeemed anything, so it is no evidence of a lost key.
    hub.sign_in()
    rid = hub.post("/api/claim").json()["request_id"]
    if unreachable:
        fake_daemon.down = True
        redeems = lambda: sum(url.endswith("/token") for url in fake_daemon.sent)  # noqa: E731
        refused = redeems()
        deadline = time.monotonic() + 5
        while redeems() < refused + 2 and time.monotonic() < deadline:
            time.sleep(0.02)
        assert redeems() >= refused + 2 and hub.get("/api/claim").json()["state"] == "pending"
    fake_daemon.requests[rid]["state"] = "redeemed"
    fake_daemon.down = False
    assert "did not complete" in hub.wait_for("failed")["message"]


def test_only_the_redeem_waits_longer_for_the_daemon(claimed, fake_daemon):
    claimed.get("/api/overview")
    redeem = next(path for path in fake_daemon.timeouts if path.endswith("/token"))
    assert fake_daemon.timeouts[redeem] == {"connect": 5.0, "read": 60.0, "write": 60.0, "pool": 60.0}
    others = {path: t for path, t in fake_daemon.timeouts.items() if path != redeem}
    assert others and all(t == {"connect": 5.0, "read": 10.0, "write": 10.0, "pool": 10.0} for t in others.values())


def test_release_on_the_host_drops_the_key(hub, fake_daemon):
    hub.sign_in()
    fake_daemon.approve(hub.post("/api/claim").json()["request_id"])
    hub.wait_for("claimed")
    fake_daemon.release()
    overview = hub.get("/api/overview").json()
    assert overview["claim"]["state"] == "released"
    assert "released on the daemon host" in overview["claim"]["message"]
    assert overview["ownership"] is None
    assert "key" not in state_file(hub)


def test_daemon_down_shows_the_error(hub, fake_daemon):
    hub.sign_in()
    fake_daemon.down = True
    overview = hub.get("/api/overview").json()
    assert overview["reachable"] is False
    assert "did not answer" in overview["error"]
    started = hub.post("/api/claim")
    assert started.status_code == 502 and started.json()["claim"]["state"] == "failed"


def test_installer_claim_file_holds_only_id_and_match_code(make_hub, fake_daemon, tmp_path):
    claim_file = tmp_path / "state" / "claim.json"
    hub = make_hub(claim_file=claim_file)
    published = json.loads(claim_file.read_text(encoding="utf-8"))
    row = fake_daemon.latest()
    assert published == {"id": row["id"], "match_code": row["match_code"]}
    fake_daemon.approve(row["id"])
    hub.sign_in()
    hub.wait_for("claimed")
    assert not claim_file.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")
def test_state_is_owner_only(hub, fake_daemon):
    hub.sign_in()
    hub.post("/api/claim")
    assert (hub.state_dir / STATE_FILE).stat().st_mode & 0o077 == 0
    assert hub.state_dir.stat().st_mode & 0o077 == 0
