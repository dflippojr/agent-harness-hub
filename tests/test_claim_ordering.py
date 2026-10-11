"""Bounded, event-controlled races: no sleeps or timing-dependent assertions."""

from __future__ import annotations

import asyncio
import threading

import httpx
import pytest

from harness_hub import state
from harness_hub.auth import create_signin_code
from harness_hub.claim import ClaimError
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
            await claims.released(key["token"])
        assert claims.key() == StateStore(tmp_path).get("key") == key
        assert claims.state() == "claimed"
        assert claims.store.get("outcome") is None

    run_scenario(tmp_path, monkeypatch, KEY_REPLY, exercise)


@pytest.mark.parametrize("route", ["/api/overview", "/api/modules"])
@pytest.mark.parametrize("redeemed", [False, True])
def test_stale_release_confirmation_preserves_new_claim(tmp_path, monkeypatch, route, redeemed):
    async def exercise(claims, daemon, browser):
        old_token = "ho-synthetic-old-key"
        claims.store.update(key={"token": old_token, "key_id": "k-old",
                                 "daemon_url": claims.daemon.base_url}, claim=None)
        confirmation_entered, confirm = asyncio.Event(), asyncio.Event()
        calls = 0

        async def admin(token, *_):
            nonlocal calls
            assert token == old_token
            calls += 1
            if calls == (3 if route == "/api/overview" else 1):
                confirmation_entered.set()
                await confirm.wait()
            return Reply(401, {})

        async def inventory(token):
            assert token == old_token
            return Reply(401, {})

        monkeypatch.setattr(claims.daemon, "admin", admin)
        monkeypatch.setattr(claims.daemon, "inventory", inventory)
        stale = asyncio.create_task(browser.get(route))
        await confirmation_entered.wait()
        await claims.released(old_token)
        daemon.reply = KEY_REPLY if redeemed else PENDING_REPLY
        await claims.start()
        await daemon.entered.wait()
        confirm.set()
        await asyncio.wait_for(claims._lock.waiters.get(), timeout=2)
        assert claims.pending()["request_id"] == "pr-new"
        daemon.respond.set()
        response = await stale
        assert response.status_code in (200, 409)
        assert claims.state() == ("claimed" if redeemed else "pending")
        assert claims.store.get("outcome") is None
        if redeemed:
            assert claims.key()["token"] == KEY_REPLY.body["token"]
        else:
            assert claims.pending()["request_id"] == "pr-new"
        await claims.released("")
        assert claims.state() == ("claimed" if redeemed else "pending")

    run_scenario(tmp_path, monkeypatch, KEY_REPLY, exercise)


@pytest.mark.parametrize("method", ["DELETE", "POST"])
def test_control_wait_is_bounded_without_cancelling_redeem(tmp_path, monkeypatch, method):
    async def exercise(claims, daemon, browser):
        claims.ensure_polling()
        await daemon.entered.wait()
        claims.wait_seconds = 0  # expire at the next suspension, without wall-clock timing
        response = await browser.request(method, "/api/claim")
        assert response.status_code == 503 and response.json()["claim"]["state"] == "pending"
        assert claims.pending()["request_id"] == "pr-old"
        assert not claims._poller.done() and not daemon.respond.is_set()
        claims.wait_seconds = 15
        daemon.respond.set()
        await claims._poller
        assert claims.state() == "claimed" and len(daemon.redemptions) == 1
        assert StateStore(tmp_path).get("key") == claims.key()

    run_scenario(tmp_path, monkeypatch, KEY_REPLY, exercise)


def test_start_network_deadline_releases_claim_lock(tmp_path, monkeypatch):
    async def exercise(claims, daemon, _browser):
        async def wedged_start(*_):
            daemon.starts += 1
            await asyncio.Event().wait()

        monkeypatch.setattr(claims.daemon, "start_hub_claim", wedged_start)
        claims.wait_seconds = 0
        with pytest.raises(ClaimError) as error:
            await claims.start()
        assert error.value.state == "failed" and daemon.starts == 1
        assert not claims._lock.locked() and claims.pending() is None
        claims.wait_seconds = 15
        await claims.forget()
        assert claims.state() == "unclaimed"
        assert daemon.redemptions == []

    run_scenario(tmp_path, monkeypatch, KEY_REPLY, exercise)


