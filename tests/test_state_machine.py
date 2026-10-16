"""Restart and cache ordering as a state machine (#143): the class check behind #89, #100 and #101.

Hypothesis plays random sequences of owner and daemon events against a real Hub (`create_app`) and the daemon fake
(`tests/conftest.py`), and feeds every reply the browser would get to the shell's real status module
(`harness_hub/static/lib/status.mjs`, through `tests/status_driver.mjs`). A read can be left in flight, held either
before its request reaches the daemon or after the daemon answered, so other events land between the browser starting
the read, the daemon's sample and the Hub's use of it.

Rules: visit a page, start a page read and finish it later, save a settings revision, start a restart, finish the
restart (the daemon boots the revision it accepted), the daemon stops or starts answering, and the inventory changes
(clean, or with a credential-like string that one page's view would show).

Invariants, checked after every reply:
- a restart ends only on health read since it began: no page reports it over from health read before (#100), and
  health read since then at its target does end it;
- the shell keeps "restart needed" while the daemon has not applied the saved revision it last read (#101);
- a cached inventory never makes another page answer a withheld 500 (#89): every cached snapshot is clean in every
  page's view, and no reply carries a credential-like string;
- every reply that does not show fresh data says why (an error sentence, or `stale` with one).

The profile is fixed (`derandomize`) and bounded so the suite stays near 30 seconds in CI; set
HUB_STATE_MACHINE_EXAMPLES for a longer local search. The deterministic tests at the end replay #89, #100 and #101.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from hypothesis import HealthCheck, settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, precondition, rule

from harness_hub import server
from harness_hub.auth import create_signin_code
from harness_hub.config import Settings
from harness_hub.guard import TOKEN_SHAPE, WITHHELD, leaks
from harness_hub.inventory import INVENTORY_FILE
from harness_hub.state import StateStore
from harness_hub.views import apps_view, entries_view, module_view

from .conftest import HUB_HOST, FakeDaemon

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")
if NODE is None:
    if os.environ.get("CI"):
        raise RuntimeError("the state machine drives the shell's status module and needs Node.js on PATH")
    pytest.skip("Node.js is not on PATH; the state machine drives the shell's status module", allow_module_level=True)

DAEMON_URL = "https://daemon.test"
ORIGIN = {"Origin": f"http://{HUB_HOST}", "X-Hub-Request": "1"}
PAGES = ("overview", "settings/health", "settings/config", "modules", "apps", "marketplace")
# The daemon reply a held read waits on: the one the page's answer is built from.
HELD_PATH = {"overview": "/health", "settings/health": "/health", "settings/config": "/api/admin/v1/config",
             "modules": "/api/admin/v1/hub", "apps": "/api/admin/v1/hub", "marketplace": "/api/admin/v1/hub"}
HEALTH_PAGES = ("overview", "settings/health")
INVENTORY_PAGES = ("modules", "apps", "marketplace")
# Settings the fake applies live, and ones it keeps pending until a restart.
LIVE, RESTART = ("queue.limit",), ("listen.idle", "search.enabled")
MAX_IN_FLIGHT = 3
WAIT = 10.0  # seconds; only a broken Hub waits this long
LEAK = "auth failed for ha-" + "A1b2C3d4" * 6

settings.register_profile(
    "hub-state-machine",
    max_examples=int(os.environ.get("HUB_STATE_MACHINE_EXAMPLES", "200")),
    stateful_step_count=25,
    derandomize=True,
    database=None,
    deadline=None,
    print_blob=False,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
)


class Shell:
    """The browser's status module in one Node process, shared by every example."""

    def __init__(self):
        self.process = subprocess.Popen([NODE, str(ROOT / "tests" / "status_driver.mjs")], stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, text=True, encoding="utf-8", cwd=ROOT)

    def send(self, **command) -> dict:
        self.process.stdin.write(json.dumps(command) + "\n")
        self.process.stdin.flush()
        answer = json.loads(self.process.stdout.readline())
        assert "error" not in answer, answer["error"]
        return answer

    def close(self):
        self.process.stdin.close()
        self.process.wait(timeout=WAIT)


