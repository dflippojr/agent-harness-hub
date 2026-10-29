"""What each display-text helper in views.py did to token-shaped, prefix-shaped, exact-secret and control-character
input, and the proof that display_text, which replaced all four (#189), does the same and redacts their union.

The cases were written against the four helpers before they were merged; the expected values are unchanged since.
`ORACLES` are those helpers as they were on main before the merge, frozen here to compare against."""

import re
import unicodedata
from functools import partial

import pytest
from hypothesis import example, given
from hypothesis import strategies as st

from harness_hub import server, views
from harness_hub.guard import KEY_PREFIX_SHAPE, TOKEN_SHAPE
from harness_hub.views import FIELD, GUARD, REDACTED, WORD, display_text

KEY = "ho-" + "s" * 43
CODE = "hp-" + "c0de" * 6
PREFIX = "ho-LW5TqUX"
SECRET = "synthetic-verifier-sentinel"
R = "[withheld: token-shaped]"


def _str(value, limit=200):
    if not isinstance(value, str):
        return ""
    for token in TOKEN_SHAPE.finditer(value):
        if token.start() >= limit:
            break
        if limit < token.end():
            return value[:token.end()]
    return value[:limit]


def _prefix(match):
    return match[0] if TOKEN_SHAPE.match(match[0]) else REDACTED


def _diagnostic_text(value, limit=200):
    return KEY_PREFIX_SHAPE.sub(_prefix, value[:limit]) if isinstance(value, str) else ""


def _app_text(value, limit=200, secrets=()):
    if isinstance(value, str):
        for secret in secrets:
            if secret and secret in value:
                return secret
        if KEY_PREFIX_SHAPE.search(value):
            return REDACTED
    return _str(value, limit)


def entry_text(value, limit=200, secrets=()):
    if isinstance(value, str):
        value = "".join(char for char in value if unicodedata.category(char) not in {"Cc", "Cf"})
    return _app_text(value, limit, secrets)


ORACLES = {"_str": _str, "_diagnostic_text": _diagnostic_text, "_app_text": _app_text, "entry_text": entry_text}
# Each old helper is display_text with these options; every call site in views.py and server.py was moved this way.
MODES = {"_str": {"redact": GUARD}, "_diagnostic_text": {"redact": WORD}, "_app_text": {},
         "entry_text": {"strip_controls": True}}


def _new(helper, value, limit=200, secrets=()):
    return display_text(value, limit, secrets=secrets, **MODES[helper])


HELPERS = {name: partial(_new, name) for name in MODES}

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


@pytest.mark.parametrize(("helper", "value", "limit", "secrets", "expected"), CASES)
def test_the_frozen_oracles_match_the_cases(helper, value, limit, secrets, expected):
    assert ORACLES[helper](value, limit, *([secrets] if secrets else [])) == expected


TOKEN_CHARS = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-"
SECRETS = ("synthetic-verifier-sentinel", "ho-" + "H" * 43, "")
FRAGMENTS = ("ha-", "ho-", "hk-", "hp-", "hrp-", "ahg-", "core", "LW5TqUX", " ", ".", "/", "-", "_", "\n", "\t", "\x07",
             "\u202e", "\u200b", "\u00ad", "é", "x" * 7, "c0de" * 6, "S" * 43, *SECRETS)
fragment = st.sampled_from(FRAGMENTS) | st.text(alphabet=TOKEN_CHARS, max_size=12)
values = st.lists(fragment, max_size=12).map("".join) | st.none() | st.integers()
limits = st.integers(0, 120)
secret_sets = st.lists(st.sampled_from(SECRETS), max_size=3).map(tuple)


@given(values, limits, secret_sets)
@example("x" * 50 + " " + KEY, 64, ())
@example("h\u200bo-LW5TqUX", 200, ())
@example("synthetic-\u200bverifier-sentinel", 10, (SECRET,))
def test_each_mode_is_its_old_helper_exactly(value, limit, secrets):
    """Moving a call site to its mode changes no output, so no view is withheld or shown that was not before."""
    for helper, oracle in ORACLES.items():
        old = oracle(value, limit, secrets) if helper in ("_app_text", "entry_text") else oracle(value, limit)
        new = display_text(value, limit, secrets=secrets if helper in ("_app_text", "entry_text") else (),
                           **MODES[helper])
        assert new == old, helper


def revealed(text, secrets):
    """What reaches the browser: the response guard withholds a whole view that holds a token or a known secret."""
    return None if TOKEN_SHAPE.search(text) or any(secret and secret in text for secret in secrets) else text


@given(values, limits, secret_sets)
@example("Home hp-printer helper", 200, ())
@example("echo " + KEY, 200, ())
@example("x" * 190 + " " + KEY, 200, ())
def test_the_default_redacts_the_union_of_the_old_helpers(value, limit, secrets):
    """The default shows text only where every old helper showed the same text; otherwise it shows the marker, nothing,
    or a value the guard withholds. With strip_controls it does the same for entry_text and for the other three helpers
    given the stripped text."""
    for strip in (False, True):
        shown = revealed(display_text(value, limit, secrets=secrets, strip_controls=strip), secrets)
        if shown in (None, "", REDACTED):
            continue
        assert not KEY_PREFIX_SHAPE.search(shown)
        stripped = "".join(char for char in value if unicodedata.category(char) not in {"Cc", "Cf"}) if (
            strip and isinstance(value, str)) else value
        old = [_str(stripped, limit), _diagnostic_text(stripped, limit), _app_text(stripped, limit, secrets)]
        old += [entry_text(value, limit, secrets)] if strip else []
        assert all(revealed(text, secrets) == shown for text in old), old


def test_an_unknown_mode_fails_loudly_instead_of_redacting_less():
    with pytest.raises(ValueError, match="unknown redaction mode"):
        display_text("ho-LW5TqUX", redact="fields")
    assert display_text("ho-LW5TqUX") == display_text("ho-LW5TqUX", redact=FIELD) == REDACTED


def test_only_display_text_redacts_display_text():
    for module in (views, server):
        with open(module.__file__, encoding="utf-8") as file:
            source = file.read()
        assert not re.search(r"\b(_str|_diagnostic_text|_app_text|entry_text)\b", source), module.__name__
