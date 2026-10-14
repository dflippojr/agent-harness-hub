"""Regressions for the specialist security review: transport, authentication and private state."""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, TimeoutError

import httpx
import pytest

from harness_hub import auth
from harness_hub.auth import Sessions, create_signin_code
from harness_hub.config import Settings, normalize_daemon_url, normalize_public_url
from harness_hub.daemon import DaemonUnreachable, Reply
from harness_hub.server import MAX_SIGNIN_BODY, create_app
from harness_hub.state import StateStore, prepare_dir, read_json, write_private


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.20", "lan.example", ""])  # noqa: S104
def test_remote_listener_requires_https(host):
    with pytest.raises(ValueError, match="https --public-url"):
        Settings(host=host)
    assert Settings(host=host, public_url="https://hub.example").secure_cookies


@pytest.mark.parametrize("host", ["192.168.1.20:8120", "lan.example:8120", "http://lan.example:8120", "https://lan.example:8120"])
def test_remote_allowed_host_requires_https(host):
    with pytest.raises(ValueError):
        Settings(extra_hosts=(host,))
    if not host.startswith("http://"):
        expected = host.removeprefix("https://")
        assert expected in Settings(public_url="https://hub.example", extra_hosts=(host,)).allowed_hosts()
    else:
        with pytest.raises(ValueError):
            Settings(public_url="https://hub.example", extra_hosts=(host,))


@pytest.mark.parametrize("url", ["http://10.0.0.5:8100", "http://daemon.example", "http://[2001:db8::1]:8100"])
def test_remote_daemon_requires_https(url):
    with pytest.raises(ValueError, match="https"):
        normalize_daemon_url(url)
    with pytest.raises(ValueError):
        Settings(daemon_url=url)
    assert normalize_daemon_url(url.replace("http:", "https:")) == url.replace("http:", "https:")


@pytest.mark.parametrize("url", ["http://localhost:8100", "http://127.0.0.1:8100", "http://[::1]:8100"])
def test_loopback_daemon_allows_http(url):
    assert normalize_daemon_url(url) == url


@pytest.mark.parametrize("port", ["abc", "0", "65536", "99999", "-1"])
def test_urls_reject_invalid_ports(port):
    with pytest.raises(ValueError, match="port"):
        normalize_daemon_url(f"http://127.0.0.1:{port}")
    with pytest.raises(ValueError, match="port"):
        normalize_public_url(f"https://hub.example:{port}")
    with pytest.raises(ValueError, match="port"):
        Settings(daemon_url=f"http://127.0.0.1:{port}")
    with pytest.raises(ValueError, match="port"):
        Settings(public_url=f"https://hub.example:{port}")
    with pytest.raises(ValueError, match="port"):
        Settings(public_url="https://hub.example", extra_hosts=(f"hub.example:{port}",))


@pytest.mark.parametrize("port", ["1", "8100", "65535"])
def test_urls_accept_valid_ports(port):
    assert normalize_daemon_url(f"http://127.0.0.1:{port}") == f"http://127.0.0.1:{port}"
    assert Settings(public_url=f"https://hub.example:{port}").public_url == f"https://hub.example:{port}"


@pytest.mark.parametrize("flag", ["--daemon-url", "--public-url", "--allowed-host"])
def test_cli_reports_invalid_port_as_usage_error(tmp_path, flag):
    value = "hub.example:abc" if flag == "--allowed-host" else "https://hub.example:abc"
    extra = [] if flag == "--public-url" else ["--public-url", "https://hub.example"]
    if flag == "--daemon-url":
        value, extra = "http://127.0.0.1:abc", []
    result = subprocess.run(
        [sys.executable, "-m", "harness_hub", flag, value, *extra, "--state-dir", str(tmp_path)],
        capture_output=True, text=True, timeout=60, check=False)
    assert result.returncode == 2
    assert "Traceback" not in result.stderr
    assert "harness_hub: error:" in result.stderr


def test_signin_link_rejects_invalid_port(tmp_path):
    result = subprocess.run(
        [sys.executable, "-m", "harness_hub", "signin-link", "--url", "http://127.0.0.1:99999",
         "--state-dir", str(tmp_path)],
        capture_output=True, text=True, timeout=60, check=False)
    assert result.returncode == 2
    assert "Traceback" not in result.stderr


