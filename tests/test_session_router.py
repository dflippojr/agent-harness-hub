"""The `/api/session` router (#190, stage 2): its handlers live in `routes/session.py` and reach the Hub's state
through the serving app's `HubContext`, so two Hubs in one process never share a session."""

from __future__ import annotations

from harness_hub.config import Settings
from harness_hub.routes import session as session_routes
from harness_hub.server import create_app


def test_the_session_routes_are_the_routers_handlers(tmp_path):
    app = create_app(Settings(daemon_url="https://daemon.test", state_dir=tmp_path / "state")).fastapi
    handlers = {method: route.endpoint for route in app.routes if getattr(route, "path", "") == "/api/session"
                for method in route.methods}
    assert handlers == {"GET": session_routes.get_session, "POST": session_routes.sign_in,
                        "DELETE": session_routes.sign_out}


def test_each_hub_answers_from_its_own_state(make_hub, tmp_path):
    first, second = make_hub(), make_hub(state_dir=tmp_path / "other")
    first.sign_in()
    cookie = first.client.cookies.get("hub_session")
    binding = first.client.headers["X-Hub-Session"]
    assert cookie and first.get("/api/session").json()["signed_in"] is True

    second.client.cookies.set("hub_session", cookie)
    second.client.headers["X-Hub-Session"] = binding
    assert second.get("/api/session").json()["signed_in"] is False
    assert second.get("/api/overview").status_code == 401

    assert second.delete("/api/session").status_code == 401  # not signed in here: ends nothing in the first Hub
    assert first.get("/api/session").json()["signed_in"] is True
    assert first.delete("/api/session").status_code == 204
    assert first.get("/api/session").json()["signed_in"] is False
