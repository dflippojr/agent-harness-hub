"""The response recorder distinguishes random session values from daemon data."""

from unittest.mock import Mock

import httpx
import pytest

from .conftest import HubClient, assert_no_secrets
from .e2e.test_real_daemon import Hub


def e2e_hub(tmp_path):
    hub = Hub.__new__(Hub)
    hub.url = "http://127.0.0.1:8120"
    hub.state_dir = tmp_path
    hub.http = Mock(spec=httpx.Client)
    hub.http.headers = {}
    hub.bodies = []
    return hub


@pytest.fixture(params=["unit", "e2e"])
def recorder(request, tmp_path):
    if request.param == "unit":
        client = HubClient(None, tmp_path)
        return client, client._keep
    hub = e2e_hub(tmp_path)

    def keep(response):
        hub.http.request.return_value = response
        return hub.call("POST", "/api/session")

    return hub, keep


@pytest.fixture(params=["unit", "e2e"])
def session_recorder(request, tmp_path, monkeypatch):
    """The unit recorder keeps the response directly; the e2e one goes through the Hub's real sign-in path."""
    if request.param == "unit":
        client = HubClient(None, tmp_path)
        return client, client._keep
    hub = e2e_hub(tmp_path)
    monkeypatch.setattr("tests.e2e.test_real_daemon.subprocess.run",
                        Mock(return_value=Mock(stdout=hub.url + "/#signin=synthetic-code")))

    def sign_in(response):
        hub.http.request.return_value = response
        hub.sign_in()
        hub.http.request.assert_called_once_with("POST", "/api/session", json={"code": "synthetic-code"})
        assert hub.http.headers["X-Hub-Session"] == response.headers["x-hub-session"]

    return hub, sign_in


def test_session_headers_are_not_scanned(session_recorder):
    client, keep = session_recorder
    keep(httpx.Response(204, headers={"Set-Cookie": "cookie_hp-synthetic-session",
                                      "X-Hub-Session": "AAAA-ho-abcdBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB",
                                      "x-synthetic": "retained"}))
    assert client.bodies == ["", "retained"]
    assert_no_secrets(client.bodies)


def test_secrets_in_bodies_and_other_headers_are_still_caught(recorder):
    client, keep = recorder
    keep(httpx.Response(200, text="ho-synthetic-secret"))
    with pytest.raises(AssertionError):
        assert_no_secrets(client.bodies)
    client.bodies.clear()
    keep(httpx.Response(200, text="safe", headers={"x-synthetic": "synthetic-daemon-key"}))
    with pytest.raises(AssertionError):
        assert_no_secrets(client.bodies, "synthetic-daemon-key")
