"""Path-segment ids follow one rule (ID_SEGMENT in views.py): 1 to 80 letters, digits, '_' or '-'.

Each route that takes an id as a path segment or a body field accepts an id at the limit and a leading '-', and refuses
one past the limit or with a dot before any daemon call.
"""
import pytest

from .conftest import HUB_HOST

ORIGIN = {"Origin": "http://" + HUB_HOST, "X-Hub-Request": "1"}
ACCEPTED = ["a" * 80, "-" + "a" * 79]
REFUSED = ["a" * 81, "k.x"]


def request(claimed, method, path, body):
    return claimed._keep(claimed.client.request(method, path, json=body, headers=ORIGIN))


@pytest.mark.parametrize("app_id", ACCEPTED)
def test_accepted_app_ids_reach_the_daemon_and_come_back_unchanged(claimed, fake_daemon, app_id):
    fake_daemon.apps_rows.append({**fake_daemon.apps_rows[0], "id": app_id})
    reply = request(claimed, "PUT", f"/api/apps/{app_id}/retention", {"retention_days": 14, "confirm": True})
    assert (reply.status_code, reply.json()) == (200, {"id": app_id, "retention_days": 14})


@pytest.mark.parametrize("app_id", REFUSED)
def test_refused_app_ids_never_reach_the_daemon(claimed, fake_daemon, app_id):
    calls = list(fake_daemon.apps_calls)
    reply = request(claimed, "PUT", f"/api/apps/{app_id}/retention", {"retention_days": 14, "confirm": True})
    assert reply.status_code == 404 and reply.json()["detail"].startswith("Unknown Apps operation")
    assert fake_daemon.apps_calls == calls


@pytest.mark.parametrize("rid", ACCEPTED)
def test_accepted_pairing_ids_reach_the_daemon(claimed, fake_daemon, rid):
    calls = len(fake_daemon.pairing_calls)
    reply = request(claimed, "POST", f"/api/pairing/{rid}/deny", {"confirm": True})
    # The fake holds no request with this id, so the daemon's own not-found answer comes back unchanged.
    assert (reply.status_code, reply.json()["detail"]) == (404, "no pairing request has that id")
    assert len(fake_daemon.pairing_calls) == calls + 1


@pytest.mark.parametrize("rid", REFUSED)
def test_refused_pairing_ids_never_reach_the_daemon(claimed, fake_daemon, rid):
    calls = list(fake_daemon.pairing_calls)
    reply = request(claimed, "POST", f"/api/pairing/{rid}/deny", {"confirm": True})
    assert (reply.status_code, reply.json()["detail"]) == (404, "Unknown pairing operation")
    assert fake_daemon.pairing_calls == calls


@pytest.mark.parametrize("name", ACCEPTED)
def test_accepted_backend_names_reach_the_daemon(claimed, fake_daemon, name):
    reply = request(claimed, "PUT", "/api/settings/backend", {"name": name, "model": "sample"})
    # The fake answers only for the backend named sample, so the daemon's own refusal comes back unchanged.
    assert reply.status_code == 404
    assert fake_daemon.settings_calls[-1][:2] == ("PUT", f"backends/{name}")


@pytest.mark.parametrize("name", REFUSED)
def test_refused_backend_names_never_reach_the_daemon(claimed, fake_daemon, name):
    calls = list(fake_daemon.settings_calls)
    reply = request(claimed, "PUT", "/api/settings/backend", {"name": name, "model": "sample"})
    assert (reply.status_code, reply.json()["detail"]) == (400, "invalid backend name")
    assert fake_daemon.settings_calls == calls
