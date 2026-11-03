"""The Marketplace router's shared state (#190, stage 3): the consent lock, consent copies and observed browser slots
that the Marketplace, arming and the pairing listings share live on each Hub's `HubContext`, not in one process."""

from __future__ import annotations

from harness_hub.config import Settings
from harness_hub.routes import marketplace as marketplace_routes
from harness_hub.server import create_app


def hub_state(path):
    return create_app(Settings(daemon_url="https://daemon.test", state_dir=path)).fastapi.state.hub


def test_each_hub_keeps_its_own_marketplace_state(tmp_path):
    first, second = hub_state(tmp_path / "first"), hub_state(tmp_path / "second")
    assert first.pairing_consent_lock is not second.pairing_consent_lock
    row = {"id": "slot-1", "kind": "app", "state": "armed", "browser": True, "armed": True}
    marketplace_routes.observe_browser_slots(first, [row], "key-1")
    marketplace_routes.remember_consent(first, {"catalog_app_id": "demo", "scopes": [], "elevated": []}, "key-1", [])
    assert first.browser_slot_states == {("key-1", "slot-1"): ("armed", False)} and first.consent_copies
    assert second.browser_slot_states == {} and second.consent_copies == {}
