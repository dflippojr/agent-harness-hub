"""The Overview router (#190, stage 6b): Spend joins Overview and the inventory listing in `routes/overview.py`. With it
gone, no `/api/` route is `create_app`'s own: only wiring stays there."""

from __future__ import annotations

from harness_hub.config import Settings
from harness_hub.routes import overview as overview_routes
from harness_hub.server import create_app


def test_spend_is_the_overview_routers_handler(tmp_path):
    app = create_app(Settings(daemon_url="https://daemon.test", state_dir=tmp_path / "state")).fastapi
    assert [route.endpoint for route in app.routes if getattr(route, "path", "") == "/api/spend"] == [
        overview_routes.get_spend]


def test_no_api_route_is_create_apps_own(tmp_path):
    app = create_app(Settings(daemon_url="https://daemon.test", state_dir=tmp_path / "state")).fastapi
    own = sorted(route.path for route in app.routes if getattr(route, "path", "").startswith("/api/")
                 and route.endpoint.__module__ == "harness_hub.server")
    assert own == []