def test_signin_body_limit_does_not_spend_code(hub):
    code = create_signin_code(hub.state_dir)
    oversized = json.dumps({"code": code, "padding": "x" * MAX_SIGNIN_BODY})
    assert hub.post("/api/session", content=oversized).status_code == 413
    assert hub.post("/api/session", json={"code": code}).status_code == 204


@pytest.mark.parametrize("length", [None, "1"])
def test_signin_stream_limit_without_trustworthy_content_length(tmp_path, length):
    app = create_app(Settings(state_dir=tmp_path))
    read = []

    async def check():
        async def body():
            for i in range(5):
                read.append(i)
                yield b"x" * 600
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://127.0.0.1:8120") as client:
            headers = {"X-Hub-Request": "1"}
            if length is not None:
                headers["Content-Length"] = length
            response = await client.post("/api/session", headers=headers, content=body())
            assert response.status_code == 413
    asyncio.run(check())
    assert read == [0, 1]


def test_signin_failure_history_is_bounded(tmp_path):
    sessions = Sessions(tmp_path)
    for _ in range(100):
        assert sessions.redeem_code("bad") is None
    assert len(sessions._failures) <= auth.FAILURES_PER_MINUTE + 1
    assert sessions.redeem_code(create_signin_code(tmp_path)) is not None


def test_cookie_alone_cannot_use_or_end_a_session(hub):
    response = hub.sign_in()
    binding = response.headers["x-hub-session"]
    del hub.client.headers["X-Hub-Session"]
    assert hub.get("/api/session").json()["signed_in"] is False
    assert hub.get("/api/overview").status_code == 401
    assert hub.delete("/api/session").status_code == 401
    hub.client.headers["X-Hub-Session"] = "wrong-origin-value"
    assert hub.get("/api/overview").status_code == 401
    hub.client.headers["X-Hub-Session"] = binding
    assert hub.get("/api/overview").status_code == 200


def test_session_binding_cannot_be_swapped_between_sessions(hub):
    first = hub.sign_in().headers["x-hub-session"]
    hub.sign_in()
    hub.client.headers["X-Hub-Session"] = first
    assert hub.get("/api/overview").status_code == 401


def test_https_cookie_prefix_and_origin_scheme(make_hub):
    hub = make_hub(public_url="https://hub.example")
    hub.client.base_url = "https://hub.example"
    code = create_signin_code(hub.state_dir)
    assert hub.post("/api/session", json={"code": code}, headers={"Origin": "http://hub.example"}).status_code == 403
    response = hub.post("/api/session", json={"code": code}, headers={"Origin": "https://hub.example"})
    assert response.status_code == 204
    cookie = response.headers["set-cookie"]
    assert cookie.startswith("__Host-hub_session=")
    assert "Secure" in cookie and "Path=/" in cookie and "Domain=" not in cookie
    hub.client.headers["X-Hub-Session"] = response.headers["x-hub-session"]
    assert hub.get("/api/session").json()["signed_in"] is True
    assert hub.delete("/api/session", headers={"Origin": "https://hub.example"}).status_code == 204
    assert hub.get("/api/session").json()["signed_in"] is False


@pytest.mark.parametrize("origin", ["https://127.0.0.1:8120", "null", "http://localhost:8120", "http://127.0.0.1:8120/"])
def test_origin_requires_the_full_same_origin(hub, origin):
    code = create_signin_code(hub.state_dir)
    assert hub.post("/api/session", json={"code": code}, headers={"Origin": origin}).status_code == 403
    assert hub.post("/api/session", json={"code": code}, headers={"Origin": "http://127.0.0.1:8120"}).status_code == 204


@pytest.mark.parametrize("confirmation", [200, 500, "unreachable"])
def test_one_unauthorized_reply_does_not_destroy_key(hub, fake_daemon, monkeypatch, confirmation):
    hub.sign_in()
    fake_daemon.approve(hub.post("/api/claim").json()["request_id"])
    hub.wait_for("claimed")
    daemon = hub.client.app.fastapi.state.claims.daemon
    calls = []

    async def admin(token, path=""):
        calls.append((token, path))
        if len(calls) == 1:
            return Reply(401, {})
        if len(calls) == 2:
            return Reply(200, {})
        if confirmation == "unreachable":
            raise DaemonUnreachable()
        return Reply(confirmation, {})

    monkeypatch.setattr(daemon, "admin", admin)
    overview = hub.get("/api/overview").json()
    assert overview["claim"]["state"] == "claimed" and overview["ownership"] is None
    assert overview["error"]
    assert read_json(hub.state_dir / "hub-state.json", {})["key"]["token"] == fake_daemon.hub_token
    assert len(calls) == 3


