"""The daemon's Owner API version, classified against the Hub's minimum: the parser, and what Overview reports."""

from __future__ import annotations

import pytest

from harness_hub.views import owner_api_compatibility, ownership_view


@pytest.mark.parametrize(("version", "expected"), [
    ("1.23", "too_old"), ("1.0", "too_old"), ("0.99", "too_old"),
    ("1.24", "no_tailnet_signin"), ("1.25", "no_tailnet_signin"),
    ("1.26", "supported"), ("1.27", "supported"), ("1.100", "supported"), ("2.0", "supported"),
    ("", "unknown"), ("x", "unknown"), ("1", "unknown"), ("1.2.3", "unknown"),
    (" 1.26", "unknown"), ("1.26 ", "unknown"), ("+1.26", "unknown"), ("1.-2", "unknown"),
])
def test_the_reported_version_is_classified_numerically(version, expected):
    assert owner_api_compatibility(version) == expected


@pytest.mark.parametrize("value", [None, 1.26, 126, True, ["1.26"], {"api_version": "1.26"}])
def test_a_version_that_is_not_a_string_is_unknown(value):
    assert owner_api_compatibility(value) == "unknown"
    assert ownership_view({"api_version": value}, {}, "")["compatibility"] == "unknown"


def test_a_missing_discovery_root_is_unknown():
    view = ownership_view(None, {"claimed": False}, "")
    assert view == {"api_version": "", "compatibility": "unknown", "daemon_has_hub": False,
                    "this_hub": False, "record": None}


@pytest.mark.parametrize(("version", "compatibility"), [
    ("1.23", "too_old"), ("1.24", "no_tailnet_signin"), ("1.26", "supported"), ("", "unknown"),
])
def test_overview_reports_the_class_of_the_daemons_owner_api(claimed, fake_daemon, version, compatibility):
    fake_daemon.owner_api = version
    ownership = claimed.get("/api/overview").json()["ownership"]
    assert ownership["api_version"] == version
    assert ownership["compatibility"] == compatibility
    assert ownership["this_hub"] is True