_shell: Shell | None = None


def shell() -> Shell:
    global _shell
    if _shell is None or _shell.process.poll() is not None:
        _shell = Shell()
    return _shell


@pytest.fixture(scope="module", autouse=True)
def _close_shell():
    yield
    if _shell is not None:
        _shell.close()


class Gate:
    """One daemon request, held before the daemon sees it (`before`) or after it answered, until the test lets it on."""

    def __init__(self, path: str, before: bool):
        self.path, self.before = path, before
        self.reached = threading.Event()
        self.release = threading.Event()
        self.sample: tuple[int, object] | None = None


class HoldingTransport(httpx.AsyncBaseTransport):
    """The fake's transport, able to hold the next reply on one path and recording what the daemon last answered."""

    def __init__(self, inner: httpx.AsyncBaseTransport):
        self.inner = inner
        self.armed: Gate | None = None
        self.lock = threading.Lock()
        self.last: dict[str, tuple[int, object] | None] = {}  # path -> (status, body), None when unreachable

    async def handle_async_request(self, request):
        path = request.url.path
        with self.lock:
            gate = self.armed if self.armed and self.armed.path == path else None
            if gate:
                self.armed = None
        if gate and gate.before:
            await self._hold(gate)
        try:
            response = await self.inner.handle_async_request(request)
            await response.aread()
            try:
                sample = (response.status_code, json.loads(response.content))
            except ValueError:
                sample = (response.status_code, None)
        except httpx.TransportError:
            response, sample = None, None
        if gate:
            gate.sample = sample
            if not gate.before:
                await self._hold(gate)
        else:
            self.last[path] = sample
        if response is None:
            raise httpx.ConnectError("refused", request=request)
        return response

    @staticmethod
    async def _hold(gate: Gate) -> None:
        gate.reached.set()
        assert await asyncio.to_thread(gate.release.wait, WAIT), "a held daemon request was never released"


class Read:
    """A Hub read the browser started: its start order, and what the model knew when it started."""

    def __init__(self, page: str, order: int, epoch: int, started_at: float):
        self.page, self.order, self.epoch, self.started_at = page, order, epoch, started_at
        self.gate: Gate | None = None
        self.future: Future | None = None
        self.sample: tuple[int, object] | None = None


