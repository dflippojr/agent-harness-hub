"""Ordinary words shaped like `ha-core` or `hp-laserjet` never blank a page; real tokens still never leave (#84)."""
import pytest

from harness_hub.views import control_view, module_view, preflight_view, registry_view, settings_error

from .conftest import assert_no_secrets
from .test_settings import write

WORDS = "I run Home Assistant (ha-core) and print on the hp-laserjet in the office."
FULL_TOKENS = ("ha-" + "Synthetic_Token-" * 3, "ho-" + "s" * 43, "hk-" + "S1" * 22, "hp-" + "c0de" * 6,
               "hrp-" + "C0DE" * 6)


def test_memory_profile_with_hyphenated_words_loads_and_saves(claimed, fake_daemon):
    fake_daemon.profile = WORDS
    shown = claimed.get("/api/settings/memory")
    assert shown.status_code == 200 and shown.json()["profile"] == WORDS
    saved = write(claimed, "PUT", "memory", {"content": "Also ha-addons.", "summary": "Synthetic"})
    assert saved.status_code == 200 and saved.json()["profile"] == "Also ha-addons."


def test_backend_model_with_a_prefix_word_saves_and_stays_readable(claimed, fake_daemon):
    saved = write(claimed, "PUT", "backend", {"name": "sample", "model": "hp-instruct-7b", "effort": "medium"})
    assert saved.status_code == 200 and saved.json()["model"] == "hp-instruct-7b"
    backends = claimed.get("/api/settings/backends")
    assert backends.status_code == 200 and backends.json()[0]["model"] == "hp-instruct-7b"
    assert "provider-secret-sentinel" not in backends.text and fake_daemon.hub_token[:12] not in backends.text


def test_setting_value_with_a_prefix_word_loads(claimed, fake_daemon):
    fake_daemon.values["labels.names"] = ["ha-core"]
    for resource in ("config", "schema"):
        assert claimed.get("/api/settings/" + resource).status_code == 200
    config = claimed.get("/api/settings/config").json()
    assert next(row for row in config["settings"] if row["key"] == "labels.names")["effective"] == ["ha-core"]


@pytest.mark.parametrize("token", FULL_TOKENS)
def test_a_full_token_in_owner_text_is_still_withheld(claimed, fake_daemon, token):
    fake_daemon.profile = "pasted " + token
    assert claimed.get("/api/settings/memory").status_code == 500
    fake_daemon.values["labels.names"] = [token]
    assert claimed.get("/api/settings/config").status_code == 500
    assert write(claimed, "PUT", "backend", {"name": "sample", "model": token, "effort": "medium"}).status_code == 500
    assert claimed.get("/api/settings/backends").status_code == 500
    assert all(token not in body for body in claimed.bodies)


def test_the_hub_key_in_owner_text_is_still_withheld(claimed, fake_daemon):
    fake_daemon.profile = WORDS + " " + fake_daemon.hub_token
    assert claimed.get("/api/settings/memory").status_code == 500
    assert_no_secrets(claimed.bodies, fake_daemon.hub_token)


DISPLAY_PREFIX = "ho-LW5TqUX"
REDACTED = "[withheld: token-shaped]"


def test_daemon_diagnostics_redact_a_display_prefix_in_place():
    error = settings_error({"detail": f"key {DISPLAY_PREFIX} is revoked", "error": {
        "code": "bad", "keys": {"queue.limit": {"code": "bad", "message": f"not {DISPLAY_PREFIX}"}},
        "details": {"manual": f"revoke {DISPLAY_PREFIX} on the host"}}}, 409)
    assert error["detail"] == f"key {REDACTED} is revoked"
    assert error["error"]["keys"]["queue.limit"]["message"] == f"not {REDACTED}"
    assert error["error"]["details"]["manual"] == f"revoke {REDACTED} on the host"
    registry = registry_view({"warning": DISPLAY_PREFIX, "recovery": {"reason": DISPLAY_PREFIX,
                                                                       "warning": "hp-laserjet offline"}})
    recovery = registry["recovery"]
    assert registry["warning"] == REDACTED and recovery is not None
    assert recovery["reason"] == REDACTED and recovery["warning"] == f"{REDACTED} offline"
    assert preflight_view({"ok": False, "problems": [f"key {DISPLAY_PREFIX}", "fine"]})["problems"] == [
        f"key {REDACTED}", "fine"]
    smart = control_view("smart", {"secret_ref": f"/tokens/{DISPLAY_PREFIX}.txt"})
    assert smart["secret_ref"] == f"/tokens/{REDACTED}.txt"
    memory = control_view("memory", {"profile": WORDS, "refresh_error": f"pull failed for {DISPLAY_PREFIX}"})
    assert memory["profile"] == WORDS and memory["refresh_error"] == f"pull failed for {REDACTED}"
    modules = module_view({"modules": [{"name": "hub", "state": "present", "status": {"last": DISPLAY_PREFIX}}]})
    assert modules[0]["status"] == {"last": REDACTED}


@pytest.mark.parametrize("token", FULL_TOKENS)
def test_daemon_diagnostics_leave_a_full_token_for_the_guard(token):
    # Masking it would hide a daemon leak; leaving it makes the guard withhold the response and log it.
    assert settings_error({"detail": "echo " + token}, 400)["detail"] == "echo " + token


@pytest.mark.parametrize("token", FULL_TOKENS)
def test_a_token_cut_at_the_field_limit_is_redacted(token):
    problems = preflight_view({"problems": ["x" * 50 + " " + token]})["problems"]
    assert problems == ["x" * 50 + " " + REDACTED]
