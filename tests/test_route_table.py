"""The Hub app's route table (path, methods, handler, match order), middleware and ASGI wrappers, snapshotted before
`create_app` is split into routers (#190): a change here changes what the Hub serves. Other tests prove responses."""

from __future__ import annotations

from starlette.routing import Mount

from harness_hub.config import Settings
from harness_hub.server import create_app

ROUTES = [
    ("/healthz", ["GET"], "healthz"),
    ("/api/session", ["GET"], "get_session"),
    ("/api/session", ["POST"], "sign_in"),
    ("/api/session", ["DELETE"], "sign_out"),
    ("/api/overview", ["GET"], "get_overview"),
    ("/api/modules", ["GET"], "get_modules"),
    ("/api/apps", ["GET"], "get_modules"),
    ("/api/spend", ["GET"], "get_spend"),
    ("/api/marketplace", ["GET"], "get_marketplace"),
    ("/api/marketplace/arm", ["POST"], "arm_slot"),
    ("/api/apps/{resource:path}", ["DELETE", "GET", "POST", "PUT"], "apps_action"),
    ("/api/pairing/{rid}/{action}", ["POST"], "pairing_action"),
    ("/api/pairing", ["GET"], "pairing_action"),
    ("/api/manage/catalog", ["GET"], "administration_catalog"),
    ("/api/manage/{operation}", ["DELETE", "GET", "PATCH", "POST", "PUT"], "administration_action"),
    ("/api/settings/health", ["GET"], "settings_health"),
    ("/api/settings/{resource:path}", ["GET", "PATCH", "POST", "PUT"], "settings_action"),
    ("/api/claim", ["GET"], "get_claim"),
    ("/api/claim", ["POST"], "start_claim"),
    ("/api/claim", ["DELETE"], "forget_claim"),
    ("/", ["GET"], "index"),
    ("mount", "/static", "static"),
]


def served(tmp_path):
    return create_app(Settings(daemon_url="https://daemon.test", state_dir=tmp_path / "state"))


def test_route_table_is_unchanged(tmp_path):
    app = served(tmp_path).fastapi
    table = [("mount", route.path, route.name) if isinstance(route, Mount)
             else (route.path, sorted(route.methods or ()), route.name) for route in app.routes]
    assert table == ROUTES


def test_middleware_is_unchanged(tmp_path):
    app = served(tmp_path).fastapi
    assert [(entry.cls.__name__, entry.kwargs["dispatch"].__name__) for entry in app.user_middleware] == [
        ("BaseHTTPMiddleware", "require_owner")]


def test_wrapper_chain_is_unchanged(tmp_path):
    outer = served(tmp_path)
    chain, layer = [], outer
    while layer is not outer.fastapi:
        chain.append(type(layer).__name__)
        layer = layer.app
    assert chain == ["HostCheck", "TailnetIdentity", "ResponseGuard"]
