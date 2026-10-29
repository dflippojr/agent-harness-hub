"""What each display-text helper in views.py does to token-shaped, prefix-shaped, exact-secret and control-character
input (#189). Written against the four helpers before they were merged; the expected values are the contract."""

import pytest

from harness_hub import views

KEY = "ho-" + "s" * 43
CODE = "hp-" + "c0de" * 6
PREFIX = "ho-LW5TqUX"
SECRET = "synthetic-verifier-sentinel"
R = "[withheld: token-shaped]"

HELPERS = {"_str": views._str, "_diagnostic_text": views._diagnostic_text, "_app_text": views._app_text,
           "entry_text": views.entry_text}

CASES = [
    # _str: nothing is redacted; a token cut by the limit is kept whole so the response guard withholds the view.
    ("_str", None, 200, (), ""),
    ("_str", 7, 200, (), ""),
    ("_str", "plain text", 200, (), "plain text"),
    ("_str", "x" * 300, 200, (), "x" * 200),
    ("_str", f"key {PREFIX} revoked", 200, (), f"key {PREFIX} revoked"),
    ("_str", "hp-instruct-7b", 200, (), "hp-instruct-7b"),
    ("_str", "echo " + KEY, 200, (), "echo " + KEY),
    ("_str", "x" * 190 + " " + KEY, 200, (), "x" * 190 + " " + KEY),
    ("_str", "x" * 190 + " " + CODE, 200, (), "x" * 190 + " " + CODE),
    ("_str", "x" * 250 + " " + KEY, 200, (), "x" * 200),
    ("_str", "name " + SECRET, 200, (), "name " + SECRET),
    ("_str", "a‮b\x07c", 200, (), "a‮b\x07c"),
    # _diagnostic_text: each prefix-shaped word is redacted in place; a whole token is left for the guard.
    ("_diagnostic_text", None, 200, (), ""),
    ("_diagnostic_text", f"key {PREFIX} revoked", 200, (), f"key {R} revoked"),
    ("_diagnostic_text", "hp-laserjet offline", 200, (), f"{R} offline"),
    ("_diagnostic_text", f"{PREFIX} and {PREFIX}", 200, (), f"{R} and {R}"),
    ("_diagnostic_text", "echo " + KEY, 200, (), "echo " + KEY),
    ("_diagnostic_text", "x" * 50 + " " + KEY, 64, (), "x" * 50 + " " + R),
    ("_diagnostic_text", "x" * 50 + " " + CODE, 64, (), "x" * 50 + " " + R),
    ("_diagnostic_text", "x" * 300, 200, (), "x" * 200),
    ("_diagnostic_text", "x" * 300 + " " + PREFIX, 200, (), "x" * 200),
    ("_diagnostic_text", "pre" + PREFIX, 200, (), "pre" + PREFIX),
    ("_diagnostic_text", "ho-abc", 200, (), "ho-abc"),
    ("_diagnostic_text", "a‮b\x07c", 200, (), "a‮b\x07c"),
    # _app_text: a prefix-shaped word anywhere, even past the limit and even a whole token, replaces the whole field;
    # an exact known secret is returned as is, so the guard withholds the view.
    ("_app_text", None, 200, (), ""),
    ("_app_text", None, 200, (SECRET,), ""),
    ("_app_text", "Web client", 200, (), "Web client"),
    ("_app_text", "Home hp-printer helper", 200, (), R),
    ("_app_text", "com.ha-tools.app", 200, (), R),
    ("_app_text", "https://hp-x.example", 200, (), "https://hp-x.example"),
    ("_app_text", "echo " + KEY, 200, (), R),
    ("_app_text", "x" * 300, 200, (), "x" * 200),
    ("_app_text", "x" * 300 + " " + PREFIX, 200, (), R),
    ("_app_text", "x" * 220 + SECRET, 200, (SECRET,), SECRET),
    ("_app_text", f"{PREFIX} {SECRET}", 200, (SECRET,), SECRET),
    ("_app_text", "name " + SECRET, 200, ("", "other"), "name " + SECRET),
    ("_app_text", "a‮b\x07c", 200, (), "a‮b\x07c"),
    # entry_text: control and format characters go first, then everything _app_text does.
    ("entry_text", None, 200, (), ""),
    ("entry_text", "a‮b\x07c​d", 200, (), "abcd"),
    ("entry_text", "two\nlines\t", 200, (), "twolines"),
    ("entry_text", "h​o-LW5TqUX", 200, (), R),
    ("entry_text", KEY, 80, (), R),
    ("entry_text", "Notes", 200, (), "Notes"),
    ("entry_text", "x" * 300, 280, (), "x" * 280),
    ("entry_text", "synthetic-​verifier-sentinel", 200, (SECRET,), SECRET),
]


@pytest.mark.parametrize(("helper", "value", "limit", "secrets", "expected"), CASES)
def test_display_text_helper_contract(helper, value, limit, secrets, expected):
    if secrets:
        assert HELPERS[helper](value, limit, secrets) == expected
    else:
        assert HELPERS[helper](value, limit) == expected
