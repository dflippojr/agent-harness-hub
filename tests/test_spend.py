"""Spend: every App's usage and session-cap use, backend windows, local tokens only, and no per-poll writes."""
from harness_hub.inventory import INVENTORY_FILE
from harness_hub.views import cap_use, spend_view

from .conftest import assert_no_secrets

HOSTED = {"name": "sample", "billing": "api", "today": {"requests": 2, "prompt_tokens": 30, "completion_tokens": 5,
                                                         "cost_usd": 0.25},
          "week": {"requests": 9, "prompt_tokens": 300, "completion_tokens": 50, "cost_usd": 1.5}}


def test_every_app_with_usage_caps_and_backend_windows(claimed, fake_daemon):
    fake_daemon.backend.update(HOSTED)
    fake_daemon.app_limits["k-twenty"] = {"max_running": 10, "max_queued": 10}
    reply = claimed.get("/api/spend")
    assert reply.status_code == 200
    data = reply.json()
    assert data["error"] == "" and data["stale"] is False and data["fetched_at"]
    rows = {row["id"]: row for row in data["apps"]}
    assert fake_daemon.hub["key_id"] not in rows  # the Hub's own key spends nothing
    assert set(rows) == {"k-" + label for label in
                         ("never", "minute", "twenty", "three", "thirty", "revoked", "pending", "errors", "old")}
    assert rows["k-minute"]["usage"] == {"requests": 7, "prompt_tokens": 100, "completion_tokens": 40,
                                         "cost_usd": 0.5}
    # The fake counts running 2, queued 0 and parked 1; parked sessions count toward both caps.
    assert rows["k-minute"]["caps"] == {"max_running": 2, "max_queued": 10, "running_used": 3, "queued_used": 1,
                                        "parked": 1, "share": 1.5, "near": True}
    assert rows["k-twenty"]["caps"]["near"] is False and rows["k-twenty"]["caps"]["share"] == 0.3
    assert rows["k-revoked"]["caps"] is None and rows["k-pending"]["caps"] is None  # revoked: no cap read
    assert data["backends"] == [{"name": "sample", "billing": "api", "local": False,
                                 "today": HOSTED["today"], "week": HOSTED["week"]}]
    assert_no_secrets(claimed.bodies, fake_daemon.hub_token, "provider-secret-sentinel")


def test_local_backends_show_tokens_only(claimed, fake_daemon):
    fake_daemon.backend.update({**HOSTED, "billing": "local"})
    row = claimed.get("/api/spend").json()["backends"][0]
    assert row["local"] is True
    assert row["today"] == {"requests": 2, "prompt_tokens": 30, "completion_tokens": 5, "cost_usd": None}
    assert row["week"]["cost_usd"] is None and row["week"]["prompt_tokens"] == 300


def test_spend_reads_without_writing_the_inventory_cache(claimed, fake_daemon):
    path = claimed.state_dir / INVENTORY_FILE
    assert not path.exists()
    for _ in range(3):
        assert claimed.get("/api/spend").status_code == 200
    assert not path.exists()
    assert {method for method, _ in fake_daemon.apps_calls} == {"GET"}


def test_failed_inventory_falls_back_to_the_cached_snapshot(claimed, fake_daemon):
    assert claimed.get("/api/apps").status_code == 200  # Apps saves the last good inventory
    fake_daemon.inventory_error = "hub.entries: invalid JSON"
    data = claimed.get("/api/spend").json()
    assert data["error"] == "hub.entries: invalid JSON" and data["stale"] is True
    assert "k-minute" in {row["id"] for row in data["apps"]}


def test_no_inventory_at_all_reports_the_error(claimed, fake_daemon):
    fake_daemon.inventory_absent = True
    data = claimed.get("/api/spend").json()
    assert data["apps"] is None and data["stale"] is False and data["error"]
    assert data["backends"]  # backend windows still show


def test_unclaimed_hub_refuses(hub):
    hub.sign_in()
    assert hub.get("/api/spend").status_code == 409


def test_malformed_daemon_values_are_dropped():
    assert cap_use({"configured": {}, "effective": {"max_running": 0, "max_queued": 1}}) is None
    shown = spend_view({"apps": [{"id": "bad id!", "kind": "app"}, "junk",
                                 {"id": "k-a", "kind": "app", "store": {"usage": {"requests": float("inf"),
                                                                                  "cost_usd": "1"}}},
                                 {"id": "k-owner", "kind": "owner"}]},
                       [{"name": "x", "billing": "api", "today": "junk", "week": {"requests": 2**70}}, 7], {}, "")
    assert [row["id"] for row in shown["apps"]] == ["k-a"]
    assert shown["apps"][0]["usage"] == dict.fromkeys(("requests", "prompt_tokens", "completion_tokens",
                                                       "cost_usd"))
    assert shown["backends"][0]["today"]["requests"] is None and shown["backends"][0]["week"]["requests"] is None