class HubMachine(RuleBasedStateMachine):
    def __init__(self):
        super().__init__()
        self.grace = server.RESTART_GRACE_SECONDS
        # The generation check alone must keep a restart honest; the grace second is only a heuristic on top of it.
        server.RESTART_GRACE_SECONDS = 0
        self.dir = Path(tempfile.mkdtemp(prefix="hub-sm-"))
        self.fake = FakeDaemon()
        token = "ho-" + secrets.token_urlsafe(32)
        self.fake.hub_token = token
        self.fake.live_tokens.add(token)
        self.fake.hub = {"key_id": "k-statemch", "name": "Hub", "kind": "native", "origin": "",
                         "request_id": "pr-statemachine", "claimed_at": time.time()}
        StateStore(self.dir).update(key={"token": token, "key_id": "k-statemch", "daemon_url": DAEMON_URL,
                                         "claimed_at": time.time()})
        self.transport = HoldingTransport(self.fake.transport())
        app = server.create_app(Settings(daemon_url=DAEMON_URL, state_dir=self.dir, poll_seconds=0.02),
                                transport=self.transport)
        self.client = TestClient(app, base_url=f"http://{HUB_HOST}")
        self.client.__enter__()
        code = create_signin_code(self.dir)
        signed_in = self.client.post("/api/session", json={"code": code}, headers={"X-Hub-Request": "1"})
        assert signed_in.status_code == 204, signed_in.text
        self.client.headers["X-Hub-Session"] = signed_in.headers["x-hub-session"]
        self.pool = ThreadPoolExecutor(max_workers=MAX_IN_FLIGHT, thread_name_prefix="hub-sm")
        self.in_flight: list[Read] = []
        self.shell = shell()
        self.shell.send(op="reset")
        self.shown: dict = {}
        self.order = 0  # api.mjs's request start order
        # The model: Hub restarts accepted (the server's generation), whether the newest one has been seen to end and
        # its target, the revision the daemon runs, and the newest Settings read the shell holds.
        self.epoch, self.ended, self.target = 0, True, None
        self.applied = self.fake.revision
        self.config: tuple[int, dict] | None = None
        self.restart_evidence: tuple[int, bool] = (0, False)

    def teardown(self):
        for read in self.in_flight:
            read.gate.release.set()
        self.pool.shutdown(wait=True)
        self.client.__exit__(None, None, None)
        server.RESTART_GRACE_SECONDS = self.grace
        shutil.rmtree(self.dir, ignore_errors=True)

    # --- the browser -----------------------------------------------------------------------------------------------
    def _get(self, page: str) -> httpx.Response:
        return self.client.get("/api/" + page)

    def _start(self, page: str) -> Read:
        self.order += 1
        return Read(page, self.order, self.epoch, time.time())

    def _sync(self) -> None:
        """Bring the model up to what the daemon did during the last request: with nothing pending and no restart
        under way, it runs its latest revision (a restart boots whatever is pending when the daemon starts)."""
        if self.fake.pending_revision is None and not self.fake.restart_requested:
            self.applied = self.fake.revision

    def _finish(self, read: Read, response: httpx.Response) -> None:
        self._sync()
        body = response.json() if response.content else None
        text = response.text
        # No reply is withheld, and none carries a credential-like string: a cached inventory included (#89).
        assert response.status_code != 500 and response.content != WITHHELD, (read.page, text)
        assert not TOKEN_SHAPE.search(text) and self.fake.hub_token not in text, (read.page, text)
        if read.page in HEALTH_PAGES and response.status_code == 200:
            self._check_restart(read, body)
        if read.page in INVENTORY_PAGES and response.status_code == 200:
            self._check_inventory(read, body)
        if read.page == "settings/config" and response.status_code == 200 and (
                self.config is None or read.order > self.config[0]):
            self.config = (read.order, body)
        if read.page in HEALTH_PAGES and response.status_code == 200 and read.order > self.restart_evidence[0]:
            self.restart_evidence = (read.order, body["restarting"] is True)
        self.shown = self.shell.send(op="read", path=read.page, status=response.status_code, data=body,
                                     started=read.order)
        self._check_shell()

    def _check_restart(self, read: Read, body: dict) -> None:
        """The server's restart flag, exactly: health requested in this restart's generation, confirmed at its target
        or a later revision (a save made since), ends it, and nothing else does (#100). The fake answers 503 until its
        restart is over, so such health is the restarted daemon's."""
        sample = read.sample
        health = sample[1] if sample and sample[0] == 200 and isinstance(sample[1], dict) else None
        config = health.get("config", {}) if health else {}
        if (self.epoch and read.epoch == self.epoch and health and health.get("ok") is True
                and config.get("confirmed") is True and config.get("revision", -1) >= self.target):
            self.ended = True
        assert body["restarting"] is (not self.ended), (
            f"{read.page} read at restart generation {read.epoch} answered restarting={body['restarting']} "
            f"during generation {self.epoch} (model: ended={self.ended}, health={config})")

    def _check_inventory(self, read: Read, body: dict) -> None:
        """Every page that does not show what the daemon just answered says why."""
        assert body["state"] in ("ready", "error", "absent", "disabled"), body
        if body["state"] != "ready":
            assert body["error"], body
        assert body["stale"] is (body["state"] != "ready" and body["fetched_at"] is not None), body
        if body["state"] == "ready":
            assert body["fetched_at"] >= read.started_at - 1, "a ready page showed an inventory older than its read"

    def _check_shell(self) -> None:
        banner = self.shown["banner"]
        # "Restart needed" stays until the daemon runs the saved revision the shell last read (#101).
        if self.config is not None:
            saved = self.config[1].get("pending_revision")
            if self.config[1].get("restart_required") and isinstance(saved, int) and self.applied < saved:
                assert banner is not None, (
                    f"the shell cleared restart needed for revision {saved}; the daemon runs {self.applied}")
        # The newest restart evidence wins: a stale reply never brings the restart banner back, nor hides it.
        restarting = banner is not None and banner["title"] == "The daemon is restarting."
        if not self.restart_evidence[1]:
            assert not restarting, f"the shell shows a restart its newest evidence says is over: {banner}"
        else:
            assert banner is not None, "the shell lost a restart its newest evidence says is under way"

    # --- rules ------------------------------------------------------------------------------------------------------
    @rule(page=st.sampled_from(PAGES))
    def visit(self, page):
        read = self._start(page)
        response = self._get(page)
        read.sample = self.transport.last.get(HELD_PATH[page])
        self._finish(read, response)

    @precondition(lambda self: len(self.in_flight) < MAX_IN_FLIGHT)
    @rule(page=st.sampled_from(PAGES), before=st.booleans())
    def begin_read(self, page, before=False):
        read = self._start(page)
        read.gate = Gate(HELD_PATH[page], before)
        with self.transport.lock:
            self.transport.armed = read.gate
        read.future = self.pool.submit(self._get, page)
        deadline = time.monotonic() + WAIT
        while not read.gate.reached.wait(0.005):
            assert not read.future.done(), read.future.result().text  # the page answered without asking the daemon
            assert time.monotonic() < deadline, f"{page} never asked the daemon for {read.gate.path}"
        self.in_flight.append(read)

    @precondition(lambda self: self.in_flight)
    @rule(data=st.data())
    def finish_read(self, data):
        self.finish(data.draw(st.integers(0, len(self.in_flight) - 1), label="read"))

    def finish(self, index: int) -> None:
        """Let the read in flight at `index` (0 is the oldest) reach the daemon or the Hub, and take its reply."""
        read = self.in_flight.pop(index)
        read.gate.release.set()
        response = read.future.result(timeout=WAIT)
        read.sample = read.gate.sample
        self._finish(read, response)

    @rule(key=st.sampled_from(LIVE + RESTART), value=st.integers(1, 9))
    def save(self, key, value):
        self.order += 1
        change = {"search.enabled": value % 2 == 0}.get(key, value)
        response = self.client.patch("/api/settings/config", headers=ORIGIN,
                                     json={"revision": self.fake.revision, "changes": {key: change}})
        assert response.status_code in (200, 503), response.text
        self._sync()

    @rule()
    def restart(self):
        self.order += 1
        response = self.client.post("/api/settings/restart", headers=ORIGIN,
                                    json={"revision": self.fake.revision, "confirm": True})
        assert response.status_code in (202, 503), response.text
        if response.status_code == 202:
            # The daemon stays down until finish_restart; the Hub sees its 503s until then.
            self.fake.restart_polls = 10**9
            self.epoch, self.ended, self.target = self.epoch + 1, False, response.json()["target_revision"]
            self.order += 1
            self.restart_evidence = (self.order, True)
            self.shown = self.shell.send(op="restart", started=self.order, target=self.target)
            self._check_shell()
        self._sync()

    @precondition(lambda self: self.fake.restart_requested)
    @rule()
    def finish_restart(self):
        self.fake.restart_polls = 0  # the next /health is the restarted daemon's, at the revision it accepted

    @rule(down=st.booleans())
    def daemon_answers(self, down):
        self.fake.down = down

    @rule(kind=st.sampled_from(["generated", "changed", "leaks_on_modules", "leaks_on_apps"]))
    def inventory_changes(self, kind):
        from .apps_fake import inventory_apps

        clean = [{"name": "hub", "state": "present", "status": {"entries": 2}},
                 {"name": "search", "state": "switched_off"}]
        apps = inventory_apps(self.fake)
        if kind == "leaks_on_apps":
            apps[0]["name"] = "x" * 220 + self.fake.hub_token
        self.fake.inventory_override = None if kind == "generated" else {
            "modules": [{"name": "hub", "state": "present", "status": {"last_error": LEAK}}]
            if kind == "leaks_on_modules" else clean, "apps": apps, "entries": []}

    @invariant()
    def cached_inventory_is_clean_on_every_page(self):
        path = self.dir / INVENTORY_FILE
        if not path.exists():
            return
        body = json.loads(path.read_text(encoding="utf-8"))["inventory"]
        secrets_ = (self.fake.hub_token,)
        views = (module_view(body), apps_view(body, "k-statemch", secrets_), entries_view(body, secrets_))
        leaked = leaks(json.dumps(views).encode(), secrets_)
        assert not leaked, "the shared cache holds a snapshot one of its pages would leak"


