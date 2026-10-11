"""End to end against a real Agent Harness daemon started from a checkout (synthetic data, temporary directories).

    HUB_E2E_DAEMON_SRC=../agent-harness pytest -m e2e

The daemon runs the service profile with the Hub module on, on a free loopback port. The Hub is started exactly as the
daemon installer starts it (`--daemon-url --claim-file --state-dir`), the owner approves on the "host" with the
daemon's own CLI, and every response the browser could see is checked for the Hub key and `ha-`/`ho-`/`hp-` strings.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

import httpx
import pytest

from harness_hub.state import STATE_FILE

from ..conftest import assert_no_secrets

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
        "notify:\n  enabled: false\n", encoding="utf-8")
    (config / "profile.yaml").write_text("profile: service\nmodules:\n  hub: true\n", encoding="utf-8")
    log = (root / "daemon.log").open("w")
    proc = subprocess.Popen([DAEMON_PYTHON, "-m", "harness", "--config-dir", str(config)], cwd=src,
                            stdout=log, stderr=subprocess.STDOUT)
    url = f"http://127.0.0.1:{port}"
    try:
        wait(lambda: httpx.get(url + "/health", timeout=2).status_code == 200, 90, "the daemon to start")
    except AssertionError:
        proc.kill()
        log.close()
        raise AssertionError((root / "daemon.log").read_text(encoding="utf-8")[-4000:]) from None
    env = dict(os.environ, HARNESS_CONFIG_DIR=str(config), HARNESS_URL=url)
    env.pop("HARNESS_TOKEN", None)
    env.pop("HARNESS_LOCAL_TOKEN", None)
    yield {"url": url, "src": src, "env": env, "root": root}
    proc.terminate()
    try:
        proc.wait(15)
    except subprocess.TimeoutExpired:
        proc.kill()
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
        self.log = (state_dir.parent / f"hub-{self.port}.log").open("w")
        self.proc = subprocess.Popen(args, stdout=self.log, stderr=subprocess.STDOUT)
        self.http = httpx.Client(base_url=self.url, timeout=10, headers={"X-Hub-Request": "1"})
        self.bodies: list[str] = []
        wait(lambda: self.http.get("/healthz").status_code == 200, 30, "the Hub to start")

    def call(self, method: str, path: str, **kw) -> httpx.Response:
        response = self.http.request(method, path, **kw)
        self.bodies.append(response.text)
        self.bodies.extend(v for k, v in response.headers.items() if k.lower() != "set-cookie")
        return response

    def sign_in(self):
        link = subprocess.run([sys.executable, "-m", "harness_hub", "signin-link", "--state-dir", str(self.state_dir)],
                              capture_output=True, text=True, check=True).stdout.strip()
        assert link.startswith(self.url + "/#signin=")
        response = self.call("POST", "/api/session", json={"code": link.split("#signin=", 1)[1]})
        assert response.status_code == 204
        self.http.headers["X-Hub-Session"] = response.headers["x-hub-session"]

    def claim_state(self) -> dict:
        return self.call("GET", "/api/claim").json()

    def token(self) -> str:
        return (json.loads((self.state_dir / STATE_FILE).read_text(encoding="utf-8")).get("key") or {}).get("token", "")

    def stop(self):
        self.http.close()
        self.proc.terminate()
        try:
            self.proc.wait(15)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        self.log.close()


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
        first.stop()
        if second:
            second.stop()
