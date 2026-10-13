"""Routes that make several independent daemon reads keep them in flight together (#56)."""
import asyncio

import pytest

from harness_hub.daemon import ADMIN, DaemonClient

from .marketplace_fake import manifest_entry


@pytest.fixture
def recorded(monkeypatch):
    """Every daemon call as ("start"|"end", method, path), with a delay so sequential calls could never overlap."""
    events = []
    call = DaemonClient._call

    async def slow(self, method, path, **kw):
        events.append(("start", method, path))
        try:
            await asyncio.sleep(0.05)
            return await call(self, method, path, **kw)
        finally:
            events.append(("end", method, path))

    monkeypatch.setattr(DaemonClient, "_call", slow)
    return events


def assert_in_flight_together(events, *calls):
    """Each call started before any of them finished, so all of them were awaited at the same time."""
    first_end = min(events.index(("end", *call)) for call in calls)
    for call in calls:
        assert events.index(("start", *call)) < first_end, (call, events)


def test_overview_reads_health_discovery_and_claim_record_together(claimed, recorded):
    recorded.clear()
    reply = claimed.get("/api/overview")
    assert reply.status_code == 200 and reply.json()["error"] == "", reply.text
    assert_in_flight_together(recorded, ("GET", "/health"), ("GET", ADMIN), ("GET", ADMIN + "/hub-claim"))


def test_marketplace_reads_inventory_and_pairing_requests_together(claimed, fake_daemon, recorded):
    fake_daemon.app_entries = [manifest_entry("test.example.notes", "Synthetic notes")]
    recorded.clear()
    reply = claimed.get("/api/marketplace")
    assert reply.status_code == 200 and reply.json()["slots_error"] == "", reply.text
    assert_in_flight_together(recorded, ("GET", ADMIN + "/hub"), ("GET", ADMIN + "/pairing-requests"))


def test_app_detail_reads_limits_and_key_list_together(claimed, recorded):
    recorded.clear()
    reply = claimed.get("/api/apps/k-minute/limits")
    assert reply.status_code == 200 and "retention_days" in reply.json(), reply.text
    assert_in_flight_together(recorded, ("GET", ADMIN + "/apps/k-minute/limits"), ("GET", ADMIN + "/keys"))
