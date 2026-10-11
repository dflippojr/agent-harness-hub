"""Bounded, event-controlled races: no sleeps or timing-dependent assertions."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from harness_hub import state
from harness_hub.auth import create_signin_code
from harness_hub.config import Settings
from harness_hub.daemon import Reply
from harness_hub.server import create_app
from harness_hub.state import StateStore

from .conftest import HUB_HOST, assert_no_secrets

KEY_REPLY = Reply(201, {"token": "ho-synthetic-once-only", "app": {"id": "k-test", "role": "hub"}})
DENIED_REPLY = Reply(403, {"detail": "owner denied this request"})
FAILED_REPLY = Reply(500, {"detail": "synthetic failure"})
PENDING_REPLY = Reply(409, {"error": {"code": "pairing_pending"}})


class ObservedLock(asyncio.Lock):
    """Notify tests only once an operation actually contends for the claim lock."""

    def __init__(self):
        super().__init__()
        self.waiters = asyncio.Queue()

    async def acquire(self):
        if self.locked():
            self.waiters.put_nowait(asyncio.current_task())
        return await super().acquire()


class ControlledDaemon:
    def __init__(self, reply):
        self.reply = reply
        self.entered = asyncio.Event()
        self.respond = asyncio.Event()
        self.redemptions = []
        self.starts = 0

    async def redeem(self, rid, verifier):
        reply = self.reply
        self.redemptions.append((rid, verifier))
        self.entered.set()
        await self.respond.wait()
        return reply

    async def start(self, *_):
        self.starts += 1
        return Reply(201, {"id": "pr-new", "match_code": "654321", "expires_at": 2000})


async def scenario(tmp_path, monkeypatch, reply, exercise):
    app = create_app(Settings(state_dir=tmp_path, daemon_url="https://daemon.test", poll_seconds=3600,
                              claim_file=tmp_path / "claim.json"),
                     transport=httpx.MockTransport(lambda _: httpx.Response(503, json={"detail": "offline"})))
    claims = app.fastapi.state.claims
    claims.clock = lambda: 1000
    claims._lock = ObservedLock()
    daemon = ControlledDaemon(reply)
    monkeypatch.setattr(claims.daemon, "redeem_hub_claim", daemon.redeem)
    monkeypatch.setattr(claims.daemon, "start_hub_claim", daemon.start)
    claims.store.update(claim={"request_id": "pr-old", "verifier": "synthetic-private-verifier",
                               "match_code": "123456", "started_at": 0, "expires_at": 900,
                               "daemon_url": claims.daemon.base_url})
    claims._publish_claim_file("pr-old", "123456")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=f"http://{HUB_HOST}",
                                headers={"X-Hub-Request": "1"}) as browser:
        signin = await browser.post("/api/session", json={"code": create_signin_code(tmp_path)})
        assert signin.status_code == 204
        browser.headers["X-Hub-Session"] = signin.headers["x-hub-session"]
        try:
            await exercise(claims, daemon, browser)
        finally:
            daemon.respond.set()
            await claims.stop()
            await claims.daemon.aclose()
        assert claims._poller is None or claims._poller.done()


def run_scenario(tmp_path, monkeypatch, reply, exercise):
    asyncio.run(asyncio.wait_for(scenario(tmp_path, monkeypatch, reply, exercise), timeout=10))


@pytest.mark.parametrize("reply, expected", [(KEY_REPLY, "claimed"), (DENIED_REPLY, "unclaimed"),
                                           (FAILED_REPLY, "unclaimed"), (PENDING_REPLY, "unclaimed")])
def test_delete_waits_for_in_flight_reply(tmp_path, monkeypatch, reply, expected):
    async def exercise(claims, daemon, browser):
        claims.ensure_polling()
        await daemon.entered.wait()
        deletion = asyncio.create_task(browser.delete("/api/claim"))
        await claims._lock.waiters.get()
        assert not deletion.done()
        assert claims.pending()["request_id"] == "pr-old"
        daemon.respond.set()
        response = await deletion
        assert response.status_code == 200 and response.json()["state"] == expected
        await claims._poller
        assert claims.state() == expected
        assert claims.pending() is None
        assert not claims.claim_file.exists()
        reloaded = StateStore(tmp_path)
        assert reloaded.get("claim") is None
        assert reloaded.get("outcome") is None
        assert claims.store.get("key") == reloaded.get("key")
        if expected == "claimed":
            assert reloaded.get("key")["request_id"] == "pr-old"
            assert reloaded.get("key")["token"] == KEY_REPLY.body["token"]
        else:
            assert reloaded.get("key") is None
        assert await claims.poll_once() is True
        assert len(daemon.redemptions) == 1
        assert_no_secrets([response.text], KEY_REPLY.body["token"], "synthetic-private-verifier")

    run_scenario(tmp_path, monkeypatch, reply, exercise)


def test_delete_wins_before_queued_redemption(tmp_path, monkeypatch):
    async def exercise(claims, daemon, browser):
        await claims._lock.acquire()
        deletion = asyncio.create_task(browser.delete("/api/claim"))
        await claims._lock.waiters.get()
        claims.ensure_polling()
        await claims._lock.waiters.get()
        claims._lock.release()
        response = await deletion
        assert response.status_code == 200 and response.json()["state"] == "unclaimed"
        await claims._poller
        assert daemon.redemptions == []
        assert claims.state() == "unclaimed" and claims.pending() is None
        assert StateStore(tmp_path).get("claim") is None
        assert StateStore(tmp_path).get("key") is None
        assert not claims.claim_file.exists()

    run_scenario(tmp_path, monkeypatch, KEY_REPLY, exercise)


@pytest.mark.parametrize("reply, expected", [(KEY_REPLY, "claimed"), (DENIED_REPLY, "pending"),
                                           (FAILED_REPLY, "pending"), (PENDING_REPLY, "pending")])
def test_replacement_waits_for_old_reply(tmp_path, monkeypatch, reply, expected):
    async def exercise(claims, daemon, browser):
        claims.ensure_polling()
        await daemon.entered.wait()
        replacement = asyncio.create_task(browser.post("/api/claim"))
        await claims._lock.waiters.get()
        assert not replacement.done() and daemon.starts == 0
        assert claims.pending()["request_id"] == "pr-old"
        old_poller = claims._poller
        # Let a new request wait for approval rather than replaying the old terminal reply.
        daemon.reply = PENDING_REPLY
        # The in-flight call has already captured which terminal response to return below.
        daemon.respond.set()
        response = await replacement
        assert response.json().get("claim", response.json())["state"] == expected
        await claims.stop()
        assert old_poller.done()
        reloaded = StateStore(tmp_path)
        if expected == "claimed":
            assert response.status_code == 409 and daemon.starts == 0
            assert reloaded.get("key")["request_id"] == "pr-old"
            assert claims.pending() is None
            assert not claims.claim_file.exists()
        else:
            assert response.status_code == 201 and daemon.starts == 1
            assert response.json()["request_id"] == "pr-new"
            assert claims.pending()["request_id"] == reloaded.get("claim")["request_id"] == "pr-new"
            assert reloaded.get("key") is None and reloaded.get("outcome") is None
            assert claims.claim_file.exists()
        assert_no_secrets([response.text], KEY_REPLY.body["token"], "synthetic-private-verifier")

    run_scenario(tmp_path, monkeypatch, reply, exercise)


def test_redeemed_key_retries_only_persistence(tmp_path, monkeypatch, caplog):
    async def exercise(claims, daemon, browser):
        original = claims.store.path.read_bytes()
        real_write = state.write_private

        def fail_write(*_):
            raise OSError(KEY_REPLY.body["token"] + " synthetic-private-verifier")

        monkeypatch.setattr(state, "write_private", fail_write)
        daemon.respond.set()
        assert await claims.poll_once() is False
        assert claims.state() == "persisting"
        assert claims.key() is None and claims.token() == ""
        assert claims.pending()["request_id"] == "pr-old"
        assert claims.store.path.read_bytes() == original
        assert claims.claim_file.exists()
        assert claims.store.get("key") == StateStore(tmp_path).get("key") is None
        assert KEY_REPLY.body["token"] in claims.secrets()
        assert "synthetic-private-verifier" in claims.secrets()
        get = await browser.get("/api/claim")
        delete = await browser.delete("/api/claim")
        start = await browser.post("/api/claim")
        overview = await browser.get("/api/overview")
        assert get.json()["state"] == delete.json()["state"] == "persisting"
        assert start.status_code == 503 and start.json()["claim"]["state"] == "persisting"
        assert overview.json()["claim"]["state"] == "persisting"
        assert daemon.starts == 0
        assert claims.store.path.read_bytes() == original
        assert len(daemon.redemptions) == 1
        assert_no_secrets([get.text, delete.text, start.text, overview.text, caplog.text],
                          KEY_REPLY.body["token"], "synthetic-private-verifier")
        monkeypatch.setattr(state, "write_private", real_write)
        assert await claims.poll_once() is True
        assert claims.state() == "claimed"
        saved = StateStore(tmp_path)
        assert saved.get("key") == claims.key()
        assert saved.get("key")["request_id"] == "pr-old"
        assert saved.get("key")["token"] == KEY_REPLY.body["token"]
        assert saved.get("claim") is None and saved.get("outcome") is None
        assert not claims.claim_file.exists()
        assert len(daemon.redemptions) == 1
        await claims.stop()
        assert claims._poller.done()

    run_scenario(tmp_path, monkeypatch, KEY_REPLY, exercise)


def test_in_flight_delete_preserves_a_key_waiting_for_storage(tmp_path, monkeypatch):
    async def exercise(claims, daemon, browser):
        real_write = state.write_private
        original = claims.store.path.read_bytes()

        def fail_write(*_):
            raise OSError("synthetic storage failure")

        monkeypatch.setattr(state, "write_private", fail_write)
        claims.ensure_polling()
        await daemon.entered.wait()
        deletion = asyncio.create_task(browser.delete("/api/claim"))
        await claims._lock.waiters.get()
        assert not deletion.done()
        daemon.respond.set()
        response = await deletion
        assert response.status_code == 200 and response.json()["state"] == "persisting"
        assert claims.state() == "persisting" and claims.key() is None
        assert claims.pending()["request_id"] == "pr-old"
        assert claims.store.path.read_bytes() == original
        assert StateStore(tmp_path).get("key") is None
        assert claims.claim_file.exists()
        monkeypatch.setattr(state, "write_private", real_write)
        await claims.stop()
        assert claims._poller.done() and claims.state() == "claimed"
        assert StateStore(tmp_path).get("key")["request_id"] == "pr-old"
        assert StateStore(tmp_path).get("claim") is None
        assert len(daemon.redemptions) == 1
        assert_no_secrets([response.text], KEY_REPLY.body["token"], "synthetic-private-verifier")

    run_scenario(tmp_path, monkeypatch, KEY_REPLY, exercise)


def test_poller_retries_after_storage_recovers(tmp_path, monkeypatch):
    async def exercise(claims, daemon, browser):
        real_write = state.write_private
        first_attempt = asyncio.Event()
        real_poll = claims.poll_once

        def fail_write(*_):
            raise OSError("synthetic storage failure")

        async def observed_poll():
            result = await real_poll()
            first_attempt.set()
            return result

        monkeypatch.setattr(state, "write_private", fail_write)
        monkeypatch.setattr(claims, "poll_once", observed_poll)
        daemon.respond.set()
        claims.ensure_polling()
        await first_attempt.wait()
        assert claims.state() == "persisting" and not claims._poller.done()
        assert claims.pending()["request_id"] == "pr-old"
        assert StateStore(tmp_path).get("key") is None
        monkeypatch.setattr(state, "write_private", real_write)
        # Starting while recovery is pending wakes the existing poller but cannot replace its credential.
        response = await browser.post("/api/claim")
        assert response.status_code == 503
        await claims._poller
        assert daemon.starts == 0 and len(daemon.redemptions) == 1
        assert claims.state() == "claimed"
        assert StateStore(tmp_path).get("key") == claims.key()
        assert claims.pending() is None and not claims.claim_file.exists()

    run_scenario(tmp_path, monkeypatch, KEY_REPLY, exercise)


def test_shutdown_reports_host_recovery_if_storage_stays_broken(tmp_path, monkeypatch, caplog):
    async def exercise(claims, daemon, _browser):
        original = claims.store.path.read_bytes()

        def fail_write(*_):
            raise OSError(KEY_REPLY.body["token"])

        monkeypatch.setattr(state, "write_private", fail_write)
        daemon.respond.set()
        assert await claims.poll_once() is False
        await claims.stop()
        assert claims.state() == "persisting"
        assert claims.key() is None and claims.pending()["request_id"] == "pr-old"
        assert claims.store.path.read_bytes() == original
        assert StateStore(tmp_path).get("key") is None
        assert claims.claim_file.exists()
        assert len(daemon.redemptions) == 1
        assert "release the Hub on the daemon host" in caplog.text
        assert_no_secrets([caplog.text], KEY_REPLY.body["token"], "synthetic-private-verifier")

    run_scenario(tmp_path, monkeypatch, KEY_REPLY, exercise)


def test_shutdown_waits_for_redemption_and_final_save(tmp_path, monkeypatch):
    async def exercise(claims, daemon, _browser):
        claims.ensure_polling()
        await daemon.entered.wait()
        stopping = asyncio.create_task(claims.stop())
        await claims._stopping.wait()
        assert not stopping.done()
        real_write = state.write_private
        attempts = 0

        def fail_once(path, text):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise OSError("synthetic storage failure")
            real_write(path, text)

        monkeypatch.setattr(state, "write_private", fail_once)
        daemon.respond.set()
        await stopping
        assert attempts == 2 and len(daemon.redemptions) == 1
        assert claims.state() == "claimed" and claims._poller.done()
        assert StateStore(tmp_path).get("key") == claims.key()
        assert StateStore(tmp_path).get("claim") is None

    run_scenario(tmp_path, monkeypatch, KEY_REPLY, exercise)


def test_failed_forget_and_release_preserve_state(tmp_path, monkeypatch):
    async def exercise(claims, daemon, _browser):
        real_write = state.write_private

        def fail_write(*_):
            raise OSError("synthetic storage failure")

        pending = claims.pending()
        monkeypatch.setattr(state, "write_private", fail_write)
        with pytest.raises(OSError):
            await claims.forget()
        assert claims.pending() == StateStore(tmp_path).get("claim") == pending
        assert claims.state() == "pending"
        monkeypatch.setattr(state, "write_private", real_write)
        daemon.respond.set()
        assert await claims.poll_once() is True
        key = claims.key()
        monkeypatch.setattr(state, "write_private", fail_write)
        with pytest.raises(OSError):
            claims.released()
        assert claims.key() == StateStore(tmp_path).get("key") == key
        assert claims.state() == "claimed"
        assert claims.store.get("outcome") is None

    run_scenario(tmp_path, monkeypatch, KEY_REPLY, exercise)
