"""The answers the web framework shapes rather than a handler, pinned byte for byte (#254): `read_json`'s refusals, a
handler's returned dict, an unknown path, a wrong method and HEAD on a GET-only route. Moving the Hub from FastAPI to
Starlette must not change any of them."""

from __future__ import annotations

import pytest

from harness_hub import __version__

from .conftest import HUB_HOST

WRITE = {"Origin": "http://" + HUB_HOST, "X-Hub-Request": "1"}
JSON = "application/json"
NOT_FOUND = b'{"detail":"Not Found"}'
NOT_ALLOWED = b'{"detail":"Method Not Allowed"}'


def exact(reply):
    return reply.status_code, reply.headers["content-type"], reply.content


def test_read_json_refuses_a_write_with_no_origin(claimed):
    reply = claimed.client.put("/api/apps/k-minute/limits", headers={"X-Hub-Request": "1"}, content=b"{}")
    assert exact(reply) == (403, JSON, b'{"detail":"Apps writes require a same-origin Origin"}')


def test_read_json_refuses_a_streamed_body_over_its_limit(claimed):
    def chunks():  # no Content-Length: the limit is counted while the body streams in
        for _ in range(5):
            yield b" " * 300

    reply = claimed.client.put("/api/apps/k-minute/limits", headers=WRITE, content=chunks())
    assert "content-length" not in reply.request.headers
    assert exact(reply) == (413, JSON, b'{"detail":"Apps body is too large"}')


def test_read_json_refuses_a_body_that_is_not_json(claimed):
    reply = claimed.client.put("/api/apps/k-minute/limits", headers=WRITE, content=b"{")
    assert exact(reply) == (400, JSON, b'{"detail":"Apps body must be JSON"}')


def test_a_returned_dict_is_answered_as_json(claimed):
    assert exact(claimed.client.get("/healthz")) == (
        200, JSON, b'{"ok":true,"version":"' + __version__.encode() + b'"}')
    reply = claimed.client.get("/api/manage/catalog")
    assert (reply.status_code, reply.headers["content-type"]) == (200, JSON)
    assert reply.content.startswith(b'{"operations":[{"id":"accounts","group":"accounts",')


@pytest.mark.parametrize("path", ["/api/nope", "/api/session/extra", "/nope", "/static/nope.mjs"])
def test_an_unknown_path_is_a_json_404(claimed, path):
    assert exact(claimed.client.get(path)) == (404, JSON, NOT_FOUND)


@pytest.mark.parametrize(("method", "path", "allow"), [
    ("POST", "/api/overview", "GET"),
    ("PUT", "/api/session", "GET"),
    ("DELETE", "/api/pairing/pr-synthetic/approve", "POST"),
    ("POST", "/healthz", "GET"),
])
def test_a_wrong_method_is_a_json_405(claimed, method, path, allow):
    reply = claimed.client.request(method, path, headers=WRITE)
    assert exact(reply) == (405, JSON, NOT_ALLOWED)
    assert reply.headers["content-length"] == str(len(NOT_ALLOWED))
    # Allow names the methods of the first route whose path matched: /api/session has one route per method.
    assert reply.headers["allow"] == allow


@pytest.mark.parametrize("path", ["/healthz", "/", "/api/session", "/api/overview", "/api/modules", "/api/apps",
                                  "/api/spend", "/api/marketplace", "/api/pairing", "/api/manage/catalog",
                                  "/api/settings/health", "/api/claim"])
def test_head_on_a_get_only_route_is_405(claimed, path):
    reply = claimed.client.head(path)
    assert (reply.status_code, reply.headers["content-type"], reply.headers["allow"]) == (405, JSON, "GET")
    assert reply.headers["content-length"] == str(len(NOT_ALLOWED))