def test_cli_cannot_resurrect_a_code_redeemed_in_another_process(tmp_path):
    code = create_signin_code(tmp_path)
    script = """
import sys
from pathlib import Path
from harness_hub import auth
original = auth._live_codes
def paused(path, now):
    codes = original(path, now)
    print("loaded", flush=True)
    sys.stdin.readline()
    return codes
auth._live_codes = paused
auth.create_signin_code(Path(sys.argv[1]))
"""
    proc = subprocess.Popen([sys.executable, "-c", script, str(tmp_path)], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert proc.stdout.readline().strip() == "loaded"
        with ThreadPoolExecutor(max_workers=1) as pool:
            redeemed = pool.submit(Sessions(tmp_path).redeem_code, code)
            try:
                with pytest.raises(TimeoutError):
                    redeemed.result(timeout=0.2)
            finally:
                proc.stdin.write("continue\n")
                proc.stdin.flush()
            assert redeemed.result(timeout=10) is not None
        _, errors = proc.communicate(timeout=10)
        assert proc.returncode == 0, errors
        assert Sessions(tmp_path).redeem_code(code) is None
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.communicate(timeout=10)


def _windows_sid(value):
    """Expand SDDL aliases through Windows, so LA means the exact local Administrator SID."""
    import ctypes
    from ctypes import wintypes

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi.ConvertStringSidToSidW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.LPVOID)]
    advapi.ConvertSidToStringSidW.argtypes = [wintypes.LPVOID, ctypes.POINTER(wintypes.LPWSTR)]
    kernel.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel.LocalFree.restype = wintypes.HLOCAL
    sid = wintypes.LPVOID()
    text = wintypes.LPWSTR()
    try:
        if not advapi.ConvertStringSidToSidW(value, ctypes.byref(sid)):
            raise ctypes.WinError(ctypes.get_last_error())
        if not advapi.ConvertSidToStringSidW(sid, ctypes.byref(text)):
            raise ctypes.WinError(ctypes.get_last_error())
        return text.value
    finally:
        if text:
            kernel.LocalFree(ctypes.cast(text, wintypes.HLOCAL))
        if sid:
            kernel.LocalFree(sid)


@pytest.mark.skipif(os.name != "nt", reason="Windows DACL")
def test_windows_state_replaces_public_explicit_grants(tmp_path):
    path = tmp_path / "shared-state"
    path.mkdir()
    subprocess.run(["icacls", str(path), "/grant", "*S-1-1-0:(OI)(CI)R"], check=True, capture_output=True)
    existing = path / "hub-state.json"
    existing.write_text('{"key": {"token": "dummy"}}', encoding="utf-8")
    assert StateStore(path).get("key")["token"] == "dummy"
    write_private(path / "signin-codes.json", "[]")
    identity = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"],
                              check=True, capture_output=True, text=True, timeout=10).stdout
    owner_sid = re.search(r"S-1-\d+(?:-\d+)+", identity).group()
    for number, target in enumerate((path, existing, path / "signin-codes.json")):
        saved = tmp_path / f"acl-{number}.txt"
        subprocess.run(["icacls", str(target), "/save", str(saved)],
                       check=True, capture_output=True, text=True, timeout=10)
        descriptor = saved.read_text(encoding="utf-16-le").splitlines()[1]
        assert descriptor.startswith("D:P"), descriptor  # protected DACL: no inherited public grants
        grants = re.findall(r"\(([^)]+)\)", descriptor)
        assert len(grants) == 2, descriptor
        principals = set()
        for grant in grants:
            kind, _, access, _, _, principal = grant.split(";")
            assert kind == "A" and access == "FA", descriptor
            principals.add(_windows_sid(principal))
        assert principals == {owner_sid, "S-1-5-18"}, descriptor


def test_windows_acl_failure_prevents_private_state_use(tmp_path, monkeypatch):
    from harness_hub import state, windows_acl

    def denied(path):
        raise PermissionError("cannot protect state")

    monkeypatch.setattr(windows_acl, "protect", denied)
    # Patch only this module's platform lookup, without changing pathlib's OS selection.
    class Windows:
        name = "nt"
    monkeypatch.setattr(state, "os", Windows)
    with pytest.raises(PermissionError, match="cannot protect state"):
        prepare_dir(tmp_path)
