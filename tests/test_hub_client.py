"""The response recorder distinguishes random session values from daemon data."""

import httpx
import pytest

from .conftest import HubClient, assert_no_secrets


@pytest.mark.parametrize("header", ["set-cookie", "x-hub-session", "X-Hub-Session"])
def test_random_session_headers_are_excluded_from_token_shape_scan(tmp_path, header):
    client = HubClient(None, tmp_path)
    response = httpx.Response(204, headers={header: "binding_ho-synthetic-session", "x-synthetic": "retained"})
    assert client._keep(response) is response
    assert client.bodies == ["", "retained"]
    assert_no_secrets(client.bodies)


@pytest.mark.parametrize("location", ["body", "header"])
def test_token_shaped_body_and_other_headers_are_still_detected(tmp_path, location):
    client = HubClient(None, tmp_path)
    token = "ho-synthetic-secret"
    response = httpx.Response(200, text=token if location == "body" else "safe",
                              headers={"x-synthetic": token} if location == "header" else {})
    client._keep(response)
    with pytest.raises(AssertionError):
        assert_no_secrets(client.bodies)