def test_key_rejection_withholds_the_cached_inventory(claimed, fake_daemon):
    claimed.get("/api/apps")
    fake_daemon.release()
    reply = claimed.get("/api/spend")
    assert reply.status_code == 409 and "apps" not in reply.json()
    assert claimed.get("/api/claim").json()["state"] == "released"


def test_an_inventory_outage_keeps_the_backend_windows(claimed, fake_daemon, monkeypatch):
    from harness_hub.daemon import DaemonClient, DaemonUnreachable

    async def unreachable(self, token):
        raise DaemonUnreachable("synthetic outage")

    fake_daemon.backend.update(HOSTED)
    monkeypatch.setattr(DaemonClient, "inventory", unreachable)
    data = claimed.get("/api/spend").json()
    assert data["error"] == "The daemon did not answer." and data["apps"] is None
    assert data["backends_error"] == "" and data["backends"][0]["week"] == HOSTED["week"]
    assert data["reachable"] is True  # the backend read answered, so the daemon is up


def test_spend_says_when_no_read_reached_the_daemon(claimed, monkeypatch):
    from harness_hub.daemon import DaemonClient, DaemonUnreachable

    async def unreachable(self, *args, **kwargs):
        raise DaemonUnreachable("synthetic outage")

    assert claimed.get("/api/spend").json()["reachable"] is True
    for read in ("inventory", "settings"):
        monkeypatch.setattr(DaemonClient, read, unreachable)
    data = claimed.get("/api/spend").json()
    assert data["reachable"] is False and data["error"] == "The daemon did not answer."


def test_a_rejected_cap_read_goes_through_the_key_refusal(claimed, fake_daemon, monkeypatch):
    from harness_hub.daemon import DaemonClient, Reply

    original = DaemonClient.apps

    async def rejected(self, token, action, app_id="", body=None):
        if action == "limits" and app_id == "k-minute":
            return Reply(401, {"detail": "synthetic rejection"})
        return await original(self, token, action, app_id, body)

    monkeypatch.setattr(DaemonClient, "apps", rejected)
    reply = claimed.get("/api/spend")
    assert reply.status_code == 409 and "apps" not in reply.json()
    # One unconfirmed rejection keeps the claim (as the Apps detail does); a confirmed one releases it.
    assert claimed.get("/api/claim").json()["state"] == "claimed"
    fake_daemon.release()
    assert claimed.get("/api/spend").status_code == 409
    assert claimed.get("/api/claim").json()["state"] == "released"


def test_every_live_app_gets_its_caps_with_bounded_concurrency(claimed, fake_daemon, monkeypatch):
    import asyncio
    from copy import deepcopy

    from harness_hub.daemon import DaemonClient
    from harness_hub.server import SPEND_CAP_READS

    template = fake_daemon.apps_rows[1]  # k-minute: live, used a minute ago
    for n in range(60):
        row = deepcopy(template)
        row["id"] = f"k-extra-{n}"
        fake_daemon.apps_rows.append(row)
        fake_daemon.app_limits[row["id"]] = {"max_running": None, "max_queued": 10}
    original, flight, peak = DaemonClient.apps, [0], [0]

    async def counted(self, token, action, app_id="", body=None):
        flight[0] += 1
        peak[0] = max(peak[0], flight[0])
        try:
            await asyncio.sleep(0.001)
            return await original(self, token, action, app_id, body)
        finally:
            flight[0] -= 1

    monkeypatch.setattr(DaemonClient, "apps", counted)
    rows = {row["id"]: row for row in claimed.get("/api/spend").json()["apps"]}
    assert all(rows[f"k-extra-{n}"]["caps"] is not None for n in range(60))
    assert 1 < peak[0] <= SPEND_CAP_READS


def test_an_unanswered_refusal_confirmation_is_a_503_not_a_crash(claimed, fake_daemon, monkeypatch):
    from harness_hub.daemon import DaemonClient, DaemonUnreachable, Reply

    original = DaemonClient.apps

    async def rejected(self, token, action, app_id="", body=None):
        if action == "limits":
            return Reply(401, {"detail": "synthetic rejection"})
        return await original(self, token, action, app_id, body)

    async def unanswered(self, token, path=""):
        raise DaemonUnreachable("synthetic outage")

    monkeypatch.setattr(DaemonClient, "apps", rejected)
    monkeypatch.setattr(DaemonClient, "admin", unanswered)
    reply = claimed.get("/api/spend")
    assert reply.status_code == 503 and "apps" not in reply.json()
    assert claimed.get("/api/claim").json()["state"] == "claimed"
