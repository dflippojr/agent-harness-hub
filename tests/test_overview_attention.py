"""Overview counts the App pairing requests waiting for the owner (#58): counts only, never the rows themselves."""
import asyncio

import pytest

from harness_hub.daemon import ADMIN, DaemonClient, DaemonUnreachable, Reply

from .conftest import assert_no_secrets
from .test_concurrent_reads import consent_lock

WARNING = "This daemon approved the claim without confirmation; update the daemon."


def overview(client):
    reply = client.get("/api/overview")
    assert reply.status_code == 200, reply.text
    return reply.json()


def without_volatile(data):
    return {key: value for key, value in data.items() if key not in ("fetched_at", "attention")}


def test_attention_counts_only_unexpired_app_rows_the_owner_decides(claimed, fake_daemon):
    for name in ("ho-SyntheticTokenName1", "Synthetic notes"):
        fake_daemon.add_pairing(name, ["sessions"])
    fake_daemon.add_pairing("hp-SyntheticTokenName2", ["sessions"], state="claimed", origin="")
    fake_daemon.add_pairing("Armed slot", ["sessions"], state="armed")
    fake_daemon.add_pairing("Approved, waiting to redeem", ["sessions"], state="approved")
    fake_daemon.add_pairing("Expired request", ["sessions"], expires_in=-1)
    fake_daemon.add_pairing("Expired claimed slot", ["sessions"], state="claimed", expires_in=-1)
    fake_daemon.add_pairing("Another Hub", ["admin"], kind="hub")
    data = overview(claimed)
    assert data["attention"] == {"approve": 2, "confirm": 1}
    assert data["error"] == "" and data["reachable"] is True and data["ownership"]["this_hub"] is True
    assert ("GET", ADMIN + "/pairing-requests") in fake_daemon.pairing_calls
    for leaked in ("Synthetic notes", "Armed slot", "Another Hub", "pr-", "match"):
        assert leaked not in claimed.bodies[-1]
    assert_no_secrets(claimed.bodies, fake_daemon.hub_token)


def test_attention_is_zero_with_no_waiting_requests(claimed):
    assert overview(claimed)["attention"] == {"approve": 0, "confirm": 0}


def test_attention_is_null_and_unread_while_unclaimed(hub, fake_daemon):
    hub.sign_in()
    data = overview(hub)
    assert data["attention"] is None and data["reachable"] is True and data["error"] == ""
    assert fake_daemon.pairing_calls == []


@pytest.mark.parametrize("failure", ["5xx", "not_a_list", "unreachable"])
def test_a_failed_pairing_read_only_nulls_attention(claimed, fake_daemon, monkeypatch, failure):
    fake_daemon.add_pairing("Synthetic notes", ["sessions"])
    before = overview(claimed)
    assert before["attention"] == {"approve": 1, "confirm": 0}
    if failure == "5xx":
        fake_daemon.pairing_list_error = (503, {"detail": "synthetic outage"})
    elif failure == "not_a_list":
        fake_daemon.pairing_list_error = (200, {"requests": []})
    else:
        async def unreachable(self, token, operation, *args, **kwargs):
            raise DaemonUnreachable("synthetic outage")

        monkeypatch.setattr(DaemonClient, "pairing", unreachable)
    after = overview(claimed)
    assert after["attention"] is None
    assert without_volatile(after) == without_volatile(before)
    assert claimed.client.get("/api/claim").json()["state"] == "claimed"


def refuse_pairing(monkeypatch, *, confirmed):
    """The pairing read answers 401; the confirmation read answers 401 too only when `confirmed`."""
    admin = DaemonClient.admin
    refused, discovery_reads = [], []

    async def pairing(self, token, operation, *args, **kwargs):
        refused.append(operation)
        return Reply(401, {"detail": "synthetic refusal"})

    async def confirming(self, token, path=""):
        # The first discovery read is Overview's own and succeeds; only the follow-up confirmation read is refused.
        if path == "":
            discovery_reads.append(path)
            if confirmed and len(discovery_reads) > 1:
                return Reply(401, {"detail": "synthetic refusal"})
        return await admin(self, token, path)

    monkeypatch.setattr(DaemonClient, "pairing", pairing)
    monkeypatch.setattr(DaemonClient, "admin", confirming)
    return refused, discovery_reads


def test_a_single_pairing_401_keeps_the_key(claimed, fake_daemon, monkeypatch):
    refused, discovery_reads = refuse_pairing(monkeypatch, confirmed=False)
    data = overview(claimed)
    assert refused == ["list"] and len(discovery_reads) == 2
    assert data["attention"] is None and data["claim"]["state"] == "claimed"
    assert data["error"] == "the owner API rejected the key once; retained it pending confirmation"
    assert data["ownership"]["this_hub"] is True
    assert fake_daemon.hub_token in fake_daemon.live_tokens


def test_a_confirmed_pairing_401_releases_the_key(claimed, monkeypatch):
    refused, discovery_reads = refuse_pairing(monkeypatch, confirmed=True)
    data = overview(claimed)
    assert refused == ["list"] and len(discovery_reads) == 2
    assert data["attention"] is None and data["ownership"] is None
    assert data["claim"]["state"] != "claimed" and data["error"] == ""


@pytest.mark.parametrize("failure", ["refused", "5xx", "unreachable", "health_5xx"])
def test_a_failed_overview_read_does_not_wait_for_the_pairing_read(claimed, monkeypatch, failure):
    lock = consent_lock(claimed)
    admin = DaemonClient.admin
    held = []

    async def stuck(self, token, operation, *args, **kwargs):
        # The pairing read holds the consent lock and the daemon never answers it.
        held.append(lock.locked())
        await asyncio.Event().wait()

    async def unhealthy(self):
        return Reply(503, {"detail": "synthetic outage"})

    async def failing(self, token, path=""):
        if path == "/hub-claim" and failure != "health_5xx":
            if failure == "unreachable":
                raise DaemonUnreachable("synthetic outage")
            return Reply(401 if failure == "refused" else 500, {"detail": "synthetic failure"})
        return await admin(self, token, path)

    monkeypatch.setattr(DaemonClient, "pairing", stuck)
    monkeypatch.setattr(DaemonClient, "admin", failing)
    if failure == "health_5xx":
        monkeypatch.setattr(DaemonClient, "health", unhealthy)
    data = overview(claimed)
    assert held == [True]
    assert data["attention"] is None and data["error"] and data["claim"]["state"] == "claimed"
    # The abandoned read was cancelled and gave the lock back, so Marketplace and Pairing are not blocked.
    assert not lock.locked()


def test_overview_observes_browser_slots_for_the_confirmation_warning(claimed, fake_daemon):
    rid = fake_daemon.add_pairing("Armed browser slot", ["sessions"], state="armed",
                                  origin="https://notes.example.test")
    assert overview(claimed)["attention"] == {"approve": 0, "confirm": 0}
    # A legacy daemon approves the claim before the owner confirms; Overview saw it armed, so Pairing warns.
    fake_daemon.pairing_rows[rid].update(state="approved", armed=True)
    assert claimed.get("/api/pairing").json()["confirmation_warning"] == WARNING