@pytest.mark.parametrize("interrupt", ["cancel", "deadline"])
def test_interrupted_shutdown_drains_redeem_and_final_save(tmp_path, monkeypatch, interrupt):
    async def exercise(claims, daemon, _browser):
        claims.ensure_polling()
        await daemon.entered.wait()
        real_write = state.write_private
        attempts = 0
        closed = asyncio.Event()
        real_close = claims.daemon.aclose

        def fail_once(path, text):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise OSError("synthetic failure")
            real_write(path, text)

        async def close():
            closed.set()
            await real_close()

        monkeypatch.setattr(state, "write_private", fail_once)
        monkeypatch.setattr(claims.daemon, "aclose", close)
        if interrupt == "deadline":
            claims.wait_seconds = 0
        stopping = asyncio.create_task(claims.stop())
        await claims._stopping.wait()
        if interrupt == "cancel":
            stopping.cancel()
            with pytest.raises(asyncio.CancelledError):
                await stopping
        else:
            await stopping
        assert not closed.is_set() and not claims._poller.done()
        assert not claims._shutdown_task.cancelled()
        daemon.respond.set()
        await claims._shutdown_task
        assert attempts == 2 and len(daemon.redemptions) == 1
        assert claims.state() == "claimed" and closed.is_set()
        assert claims._poller.done()
        assert StateStore(tmp_path).get("key") == claims.key()

    run_scenario(tmp_path, monkeypatch, KEY_REPLY, exercise)


@pytest.mark.parametrize("reply, expected", [
    (DENIED_REPLY, "denied"), (FAILED_REPLY, "failed"),
    (Reply(409, {"error": {"code": "hub_claimed"}}), "refused"), (Reply(404, {}), "expired"),
])
def test_terminal_write_retries_original_outcome_without_redeeming(tmp_path, monkeypatch, reply, expected):
    async def exercise(claims, daemon, browser):
        real_write = state.write_private
        original = claims.store.path.read_bytes()

        def fail_write(*_):
            raise OSError("synthetic storage failure")

        monkeypatch.setattr(state, "write_private", fail_write)
        daemon.respond.set()
        assert await claims.poll_once() is False
        assert claims.state() == expected
        outcome = dict(claims._terminal_outcome)
        daemon.reply = Reply(404, {})
        assert await claims.poll_once() is False
        assert claims._terminal_outcome == outcome
        assert claims.store.path.read_bytes() == original and claims.claim_file.exists()
        assert (await browser.get("/api/claim")).json()["state"] == expected
        # No user action may replace a result that has not been saved yet.
        monkeypatch.setattr(claims, "ensure_polling", lambda: None)
        await claims.forget()
        with pytest.raises(ClaimError) as error:
            await claims.start()
        assert error.value.state == "persisting" and daemon.starts == 0
        monkeypatch.setattr(state, "write_private", real_write)
        assert await claims.poll_once() is True
        assert len(daemon.redemptions) == 1
        assert claims.state() == expected
        assert StateStore(tmp_path).get("outcome") == outcome
        assert claims.pending() is None and not claims.claim_file.exists()

    run_scenario(tmp_path, monkeypatch, reply, exercise)