TestHubStateMachine = HubMachine.TestCase
TestHubStateMachine.settings = settings.get_profile("hub-state-machine")


# --- deterministic interleavings -----------------------------------------------------------------------------------
@pytest.fixture
def machine():
    instance = HubMachine()
    yield instance
    instance.teardown()


def test_89_an_apps_or_marketplace_visit_never_poisons_the_cache_for_modules(machine):
    """#89: Modules withholds a leaking snapshot; Apps (or Marketplace) must not cache it for Modules to show."""
    for writer in ("apps", "marketplace"):
        machine.visit("modules")
        machine.inventory_changes("leaks_on_modules")
        machine.visit("modules")
        machine.visit(writer)
        machine.cached_inventory_is_clean_on_every_page()
        machine.visit("modules")
        machine.inventory_changes("generated")


def test_100_health_read_before_a_restart_cannot_end_it(machine):
    """#100: Overview reads health, a restart starts while that read is in flight, then Overview finishes."""
    for page in HEALTH_PAGES:
        machine.begin_read(page)
        machine.restart()
        machine.finish(0)
        machine.finish_restart()
        machine.visit("settings/health")


def test_101_a_revision_saved_after_a_restart_ended_still_needs_one(machine):
    """#101: a Settings read starts during a restart to revision 4 but reaches the daemon only after the restart ended
    and another tab saved revision 5; health read after it started confirms revision 4 with the restart over."""
    machine.save("listen.idle", 2)
    machine.restart()
    machine.begin_read("settings/config", before=True)
    machine.visit("settings/health")  # the restart is still under way
    machine.finish_restart()
    machine.visit("settings/health")  # confirmed at revision 4: the restart is over
    machine.save("listen.idle", 3)  # revision 5, pending
    machine.finish(0)  # the shell must keep restart needed for revision 5


def test_143_health_at_the_saved_revision_is_not_a_restart(machine):
    """Found by this machine: the daemon's /health reports a revision as soon as it is saved, so health at the saved
    revision must not clear restart needed before any restart applied it."""
    machine.save("listen.idle", 2)
    machine.visit("settings/config")
    machine.visit("overview")
    machine.restart()
    machine.finish_restart()
    machine.visit("settings/health")


def test_143_a_save_during_a_restart_does_not_keep_it_running(machine):
    """Found by this machine: a revision saved while a restart is under way is the one the restarted daemon reports,
    so confirmed health at it ends the restart; at the old target only, the banner and the health poll never stopped."""
    machine.save("listen.idle", 2)
    machine.visit("settings/config")
    machine.restart()
    machine.save("listen.idle", 3)
    machine.visit("settings/config")
    machine.finish_restart()
    machine.visit("settings/health")
    machine.visit("overview")
