"""End to end against a real Agent Harness daemon started from a checkout (synthetic data, temporary directories).

    HUB_E2E_DAEMON_SRC=../agent-harness pytest -m e2e

The daemon runs the service profile with the Hub module on, on a free loopback port. The Hub is started exactly as the
daemon installer starts it (`--daemon-url --claim-file --state-dir`), the owner approves on the "host" with the
daemon's own CLI, and every response the browser could see is checked for the Hub key and `ha-`/`ho-`/`hp-` strings.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
import uuid
from contextlib import ExitStack
from pathlib import Path

import httpx
import pytest

from harness_hub.state import STATE_FILE
from harness_hub.views import ELEVATED_SCOPES, SCOPE_DISCLOSURES

from ..conftest import assert_no_secrets
from ..response_capture import capture_response

pytestmark = pytest.mark.e2e

SRC = os.environ.get("HUB_E2E_DAEMON_SRC", "")
DAEMON_PYTHON = os.environ.get("HUB_E2E_DAEMON_PYTHON", sys.executable)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait(check, timeout: float, what: str):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            value = check()
        except (httpx.HTTPError, OSError, ValueError):
            value = None
        if value:
            return value
        time.sleep(0.25)
    raise AssertionError(f"timed out waiting for {what}")


@pytest.fixture(scope="module")
def daemon(tmp_path_factory):
    if not SRC:
        pytest.skip("set HUB_E2E_DAEMON_SRC to an agent-harness checkout")
    src = Path(SRC).resolve()
    root = tmp_path_factory.mktemp("daemon")
    config, data, repos = root / "config", root / "data", root / "repos"
    for d in (config, data, repos):
        d.mkdir()
    for name in ("harness.yaml", "projects.yaml"):
        shutil.copy(src / "config" / name, config / name)
    port = free_port()
    (config / "harness.local.yaml").write_text(
        f"listen:\n  port: {port}\ndata_dir: {data.as_posix()}\nrepos_dir: {repos.as_posix()}\n"
        "notify:\n  enabled: false\nallowed_logins: [owner@example.test]\n", encoding="utf-8")
    (config / "profile.yaml").write_text("profile: service\nmodules:\n  hub: true\n", encoding="utf-8")
    log = (root / "daemon.log").open("w")
    def launch():
        return subprocess.Popen([DAEMON_PYTHON, "-m", "harness", "--config-dir", str(config)], cwd=src,
                                env=dict(os.environ, HARNESS_SUPERVISED="1"),
                                stdout=log, stderr=subprocess.STDOUT)

    processes = [launch()]
    proc = processes[0]
    stopping = threading.Event()

    def supervise():
        # A test-only supervisor exercises the daemon's supported restart contract.
        while not stopping.is_set():
            processes[-1].wait()
            if not stopping.is_set():
                processes.append(launch())

    supervisor = threading.Thread(target=supervise, daemon=True)
    supervisor.start()
    url = f"http://127.0.0.1:{port}"
    try:
        wait(lambda: httpx.get(url + "/health", timeout=2).status_code == 200, 90, "the daemon to start")
    except AssertionError:
        stopping.set()
        processes[-1].kill()
        supervisor.join(15)
        log.close()
        raise AssertionError((root / "daemon.log").read_text(encoding="utf-8")[-4000:]) from None
    env = dict(os.environ, HARNESS_CONFIG_DIR=str(config), HARNESS_URL=url)
    env.pop("HARNESS_TOKEN", None)
    env.pop("HARNESS_LOCAL_TOKEN", None)
    yield {"url": url, "src": src, "env": env, "root": root, "processes": processes}
    stopping.set()
    proc = processes[-1]
    proc.terminate()
    try:
        proc.wait(15)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(15)
    supervisor.join(15)
    log.close()


def host_cli(daemon, *args) -> subprocess.CompletedProcess:
    """`harness ...` on the daemon host, with a fresh client config (no paired credentials), as the installer does."""
    client = Path.home() / ".agent-harness" / f"hub-e2e-{uuid.uuid4().hex}.json"
    result = subprocess.run([DAEMON_PYTHON, "-m", "harness.cli", "--config", str(client), *args], cwd=daemon["src"],
                            env=daemon["env"], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr + result.stdout
    return result


class Hub:
    def __init__(self, daemon, state_dir: Path, *, claim_file: Path | None = None):
        self.port = free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.state_dir = state_dir
        args = [sys.executable, "-m", "harness_hub", "--daemon-url", daemon["url"], "--state-dir", str(state_dir),
                "--port", str(self.port)]
        if claim_file:
            args += ["--claim-file", str(claim_file)]
        self.bodies: list[str] = []
        self._cleanup = ExitStack()
        try:
            self.log = self._cleanup.enter_context((state_dir.parent / f"hub-{self.port}.log").open("w"))
            self.proc = subprocess.Popen(args, stdout=self.log, stderr=subprocess.STDOUT)
            self._cleanup.callback(self._stop_process)
            self.http = httpx.Client(base_url=self.url, timeout=10, headers={"X-Hub-Request": "1"})
            self._cleanup.callback(self.http.close)
            wait(lambda: self.http.get("/healthz").status_code == 200, 30, "the Hub to start")
        except BaseException as error:
            try:
                self.stop()
            except BaseException as cleanup_error:
                error.add_note(f"Hub startup cleanup also failed: {cleanup_error!r}")
            raise

    def call(self, method: str, path: str, **kw) -> httpx.Response:
        response = self.http.request(method, path, **kw)
        return capture_response(self.bodies, response)

    def sign_in(self):
        link = subprocess.run([sys.executable, "-m", "harness_hub", "signin-link", "--state-dir", str(self.state_dir)],
                              capture_output=True, text=True, check=True, timeout=60).stdout.strip()
        assert link.startswith(self.url + "/#signin=")
        response = self.call("POST", "/api/session", json={"code": link.split("#signin=", 1)[1]})
        assert response.status_code == 204
        self.http.headers["X-Hub-Session"] = response.headers["x-hub-session"]

    def claim_state(self) -> dict:
        return self.call("GET", "/api/claim").json()

    def token(self) -> str:
        return (json.loads((self.state_dir / STATE_FILE).read_text(encoding="utf-8")).get("key") or {}).get("token", "")

    def stop(self):
        # ExitStack attempts every cleanup even if one fails, and drains callbacks only once.
        self._cleanup.close()

    def _stop_process(self):
        try:
            if self.proc.poll() is None:
                self.proc.terminate()
            self.proc.wait(timeout=15)
        except (subprocess.TimeoutExpired, OSError):
            try:
                self.proc.kill()
            finally:
                self.proc.wait(timeout=15)


def test_install_claim_overview_refuse_release_deny(daemon, tmp_path):
    # 1. The installer's start: the Hub claims at once and publishes only the id and match code.
    state = tmp_path / "hub-a"
    claim_file = state / "claim.json"
    first = Hub(daemon, state, claim_file=claim_file)
    second = None
    try:
        published = wait(lambda: json.loads(claim_file.read_text(encoding="utf-8")), 30, "the claim file")
        assert set(published) == {"id", "match_code"}

        # 2. The owner approves on the daemon host; the Hub redeems its key by itself.
        host_cli(daemon, "hub", "approve", published["id"], "--match", published["match_code"])
        first.sign_in()
        wait(lambda: first.claim_state()["state"] == "claimed", 30, "the Hub to redeem its key")
        token = first.token()
        assert token.startswith("ho-")

        # 3. Overview from the real daemon.
        overview = first.call("GET", "/api/overview").json()
        assert overview["reachable"] and overview["error"] == ""
        assert overview["ownership"]["this_hub"] is True and overview["ownership"]["daemon_has_hub"] is True
        assert overview["capabilities"]["modules"]["hub"] is True
        assert overview["daemon"]["release"]
        first.call("GET", "/")

        # Settings are edited through the Hub with optimistic concurrency, then
        # the real daemon exits and the test supervisor starts its saved generation.
        schema = first.call("GET", "/api/settings/schema")
        assert schema.status_code == 200 and schema.json()["settings"]
        config = first.call("GET", "/api/settings/config").json()
        row = next(s for s in config["settings"] if s["key"] == "sessions.max_turns")
        payload = {"revision": config["revision"], "changes": {"sessions.max_turns": row["effective"] + 1}}
        origin = {"Origin": first.url}
        validated = first.call("POST", "/api/settings/validate", json=payload, headers=origin)
        assert validated.status_code == 200, validated.text
        saved = first.call("PATCH", "/api/settings/config", json=payload, headers=origin)
        assert saved.status_code == 200, saved.text
        revision = saved.json()["revision"]
        assert revision > config["revision"]
        stale = first.call("PATCH", "/api/settings/config", json=payload, headers=origin)
        assert stale.status_code == 409 and stale.json()["error"]["code"] == "revision_conflict"
        old_process = daemon["processes"][-1]
        started = first.call("POST", "/api/settings/restart", headers=origin,
                             json={"revision": revision, "confirm": True})
        assert started.status_code == 202, started.text
        assert first.call("GET", "/api/settings/health").json()["restarting"] is True
        wait(lambda: old_process.poll() is not None and len(daemon["processes"]) > 1,
             30, "the daemon process to restart")
        restored = wait(lambda: (data if (data := first.call("GET", "/api/settings/health").json())
                                ["reachable"] and not data["restarting"] else None),
                        90, "the new daemon's confirmed health")
        assert restored["health"]["config_revision"] == revision
        after = first.call("GET", "/api/settings/config").json()
        assert after["revision"] == revision
        changed = next(s for s in after["settings"] if s["key"] == "sessions.max_turns")
        assert changed["effective"] == row["effective"] + 1
        assert first.call("GET", "/api/overview").json()["restarting"] is False

        # The inventory switch is live and harmless: it leaves claims and the
        # settings registry running, so Modules can switch it back on.
        inventory = first.call("GET", "/api/modules").json()
        assert inventory["state"] == "ready"
        assert next(m for m in inventory["modules"] if m["name"] == "hub")["state"] == "present"
        switch = next(s for s in schema.json()["settings"] if s["type"] == "bool" and
                      s["key"].endswith(".enabled") and "hub" in s["modules"])
        for enabled in (False, True):
            current = first.call("GET", "/api/settings/config").json()
            answer = first.call("PATCH", "/api/settings/config", headers=origin,
                                json={"revision": current["revision"], "changes": {switch["key"]: enabled}})
            assert answer.status_code == 200, answer.text
            result = first.call("GET", "/api/modules").json()
            assert result["state"] == ("ready" if enabled else "disabled")
            if not enabled:
                assert result["stale"] and result["modules"] == inventory["modules"]
        entries = daemon["root"] / "config" / "hub.entries.json"
        entries.write_text("{invalid", encoding="utf-8")
        try:
            broken = first.call("GET", "/api/modules").json()
            assert broken["state"] == "error" and "hub.entries: invalid JSON" in broken["error"]
            assert broken["stale"] and broken["modules"]
        finally:
            entries.unlink()
        assert first.call("GET", "/api/modules").json()["state"] == "ready"

        # Apps are created and restored on the host; the Hub only sees token-free metadata.
        created = json.loads(host_cli(daemon, "keys", "create", "Synthetic App", "--kind", "app").stdout)
        app_id, app_token = created["id"], created["key"]
        apps = first.call("GET", "/api/apps").json()
        app_row = next(app for app in apps["apps"] if app["id"] == app_id)
        assert app_row["state"] == "never_used" and not app_row["token_age_warning"]
        assert next(app for app in apps["apps"] if app["this_hub"])["role"] == "hub"
        caps = first.call("PUT", f"/api/apps/{app_id}/limits", headers=origin,
                          json={"max_running": 3, "max_queued": None})
        assert caps.status_code == 200 and caps.json()["effective"]["max_running"] == 3
        retention = first.call("PUT", f"/api/apps/{app_id}/retention", headers=origin,
                               json={"retention_days": 7.5, "confirm": True})
        assert retention.status_code == 200 and retention.json()["retention_days"] == 7.5
        assert first.call("GET", f"/api/apps/{app_id}/limits").json()["retention_days"] == 7.5
        assert first.call("DELETE", f"/api/apps/{app_id}/revoke", headers=origin,
                          json={"confirm": True}).status_code == 204
        revoked = next(app for app in first.call("GET", "/api/apps").json()["apps"] if app["id"] == app_id)
        assert revoked["state"] == "erasure_pending" and revoked["erase_after"] is not None
        assert app_id in {app["id"] for app in first.call("GET", "/api/apps/erasures").json()}
        assert first.call("POST", f"/api/apps/{app_id}/restore", headers=origin,
                          json={"confirm": True}).status_code == 404
        restored_app = json.loads(host_cli(daemon, "apps", "restore", app_id).stdout)
        assert restored_app["id"] == app_id and restored_app["key"] != app_token
        refreshed = next(app for app in first.call("GET", "/api/apps").json()["apps"] if app["id"] == app_id)
        assert refreshed["revoked_at"] is None and refreshed["erase_after"] is None
        assert_no_secrets(first.bodies, app_token, restored_app["key"], token)

        # Read every moved control; disabled modules return their actual error or
        # disabled state without ever exposing provider credentials.
        for resource in ("smart-approvals", "backends", "memory", "github", "google"):
            response = first.call("GET", "/api/settings/" + resource)
            assert response.status_code in (200, 404), response.text

        # Owner administration: only aggregate household metadata is visible.
        member = first.call("POST", "/api/manage/account_add", headers=origin,
                            json={"login": "member@example.test", "display_name": "Synthetic member"})
        assert member.status_code == 201, member.text
        member_id = member.json()["user_id"]
        members = first.call("GET", "/api/manage/accounts").json()
        assert any(row["user_id"] == member_id and row["enabled"] for row in members)
        disabled = first.call("PATCH", "/api/manage/account_save", headers=origin,
                              params={"user_id": member_id}, json={"enabled": False, "confirm": True})
        assert disabled.status_code == 200 and disabled.json()["enabled"] is False, disabled.text
        detail = first.call("GET", "/api/manage/account", params={"user_id": member_id})
        assert detail.status_code == 200 and detail.json()["enabled"] is False
        maintenance = first.call("GET", "/api/manage/maintenance")
        assert maintenance.status_code == 200, maintenance.text
        assert maintenance.json()["free_gb"] >= 0
        assert_no_secrets(first.bodies, token)

        # Pairing requests (#519): an App asks with the SDK-shaped call and keeps its own verifier. The owner approves
        # from the Hub with the code the App shows; the App redeems its own token, and no Hub response contains it.
        def app_asks(name, scopes):
            verifier = secrets.token_urlsafe(48)
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
            asked = httpx.post(daemon["url"] + "/api/v1/pair/requests", timeout=10, json={
                "name": name, "scopes": scopes, "catalog_app_id": "test.example.notes",
                "code_challenge": challenge, "code_challenge_method": "S256"})
            assert asked.status_code == 201, asked.text
            return asked.json(), verifier

        def app_redeems(rid, verifier):
            return httpx.post(f"{daemon['url']}/api/v1/pair/requests/{rid}/token", timeout=10,
                              json={"code_verifier": verifier})

        def listed(rid):
            reply = first.call("GET", "/api/pairing")
            assert reply.status_code == 200, reply.text
            return next(row for row in reply.json()["requests"] if row["id"] == rid)

        asked, verifier = app_asks("Synthetic notes", ["sessions"])
        rid, code = asked["id"], asked["match_code"]
        row = listed(rid)
        assert row["state"] == "pending" and row["needs"] == "approve" and "match_code" not in row
        assert row["origin"] == "" and row["catalog_app_id"] == "test.example.notes"
        assert row["disclosures"][0]["scope"] == "sessions" and row["disclosures"][0]["text"]
        hub_rows = [r for r in first.call("GET", "/api/pairing").json()["requests"] if r["kind"] == "hub"]
        assert hub_rows and all("match_code" not in r for r in hub_rows)
        assert app_redeems(rid, verifier).status_code == 409  # nothing exists before the owner decides
        wrong = "000000" if code != "000000" else "111111"
        refused = first.call("POST", f"/api/pairing/{rid}/approve", headers=origin, json={"match": wrong})
        assert refused.status_code == 400 and "match code" in refused.json()["detail"], refused.text
        approved = first.call("POST", f"/api/pairing/{rid}/approve", headers=origin, json={"match": code})
        assert approved.status_code == 200 and approved.json()["state"] == "approved", approved.text
        assert first.call("POST", f"/api/pairing/{rid}/approve", headers=origin,
                          json={"match": code}).status_code == 409
        redeemed = app_redeems(rid, verifier)
        assert redeemed.status_code == 201, redeemed.text
        paired_token, paired_id = redeemed.json()["token"], redeemed.json()["app"]["id"]
        assert paired_token.startswith("ha-")
        row = listed(rid)
        assert row["state"] == "redeemed" and row["key_id"] == paired_id and "match_code" not in row
        assert any(app["id"] == paired_id for app in first.call("GET", "/api/apps").json()["apps"])

        # Elevated scopes need the separate acknowledgement; then the owner withdraws the unredeemed approval.
        asked, verifier = app_asks("Synthetic approver", ["sessions", "approvals"])
        rid, code = asked["id"], asked["match_code"]
        assert listed(rid)["elevated"] == ["approvals"]
        refused = first.call("POST", f"/api/pairing/{rid}/approve", headers=origin, json={"match": code})
        assert refused.status_code == 400 and refused.json()["error"]["code"] == "acknowledgement_required"
        assert listed(rid)["state"] == "pending"
        approved = first.call("POST", f"/api/pairing/{rid}/approve", headers=origin,
                              json={"match": code, "acknowledge_elevated": True})
        assert approved.status_code == 200 and approved.json()["state"] == "approved", approved.text
        assert first.call("POST", f"/api/pairing/{rid}/deny", headers=origin, json={}).status_code == 400
        withdrawn = first.call("POST", f"/api/pairing/{rid}/deny", headers=origin, json={"confirm": True})
        assert withdrawn.status_code == 200 and withdrawn.json()["state"] == "denied", withdrawn.text
        assert app_redeems(rid, verifier).status_code == 403
        assert first.call("POST", "/api/pairing/pr-unknown/deny", headers=origin,
                          json={"confirm": True}).status_code == 404
        hub_rid = hub_rows[0]["id"]
        assert first.call("POST", f"/api/pairing/{hub_rid}/deny", headers=origin,
                          json={"confirm": True}).status_code == 403
        assert_no_secrets(first.bodies, paired_token, token)

        # Marketplace (#520, #519 flow 4): the host's example entries list as not paired, and listing grants nothing.
        host_cli(daemon, "hub", "entries", "init")

        def market():
            reply = first.call("GET", "/api/marketplace")
            assert reply.status_code == 200, reply.text
            return reply.json()

        def entry(data, catalog_app_id):
            return next(row for row in data["entries"] if row["catalog_app_id"] == catalog_app_id)

        web_id, planner_id = "ahpub.dflippojr.agent-harness-web", "ahpub.dflippojr.financial-planner"
        listed = market()
        assert listed["state"] == "ready" and listed["withheld"] == 0 and listed["daemon_url"] == daemon["url"]
        assert {row["catalog_app_id"] for row in listed["entries"]} == {web_id, planner_id}
        for row in listed["entries"]:
            assert row["state"] == "not_paired" and not row["pending_pairing"] and row["verified"] is False
            assert row["slots"] == [] and row["icon"] == ""
        assert "example.invalid" not in first.bodies[-1]

        # A native entry: arm, the App claims with its own challenge, the owner types the code the App shows into the
        # Hub (which never shows it), and the App redeems. Its key has no origin, so it is listed under
        # unverified_origin, not as paired.
        def app_claims(rid, origin=""):
            verifier = secrets.token_urlsafe(48)
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
            claimed_slot = httpx.post(f"{daemon['url']}/api/v1/pair/requests/{rid}/claim", timeout=10,
                                      headers={"Origin": origin} if origin else {},
                                      json={"code_challenge": challenge, "code_challenge_method": "S256"})
            assert claimed_slot.status_code == 200, claimed_slot.text
            return claimed_slot.json(), verifier

        armed = first.call("POST", "/api/marketplace/arm", headers=origin,
                           json={"catalog_app_id": planner_id, "scopes": ["sessions"],
                                 "consent_digest": entry(market(), planner_id)["consent_digest"]})
        assert armed.status_code == 201, armed.text
        slot = armed.json()["request"]
        assert slot["state"] == "armed" and slot["origin"] == "" and armed.json()["daemon_url"] == daemon["url"]
        # Before arming the owner read the Hub's copy of the disclosures; the daemon's copy on the slot is the same.
        assert slot["disclosures"] == [{k: v for k, v in item.items() if k != "justification"}
                                       for item in entry(listed, planner_id)["scopes"]]
        planner = entry(market(), planner_id)
        assert planner["unverified_origin"]["pending_pairing"] is True and planner["slots"][0]["id"] == slot["id"]
        shown, verifier = app_claims(slot["id"])
        planner = entry(market(), planner_id)
        assert planner["slots"][0]["state"] == "claimed" and "match_code" not in planner["slots"][0]
        assert app_redeems(slot["id"], verifier).status_code == 409
        confirmed = first.call("POST", f"/api/pairing/{slot['id']}/confirm", headers=origin,
                               json={"match": shown["match_code"]})
        assert confirmed.status_code == 200 and confirmed.json()["state"] == "approved", confirmed.text
        redeemed = app_redeems(slot["id"], verifier)
        assert redeemed.status_code == 201, redeemed.text
        native_token, native_id = redeemed.json()["token"], redeemed.json()["app"]["id"]
        planner = entry(market(), planner_id)
        assert planner["state"] == "not_paired" and planner["unverified_origin"]["paired"] == [native_id]
        assert planner["slots"] == []

        # A browser entry: the owner gives the web entry its origin on the host (the Hub never edits the file). The
        # App claims from that exact origin, the owner confirms its App-shown code, and it redeems (#606 or later).
        entries_file = daemon["root"] / "config" / "hub.entries.json"
        document = json.loads(entries_file.read_text(encoding="utf-8"))
        web_origin = "https://notes.example.test"
        next(row for row in document["entries"] if row["app"]["app_id"] == web_id)["app"]["browser_origins"] = [
            web_origin]
        entries_file.write_text(json.dumps(document), encoding="utf-8")
        assert first.call("POST", "/api/marketplace/arm", headers=origin, json={
            "catalog_app_id": web_id, "scopes": ["sessions"]}).status_code == 400
        armed = first.call("POST", "/api/marketplace/arm", headers=origin,
                           json={"catalog_app_id": web_id, "scopes": ["sessions"], "origin": web_origin,
                                 "consent_digest": entry(market(), web_id)["consent_digest"]})
        assert armed.status_code == 201, armed.text
        assert "match_code" not in armed.json()["request"]
        rid = armed.json()["request"]["id"]
        assert entry(market(), web_id)["pending_pairing"] is True
        shown, verifier = app_claims(rid, web_origin)
        assert shown["state"] == "claimed" and shown["match_code"]
        slot = entry(market(), web_id)["slots"][0]
        assert slot["state"] == "claimed" and slot["needs"] == "confirm" and "match_code" not in slot
        for match in ("", "000000" if shown["match_code"] != "000000" else "111111"):
            refused = first.call("POST", f"/api/pairing/{rid}/confirm", headers=origin, json={"match": match})
            assert refused.status_code == 400 and shown["match_code"] not in refused.text
            slot = entry(market(), web_id)["slots"][0]
            assert slot["state"] == "claimed" and "match_code" not in slot
            waiting = httpx.post(f"{daemon['url']}/api/v1/pair/requests/{rid}/token", timeout=10,
                                 headers={"Origin": web_origin}, json={"code_verifier": verifier})
            assert waiting.status_code == 409 and "token" not in waiting.json()
        confirmed = first.call("POST", f"/api/pairing/{rid}/confirm", headers=origin,
                               json={"match": shown["match_code"]})
        assert confirmed.status_code == 200 and confirmed.json()["needs"] == "redeem", confirmed.text
        assert "match_code" not in confirmed.json()
        slot = entry(market(), web_id)["slots"][0]
        assert slot["state"] == "approved" and "match_code" not in slot
        wrong_origin = httpx.post(f"{daemon['url']}/api/v1/pair/requests/{rid}/token", timeout=10,
                                   headers={"Origin": "https://other.example.test"}, json={"code_verifier": verifier})
        assert wrong_origin.status_code == 403
        redeemed = httpx.post(f"{daemon['url']}/api/v1/pair/requests/{rid}/token", timeout=10,
                              headers={"Origin": web_origin}, json={"code_verifier": verifier})
        assert redeemed.status_code == 201, redeemed.text
        web_token, web_key = redeemed.json()["token"], redeemed.json()["app"]["id"]
        web = entry(market(), web_id)
        assert web["state"] == "paired" and web["paired"] == [web_key] and not web["pending_pairing"]

        # An unclaimed slot is withdrawn.
        spare_reply = first.call("POST", "/api/marketplace/arm", headers=origin,
                                 json={"catalog_app_id": planner_id, "scopes": ["sessions"],
                                       "consent_digest": entry(market(), planner_id)["consent_digest"]})
        assert spare_reply.status_code == 201, spare_reply.text
        spare = spare_reply.json()["request"]["id"]
        withdrawn = first.call("POST", f"/api/pairing/{spare}/deny", headers=origin, json={"confirm": True})
        assert withdrawn.status_code == 200 and withdrawn.json()["state"] == "denied", withdrawn.text
        assert entry(market(), planner_id)["slots"] == []

        # An unreadable entries file shows the daemon's named error with the last good entries, and arms nothing.
        good = entries_file.read_text(encoding="utf-8")
        entries_file.write_text("{invalid", encoding="utf-8")
        try:
            broken = market()
            assert broken["state"] == "error" and "hub.entries: invalid JSON" in broken["error"]
            assert broken["stale"] and {row["catalog_app_id"] for row in broken["entries"]} == {web_id, planner_id}
            refused = first.call("POST", "/api/marketplace/arm", headers=origin,
                                 json={"catalog_app_id": planner_id, "scopes": ["sessions"]})
            assert refused.status_code == 409 and "hub.entries: invalid JSON" in refused.json()["detail"]
        finally:
            entries_file.write_text(good, encoding="utf-8")
        assert market()["state"] == "ready"
        assert_no_secrets(first.bodies, native_token, web_token, token)

        # 4. A second Hub is refused with 409 and the release instruction.
        second = Hub(daemon, tmp_path / "hub-b")
        second.sign_in()
        refused = second.call("POST", "/api/claim")
        assert refused.status_code == 409
        assert refused.json()["claim"]["state"] == "refused"
        assert refused.json()["claim"]["release_command"] == "harness hub release --confirm"

        # 5. Release on the host: the first Hub notices and drops its key; the second may now claim.
        host_cli(daemon, "hub", "release", "--confirm")
        assert first.call("GET", "/api/overview").json()["claim"]["state"] == "released"
        assert first.token() == ""
        pending = second.call("POST", "/api/claim").json()
        assert pending["state"] == "pending"
        rid, match = pending["request_id"], pending["match_code"]
        assert pending["approve_command"] == f"harness hub approve {rid} --match {match}"

        # 6. The owner denies it on the host.
        host_cli(daemon, "hub", "deny", pending["request_id"])
        wait(lambda: second.claim_state()["state"] == "denied", 30, "the denial")

        assert_no_secrets(first.bodies + second.bodies, token)
    finally:
        with ExitStack() as cleanup:
            if second:
                cleanup.callback(second.stop)
            cleanup.callback(first.stop)


def test_all_scope_disclosures_and_elevated_tiers_match_pinned_daemon():
    if not SRC:
        pytest.skip("set HUB_E2E_DAEMON_SRC to an agent-harness checkout")
    # Import from precisely the CI fixture checkout, in its interpreter, without starting a daemon.
    result = subprocess.run([DAEMON_PYTHON, "-c",
                             "import json; from harness.pairing_requests import DISCLOSURES, ELEVATED; "
                             "print(json.dumps({'disclosures': DISCLOSURES, 'elevated': sorted(ELEVATED)}))"],
                            cwd=Path(SRC).resolve(), capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    tables = json.loads(result.stdout)
    assert {k: v for k, v in tables["disclosures"].items() if k != "admin"} == SCOPE_DISCLOSURES
    assert set(tables["elevated"]) == ELEVATED_SCOPES
