"""The Apps/pairing router (#190, stage 4): the Apps administration and pairing-request handlers live in
`routes/apps.py` and reach the Hub's state through the serving app's `HubContext`."""

from __future__ import annotations

from harness_hub.config import Settings
from harness_hub.routes import apps as apps_routes
from harness_hub.server import create_app


def test_the_apps_and_pairing_routes_are_the_routers_handlers(tmp_path):
    app = create_app(Settings(daemon_url="https://daemon.test", state_dir=tmp_path / "state")).fastapi
    handlers = {route.path: route.endpoint for route in app.routes if getattr(route, "path", "") in {
        "/api/apps/{resource:path}", "/api/pairing/{rid}/{action}", "/api/pairing"}}
    assert handlers == {"/api/apps/{resource:path}": apps_routes.apps_action,
                        "/api/pairing/{rid}/{action}": apps_routes.pairing_action,
                        "/api/pairing": apps_routes.pairing_action}
