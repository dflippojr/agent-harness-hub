"""The two targets the contract runs against: the in-repo fake daemon, and a real disposable daemon.

Both answer `call()`, which sends one contract endpoint's request and checks the answer against the contract (status
listed, body shaped). The `arrange_*` methods set up state the way it happens in real use (the owner on the daemon
host, or an App), outside the contract: on the fake through its test hooks, on the real daemon through its own CLI and
the App API.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import importlib.util
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
from fastapi.testclient import TestClient

from .endpoints import ENDPOINTS, ERROR

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("ui_instance", ROOT / "scripts" / "ui_instance.py")
ui_instance = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ui_instance)

HUB_NAME = "Contract Hub"


def pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


@dataclass
class Reply:
    key: str
    status: int
    body: Any

    @property
    def code(self) -> str:
        error = self.body.get("error") if isinstance(self.body, dict) else None
        return str(error.get("code") or "") if isinstance(error, dict) else ""

    @property
    def detail(self) -> str:
        return self.body.get("detail", "") if isinstance(self.body, dict) else ""


class ContractViolation(AssertionError):
    pass


def check_request(key: str, body: Any, params: dict | None = None) -> list[str]:
    """What is wrong with a request the Hub (or a case) sends to `key`, against the contract."""
    endpoint = ENDPOINTS[key]
    if endpoint.request is None:
        problems = [] if body is None else [f"{key} takes no body, got {body!r}"]
    else:
        problems = endpoint.request.errors(body, "request")
    if params:
        problems += endpoint.params.errors(params, "query") if endpoint.params else [f"{key} takes no query string"]
    return problems


_ROUTES = [(key, re.compile(re.sub(r"\\{[^}]*\\}", "[^/]+", re.escape(key.split(" ", 1)[1])) + "$"))
           for key in ENDPOINTS]


def endpoint_for(method: str, path: str) -> str | None:
    """The contract key a request on the wire is for, or None."""
    return next((key for key, pattern in _ROUTES if key.startswith(method + " ") and pattern.match(path)), None)


def check_sent(method: str, path: str, raw: bytes, query: dict[str, str]) -> list[str]:
    """What is wrong with one request the Hub sent: the route, the JSON body and the query field names."""
    key = endpoint_for(method, path)
    if key is None:
        return [f"the Hub called {method} {path}, which is not a contract endpoint"]
    try:
        body = json.loads(raw) if raw else None
    except ValueError:
        return [f"{key}: the body is not JSON"]
    problems = check_request(key, body)
    params = ENDPOINTS[key].params
    unknown = set(query) - (params.fields() if params else set())
    return problems + ([f"{key}: unexpected query fields {sorted(unknown)}"] if unknown else [])


def check_response(key: str, status: int, body: Any) -> list[str]:
    endpoint = ENDPOINTS[key]
    allowed = set(endpoint.responses) | set(endpoint.common)
    if status not in allowed:
        return [f"{key} answered HTTP {status}, which the contract does not list ({sorted(allowed)}): {body!r:.300}"]
    shape = endpoint.responses.get(status, ERROR)
    if shape is None:
        return []
    return [f"{key} HTTP {status} {problem}" for problem in shape.errors(body)]


class Target:
    name = ""
    token = ""

    def send(self, method: str, url: str, *, json_body: Any = None, params: dict | None = None,
             token: str = "") -> tuple[int, Any]:
        raise NotImplementedError

    def call(self, key: str, path: dict | None = None, body: Any = None, *, params: dict | None = None,
             expect: int | None = None, auth: bool = True, conform: bool = True) -> Reply:
        """Send `key`'s request and check the answer. `conform=False` sends a request outside the contract on
        purpose (a refusal case); the answer is still checked."""
        if key not in ENDPOINTS:
            raise ContractViolation(f"{key} is not a contract endpoint (tests/contract/endpoints.py)")
        problems = check_request(key, body, params) if conform else []
        if problems:
            raise ContractViolation("the case's own request breaks the contract: " + "; ".join(problems))
        method, template = key.split(" ", 1)
        status, answer = self.send(method, template.format(**(path or {})), json_body=body, params=params,
                                   token=self.token if auth else "")
        problems = check_response(key, status, answer)
        if expect is not None and status != expect:
            problems.insert(0, f"{key} answered HTTP {status}, the case expects {expect}: {answer!r:.500}")
        if problems:
            raise ContractViolation(f"[{self.name}] " + "\n".join(problems))
        return Reply(key, status, answer)

    # The Hub's own claim, as claim.py makes it: the same on both targets but for the owner's approval. -------------
    def claim(self) -> None:
        verifier, challenge = pkce()
        started = self.call("POST /api/v1/pair/requests", body={
            "name": HUB_NAME, "kind": "hub", "code_challenge": challenge, "code_challenge_method": "S256"}, expect=201)
        self.arrange_approve_hub_claim(started.body["id"], started.body["match_code"])
        redeemed = self.call("POST /api/v1/pair/requests/{request_id}/token", {"request_id": started.body["id"]},
                             {"code_verifier": verifier}, expect=201)
        assert redeemed.body["app"]["role"] == "hub" and redeemed.body["token"].startswith("ho-")
        self.token = redeemed.body["token"]
        self.key_id = redeemed.body["app"]["id"]

    # Arrangements ----------------------------------------------------------------------------------------------
    def arrange_approve_hub_claim(self, rid: str, match_code: str) -> None:
        raise NotImplementedError

    def arrange_app_key(self, name: str) -> str:
        """An App key the owner created on the host. Returns its id."""
        raise NotImplementedError

    def arrange_app_request(self, name: str, scopes: list[str]) -> tuple[str, str]:
        """A pairing request an App made. Returns its id and the match code the App shows."""
        raise NotImplementedError

    def arrange_app_claims(self, rid: str) -> str:
        """The App claims an armed slot. Returns the match code it shows."""
        raise NotImplementedError

    def module_running(self, name: str) -> bool:
        """Whether an add-on module serves its owner routes here (the fake models every one it answers for)."""
        return True

    def close(self) -> None:
        pass


class FakeTarget(Target):
    name = "fake"

    def __init__(self):
        from ..conftest import FakeDaemon

        self.fake = FakeDaemon()
        self.client = TestClient(self.fake.app, base_url="https://daemon.test")

    def send(self, method, url, *, json_body=None, params=None, token=""):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        response = self.client.request(method, url, json=json_body, params=params, headers=headers)
        return response.status_code, _json(response)

    def arrange_approve_hub_claim(self, rid, match_code):
        assert self.fake.requests[rid]["match_code"] == match_code
        self.fake.approve(rid)

    def arrange_app_key(self, name):
        return self.fake.apps_rows[0]["id"]

    def arrange_app_request(self, name, scopes):
        rid = self.fake.add_pairing(name, scopes, origin="", catalog_app_id="test.example.notes")
        return rid, self.fake.pairing_rows[rid]["match_code"]

    def arrange_app_claims(self, rid):
        return self.fake.claim_slot(rid, self.fake.pairing_rows[rid]["origin"])["match_code"]

    def close(self):
        self.client.close()


class NtfySink:
    """A loopback stand-in for the ntfy server notifications publish to: it accepts and keeps every message."""

    def __init__(self):
        received = self.received = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802 - the http.server hook name
                received.append((self.path, self.rfile.read(int(self.headers.get("content-length") or 0))))
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.end_headers()
                self.wfile.write(b"{}")

            do_PUT = do_POST  # noqa: N815

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class DaemonTarget(Target):
    """A real Agent Harness daemon from a checkout, in fake model mode, on loopback, with throwaway config and data.

    It is started as scripts/ui_instance.py starts the UI journeys' daemon (the same config, minus the Hub process),
    with an allowlisted environment and HOME inside its own directory."""

    name = "daemon"

    def __init__(self, src: Path, root: Path, python: str = sys.executable):
        self.src, self.root, self.python = src, root, python
        self.proc: subprocess.Popen | None = None
        self.ntfy = NtfySink()
        port = ui_instance.free_port()
        self.url = f"http://127.0.0.1:{port}"
        config_dir, _ = ui_instance.write_daemon_config(root, port)
        self.env = ui_instance.child_env(root, HARNESS_CONFIG_DIR=str(config_dir), HARNESS_URL=self.url)
        self._switch_on_modules(config_dir / "harness.yaml")
        self.log = (root / "daemon.log").open("w", encoding="utf-8")
        self.proc = subprocess.Popen(
            [python, "-m", "harness", "--config-dir", str(config_dir)], cwd=src, stdout=self.log,
            stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            env=dict(self.env, **{ui_instance.FAKE_ENV: str(src / "harness" / "fake_fixtures")}))
        self.http = httpx.Client(base_url=self.url, timeout=30, trust_env=False)
        try:
            ui_instance.wait(lambda: self.http.get("/health").status_code == 200, ui_instance.START_SECONDS,
                             "the daemon to start", [("daemon", self.proc)])
        except ui_instance.StartError as error:
            self.close()
            raise AssertionError(f"{error}\n{self.log_tail()}") from None

    def _switch_on_modules(self, config_file: Path) -> None:
        """The add-ons whose owner routes the Hub calls and that run with nothing outside DIR: backups into DIR,
        instruction skills (the hosted reviewer is a closed loopback port, never called), notifications to a loopback
        ntfy sink, and a memory library cloned from a bare repo in DIR (a commit identity in DIR's HOME). Images and
        Remote Control need ComfyUI or Claude Code, so they stay absent and their cases check the absent answer."""
        config = json.loads(config_file.read_text(encoding="utf-8"))
        seed, bare = self.root / "memory-seed", self.root / "memory.git"
        identity = "[user]\n\tname = Contract suite\n\temail = contract@example.test\n"
        (self.root / "home" / ".gitconfig").write_text(identity, encoding="utf-8")
        seed.mkdir()
        (seed / "agent-profile.md").write_text("Synthetic agent profile.\n", encoding="utf-8")
        for argv in (["init", "-q", "--bare", str(bare)], ["-C", str(seed), "init", "-q"],
                     ["-C", str(seed), "add", "."], ["-C", str(seed), "commit", "-qm", "Synthetic profile"],
                     ["-C", str(seed), "push", "-q", str(bare), "HEAD:refs/heads/main"],
                     ["-C", str(bare), "symbolic-ref", "HEAD", "refs/heads/main"]):
            subprocess.run(["git", *argv], env=self.env, check=True, capture_output=True, timeout=60)
        config["modules"].update(backup=True, notifications=True, memory_library=True, skills=True)
        config.update(backup={"enabled": True, "dir": (self.root / "backups").as_posix()},
                      notify={"enabled": True, "server": self.ntfy.url, "topic": "contract"},
                      skills={"enabled": True, "reviewer_base_url": "http://127.0.0.1:9/v1",
                              "reviewer_model": "contract-reviewer"},
                      memory_library={"enabled": True, "writes": True, "repo": bare.as_posix(),
                                      "clone_dir": (self.root / "memory").as_posix(),
                                      "profile_path": "agent-profile.md"})
        config_file.write_text(json.dumps(config, indent=1), encoding="utf-8")

    def log_tail(self) -> str:
        with contextlib.suppress(OSError):
            return (self.root / "daemon.log").read_text(encoding="utf-8", errors="replace")[-4000:]
        return ""

    def send(self, method, url, *, json_body=None, params=None, token=""):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        response = self.http.request(method, url, json=json_body, params=params, headers=headers)
        return response.status_code, _json(response)

    def host_cli(self, *argv: str) -> str:
        result = subprocess.run([self.python, "-m", "harness.cli", "--config", str(ui_instance.cli_config(self.root)),
                                 *argv], cwd=self.src, env=self.env, capture_output=True, text=True, timeout=60)
        assert result.returncode == 0, f"harness {' '.join(argv[:2])}: {(result.stderr or result.stdout)[-800:]}"
        return result.stdout

    def arrange_approve_hub_claim(self, rid, match_code):
        self.host_cli("hub", "approve", rid, "--match", match_code)

    def arrange_app_key(self, name):
        return json.loads(self.host_cli("keys", "create", name, "--kind", "app"))["id"]

    def arrange_app_request(self, name, scopes):
        _, challenge = pkce()
        asked = self.http.post("/api/v1/pair/requests", json={
            "name": name, "scopes": scopes, "catalog_app_id": "test.example.notes", "code_challenge": challenge,
            "code_challenge_method": "S256"})
        assert asked.status_code == 201, asked.text
        return asked.json()["id"], asked.json()["match_code"]

    def arrange_app_claims(self, rid):
        _, challenge = pkce()
        claimed = self.http.post(f"/api/v1/pair/requests/{rid}/claim",
                                 json={"code_challenge": challenge, "code_challenge_method": "S256"})
        assert claimed.status_code == 200, claimed.text
        return claimed.json()["match_code"]

    def module_running(self, name):
        status, inventory = self.send("GET", "/api/admin/v1/hub", token=self.token)
        assert status == 200, inventory
        return any(row["name"] == name and row["state"] == "present" for row in inventory["modules"])

    def close(self):
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=15)
        with contextlib.suppress(Exception):
            self.http.close()
        self.ntfy.close()
        self.log.close()


def _json(response: httpx.Response) -> Any:
    if not response.content:
        return None
    try:
        return response.json()
    except ValueError:
        return response.text


def daemon_source() -> Path | None:
    src = os.environ.get("HUB_E2E_DAEMON_SRC", "")
    return Path(src).resolve() if src else None


def keep_logs(root: Path, into: Path | None) -> None:
    """Copy the daemon log next to the test evidence (CI uploads it), never the config or data."""
    if into and (root / "daemon.log").is_file():
        into.mkdir(parents=True, exist_ok=True)
        shutil.copy(root / "daemon.log", into / f"contract-daemon-{os.getpid()}.log")
