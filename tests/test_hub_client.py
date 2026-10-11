"""The response recorder distinguishes random session values from daemon data."""

from unittest.mock import Mock

import httpx
import pytest

from .conftest import HubClient, assert_no_secrets
from .e2e.test_real_daemon import Hub


@pytest.fixture(params=["unit", "e2e"])
def recorder(request, tmp_path):
    if request.param == "unit":
        client = HubClient(None, tmp_path)
        return client, client._keep
    hub = Hub.__new__(Hub)
    hub.http = Mock(spec=httpx.Client)
    hub.bodies = []

    def keep(response):
        hub.http.request.return_value = response
        return hub.call("POST", "/api/session")

    return hub, keep


@pytest.mark.parametrize("header", ["set-cookie", "x-hub-session", "X-Hub-Session"])
def test_random_session_headers_are_excluded_from_token_shape_scan(recorder, header):
    client, keep = recorder
    response = httpx.Response(204, headers={header: "binding_ho-synthetic-session", "x-synthetic": "retained"})
    assert keep(response) is response
    assert client.bodies == ["", "retained"]
    assert_no_secrets(client.bodies)


def test_real_daemon_signin_records_data_without_scanning_random_binding(tmp_path, monkeypatch):
    hub = Hub.__new__(Hub)
    hub.url = "http://127.0.0.1:8120"
    hub.state_dir = tmp_path
    hub.http = Mock(spec=httpx.Client)
    hub.http.headers = {}
    hub.bodies = []
    binding = "AAAA-ho-abcdBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB"
    hub.http.request.return_value = httpx.Response(204, headers={"X-Hub-Session": binding,
                                                               "Set-Cookie": "cookie_hp-synthetic-session",
                                                               "x-synthetic": "retained"})
    cli = Mock(return_value=Mock(stdout=hub.url + "/#signin=synthetic-code"))
    monkeypatch.setattr("tests.e2e.test_real_daemon.subprocess.run", cli)
    hub.sign_in()
    assert hub.http.headers["X-Hub-Session"] == binding
    hub.http.request.assert_called_once_with("POST", "/api/session", json={"code": "synthetic-code"})
    assert hub.bodies == ["", "retained"]
    assert_no_secrets(hub.bodies)


@pytest.mark.parametrize("location", ["body", "header"])
@pytest.mark.parametrize("token", ["ha-synthetic-secret", "ho-synthetic-secret", "hp-synthetic-secret"])
def test_token_shaped_body_and_other_headers_are_still_detected(recorder, location, token):
    client, keep = recorder
    response = httpx.Response(200, text=token if location == "body" else "safe",
                              headers={"x-synthetic": token} if location == "header" else {})
    keep(response)
    with pytest.raises(AssertionError):
        assert_no_secrets(client.bodies)


@pytest.mark.parametrize("location", ["body", "header"])
@pytest.mark.parametrize("secret", ["synthetic-daemon-key", "synthetic-claim-verifier"])
def test_exact_daemon_keys_and_verifiers_are_still_detected(recorder, location, secret):
    client, keep = recorder
    response = httpx.Response(200, text=secret if location == "body" else "safe",
                              headers={"x-synthetic": secret} if location == "header" else {})
    keep(response)
    with pytest.raises(AssertionError):
        assert_no_secrets(client.bodies, secret)