def test_persistence_backoff_is_capped_and_logs_are_rate_limited(tmp_path, monkeypatch, caplog):
    async def exercise(claims, daemon, _browser):
        now = [0.0]
        claims.monotonic = lambda: now[0]
        real_write = state.write_private

        def fail_write(*_):
            raise OSError(KEY_REPLY.body["token"])

        monkeypatch.setattr(state, "write_private", fail_write)
        daemon.respond.set()
        delays = []
        for _ in range(8):
            assert await claims.poll_once() is False
            delays.append(claims._retry_delay())
        assert delays == [2, 4, 8, 16, 32, 60, 60, 60]
        assert caplog.text.count("waiting for private state storage") == 1
        now[0] = 60
        assert await claims.poll_once() is False
        assert caplog.text.count("waiting for private state storage") == 2
        assert len(daemon.redemptions) == 1
        claims.ensure_polling()
        assert claims._retry_delay() == 2  # explicit wake resets the delay
        monkeypatch.setattr(state, "write_private", real_write)
        await claims._poller
        assert claims._persist_failures == 0 and claims._last_persist_log is None
        assert claims.state() == "claimed"
        assert_no_secrets([caplog.text], KEY_REPLY.body["token"])

    run_scenario(tmp_path, monkeypatch, KEY_REPLY, exercise)


def test_blocked_persistence_keeps_event_loop_and_state_reads_responsive(tmp_path, monkeypatch):
    async def exercise(claims, daemon, browser):
        entered, resume = asyncio.Event(), threading.Event()
        loop = asyncio.get_running_loop()
        real_write = state.write_private
        original_thread = threading.get_ident()

        def blocked_write(path, text):
            assert threading.get_ident() != original_thread
            loop.call_soon_threadsafe(entered.set)
            assert resume.wait(timeout=5), "test did not release the storage latch"
            real_write(path, text)

        monkeypatch.setattr(state, "write_private", blocked_write)
        daemon.respond.set()
        polling = asyncio.create_task(claims.poll_once())
        try:
            await entered.wait()
            response = await browser.get("/api/claim")
            assert response.json()["state"] == "persisting"
            assert claims.store.get("key") is None
            assert "synthetic-private-verifier" in claims.secrets()
            assert not polling.done()
        finally:
            resume.set()
            await polling
        assert claims.state() == "claimed" and len(daemon.redemptions) == 1

    run_scenario(tmp_path, monkeypatch, KEY_REPLY, exercise)



def test_release_clears_only_its_key_not_pending_metadata(tmp_path, monkeypatch):
    async def exercise(claims, _daemon, _browser):
        pending = claims.pending()
        claims.store.update(key={"token": "ho-synthetic-old", "daemon_url": claims.daemon.base_url})
        await claims.released("ho-synthetic-old")
        assert claims.key() is None
        assert claims.pending() == StateStore(tmp_path).get("claim") == pending

    run_scenario(tmp_path, monkeypatch, PENDING_REPLY, exercise)


def test_start_lock_deadline_is_retryable_even_before_any_claim(tmp_path, monkeypatch):
    async def exercise(claims, daemon, browser):
        claims.store.update(claim=None)
        await claims._lock.acquire()
        claims.wait_seconds = 0
        try:
            response = await browser.post("/api/claim")
            assert response.status_code == 503
            assert response.json()["claim"]["state"] == "unclaimed"
            assert daemon.starts == 0 and daemon.redemptions == []
        finally:
            claims._lock.release()
            claims.wait_seconds = 15

    run_scenario(tmp_path, monkeypatch, KEY_REPLY, exercise)


@pytest.mark.parametrize("stored_pending", [False, True])
def test_lifespan_starts_and_drains_claim_poller(tmp_path, monkeypatch, stored_pending):
    async def exercise(claims, daemon, browser):
        if not stored_pending:
            claims.store.update(claim=None)
        app = browser._transport.app.fastapi
        async with app.router.lifespan_context(app):
            await daemon.entered.wait()
            assert daemon.starts == (0 if stored_pending else 1)
            assert claims.claim_file.exists()
            daemon.respond.set()
            await claims._poller
            assert claims.state() == "claimed"
        assert claims._shutdown_task.done() and claims.daemon._client.is_closed
        assert StateStore(tmp_path).get("key") == claims.key()

    run_scenario(tmp_path, monkeypatch, KEY_REPLY, exercise)
