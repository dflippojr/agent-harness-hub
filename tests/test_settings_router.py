"""The manage router (#190, stage 5a): the daemon-administration handlers live in `routes/settings.py`, and the claim
routes join the session router in `routes/session.py`, each reaching the Hub's state through its `HubContext`."""

from __future__ import annotations

from harness_hub.config import Settings
from harness_hub.routes import session as session_routes
from harness_hub.routes import settings as settings_routes
from harness_hub.server import create_app


def test_the_manage_and_claim_routes_are_the_routers_handlers(tmp_path):
    app = create_app(Settings(daemon_url="https://daemon.test", state_dir=tmp_path / "state")).fastapi
    handlers = {(route.path, method): route.endpoint for route in app.routes
                if getattr(route, "path", "").startswith(("/api/manage/", "/api/claim")) for method in route.methods}
    manage = {handler for (path, _), handler in handlers.items() if path.startswith("/api/manage/")}
    assert manage == {settings_routes.administration_catalog, settings_routes.administration_action}
    assert {method: handlers[("/api/claim", method)] for method in ("GET", "POST", "DELETE")} == {
        "GET": session_routes.get_claim, "POST": session_routes.start_claim, "DELETE": session_routes.forget_claim}
