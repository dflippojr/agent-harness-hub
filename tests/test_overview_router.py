"""The Overview router (#190, stage 6a): Overview and the inventory listing live in `routes/overview.py`, reaching the
Hub's state through its `HubContext`."""

from __future__ import annotations

from harness_hub.config import Settings
from harness_hub.routes import overview as overview_routes
from harness_hub.server import create_app


def test_overview_and_the_inventory_listing_are_the_routers_handlers(tmp_path):
    app = create_app(Settings(daemon_url="https://daemon.test", state_dir=tmp_path / "state")).fastapi
    handlers = {route.path: route.endpoint for route in app.routes
                if getattr(route, "path", "") in ("/api/overview", "/api/apps", "/api/modules")}
    assert handlers == {"/api/overview": overview_routes.get_overview, "/api/apps": overview_routes.get_modules,
                        "/api/modules": overview_routes.get_modules}
