"""The four helpers display_text replaced (#189), frozen as on main, and cases written against them before the merge."""

import json
import unicodedata

import pytest
from hypothesis import example, given
from hypothesis import strategies as st

from harness_hub.guard import KEY_PREFIX_SHAPE, TOKEN_SHAPE, leaks
from harness_hub.views import FIELD, GUARD, REDACTED, WORD, display_text, entry_digest, entry_view

from .test_marketplace import arm, assert_no_secrets, entries  # noqa: F401 (a fixture)

KEY = "ho-" + "s" * 43
CODE = "hp-" + "c0de" * 6
PREFIX = "ho-LW5TqUX"
SECRET = "synthetic-verifier-sentinel"
RLO, ZWSP, SHY = chr(0x202E), chr(0x200B), chr(0xAD)  # right-to-left override, zero-width space, soft hyphen


def _str(value, limit=200, secrets=()):  # `secrets` is unused here and in _diagnostic_text: they had none
    if not isinstance(value, str):
        return ""
    for token in TOKEN_SHAPE.finditer(value):
        if token.start() >= limit:
            break
        if limit < token.end():
            return value[:token.end()]
    return value[:limit]


def _diagnostic_text(value, limit=200, secrets=()):
    return KEY_PREFIX_SHAPE.sub(lambda m: m[0] if TOKEN_SHAPE.match(m[0]) else REDACTED,
                                value[:limit]) if isinstance(value, str) else ""


def _app_text(value, limit=200, secrets=()):
    if isinstance(value, str):
        for secret in secrets:
            if secret and secret in value:
                return secret
        if KEY_PREFIX_SHAPE.search(value):
            return REDACTED
    return _str(value, limit)


def _strip(value):
    return "".join(char for char in value if unicodedata.category(char) not in {"Cc", "Cf"})


def entry_text(value, limit=200, secrets=()):
    return _app_text(_strip(value) if isinstance(value, str) else value, limit, secrets)


# Each old helper is display_text with these options; every call site in views.py and server.py moved this way.
MODES = {_str: {"redact": GUARD}, _diagnostic_text: {"redact": WORD}, _app_text: {},
         entry_text: {"strip_controls": True}}

CASES = [
    # _str: nothing is redacted; a token cut by the limit is kept whole so the response guard withholds the view.
    (_str, None, 200, (), ""),
    (_str, 7, 200, (), ""),
    (_str, "plain text", 200, (), "plain text"),
    (_str, "x" * 300, 200, (), "x" * 200),
    (_str, f"key {PREFIX} revoked", 200, (), f"key {PREFIX} revoked"),
    (_str, "hp-instruct-7b", 200, (), "hp-instruct-7b"),
    (_str, "echo " + KEY, 200, (), "echo " + KEY),
    (_str, "x" * 190 + " " + KEY, 200, (), "x" * 190 + " " + KEY),
    (_str, "x" * 190 + " " + CODE, 200, (), "x" * 190 + " " + CODE),
    (_str, "x" * 250 + " " + KEY, 200, (), "x" * 200),
    (_str, "name " + SECRET, 200, (), "name " + SECRET),
    (_str, f"a{RLO}b\x07c", 200, (), f"a{RLO}b\x07c"),
    # _diagnostic_text: each prefix-shaped word is redacted in place; a whole token is left for the guard.
    (_diagnostic_text, None, 200, (), ""),
    (_diagnostic_text, f"key {PREFIX} revoked", 200, (), f"key {REDACTED} revoked"),
    (_diagnostic_text, "hp-laserjet offline", 200, (), f"{REDACTED} offline"),
    (_diagnostic_text, f"{PREFIX} and {PREFIX}", 200, (), f"{REDACTED} and {REDACTED}"),
    (_diagnostic_text, "echo " + KEY, 200, (), "echo " + KEY),
    (_diagnostic_text, "x" * 50 + " " + KEY, 64, (), "x" * 50 + " " + REDACTED),
    (_diagnostic_text, "x" * 50 + " " + CODE, 64, (), "x" * 50 + " " + REDACTED),
    (_diagnostic_text, "x" * 300, 200, (), "x" * 200),
    (_diagnostic_text, "x" * 300 + " " + PREFIX, 200, (), "x" * 200),
    (_diagnostic_text, "pre" + PREFIX, 200, (), "pre" + PREFIX),
    (_diagnostic_text, "ho-abc", 200, (), "ho-abc"),
    (_diagnostic_text, f"a{RLO}b\x07c", 200, (), f"a{RLO}b\x07c"),
    # _app_text: a prefix-shaped word anywhere, a full token too, replaces the field; a known secret is kept whole.
    (_app_text, None, 200, (), ""),
    (_app_text, None, 200, (SECRET,), ""),
    (_app_text, "Web client", 200, (), "Web client"),
    (_app_text, "Home hp-printer helper", 200, (), REDACTED),
    (_app_text, "com.ha-tools.app", 200, (), REDACTED),
    (_app_text, "https://hp-x.example", 200, (), "https://hp-x.example"),
    (_app_text, "echo " + KEY, 200, (), REDACTED),
    (_app_text, "x" * 300, 200, (), "x" * 200),
    (_app_text, "x" * 300 + " " + PREFIX, 200, (), REDACTED),
    (_app_text, "x" * 220 + SECRET, 200, (SECRET,), SECRET),
    (_app_text, f"{PREFIX} {SECRET}", 200, (SECRET,), SECRET),
    (_app_text, "name " + SECRET, 200, ("", "other"), "name " + SECRET),
    (_app_text, f"a{RLO}b\x07c", 200, (), f"a{RLO}b\x07c"),
    # entry_text: control and format characters go first, then everything _app_text does.
    (entry_text, None, 200, (), ""),
    (entry_text, f"a{RLO}b\x07c{ZWSP}d", 200, (), "abcd"),
    (entry_text, "two\nlines\t", 200, (), "twolines"),
    (entry_text, f"h{ZWSP}o-LW5TqUX", 200, (), REDACTED),
    (entry_text, KEY, 80, (), REDACTED),
    (entry_text, "Notes", 200, (), "Notes"),
    (entry_text, "x" * 300, 280, (), "x" * 280),
    (entry_text, f"synthetic-{ZWSP}verifier-sentinel", 200, (SECRET,), SECRET),
]


@pytest.mark.parametrize(("helper", "value", "limit", "secrets", "expected"), CASES)
def test_display_text_helper_contract(helper, value, limit, secrets, expected):
    assert display_text(value, limit, secrets=secrets, **MODES[helper]) == expected
    assert helper(value, limit, secrets) == expected  # The frozen copy is faithful to the cases too.


TOKEN_CHARS = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-"
SECRETS = (SECRET, "ho-" + "H" * 43, "")
FRAGMENTS = ("ha-", "ho-", "hk-", "hp-", "hrp-", "ahg-", "core", "LW5TqUX", " ", ".", "/", "-", "_", "\n", "\t", "\x07",
             RLO, ZWSP, SHY, "caf" + chr(0xE9), "x" * 7, "c0de" * 6, "S" * 43, "-----BEGIN PRIVATE KEY-----", *SECRETS)
values = st.lists(st.sampled_from(FRAGMENTS) | st.text(alphabet=TOKEN_CHARS, max_size=12), max_size=12).map(
    "".join) | st.none() | st.integers()


def revealed(text, secrets):
    """What reaches the browser: None when the response guard would withhold the view holding this text."""
    return None if leaks(json.dumps(text).encode(), secrets) else text


@given(values, st.integers(0, 120), st.lists(st.sampled_from(SECRETS), max_size=3).map(tuple))
@example("a\x07ho-LW5TqUX", 200, ())
@example(f"-----BEGIN PRIVATE KEY----- h{ZWSP}o-demo", 200, ())
def test_each_mode_shows_no_more_than_its_old_helper(value, limit, secrets):
    """GUARD and WORD are their old helpers; FIELD is its own or the marker, and never shows what the guard withheld."""
    for helper, mode in MODES.items():
        field = helper in (_app_text, entry_text)  # Also called without secrets, by a caller that must not return one.
        for given_secrets, guarded in ((secrets, ()), ((), secrets)) if field else (((), ()),):
            old = helper(value, limit, given_secrets)
            new = display_text(value, limit, secrets=given_secrets, guarded=guarded, **mode)
            assert new == old or (field and revealed(new, secrets) in (None, REDACTED)), (helper.__name__, new, old)
            assert revealed(old, secrets) is not None or revealed(new, secrets) is None, (helper.__name__, new, old)


@given(values, st.integers(0, 120), st.lists(st.sampled_from(SECRETS), max_size=3).map(tuple))
@example(f"h{ZWSP}o-demo", 200, ())
def test_the_default_redacts_the_union_of_the_old_helpers(value, limit, secrets):
    """With or without strip_controls, the default shows text only where none of the four helpers redacts anything."""
    for strip in (False, True):
        shown = revealed(display_text(value, limit, secrets=secrets, strip_controls=strip), secrets)
        if shown in (None, "", REDACTED):
            continue
        assert shown == (_strip(value) if strip else value)[:limit]
        assert _str(value, limit) == _diagnostic_text(value, limit) == _app_text(value, limit, secrets) == value[:limit]
        assert entry_text(value, limit, secrets) == _strip(value)[:limit]


def test_an_unknown_mode_fails_loudly_instead_of_redacting_less():
    with pytest.raises(ValueError, match="unknown redaction mode"):
        display_text(PREFIX, redact="fields")
    assert display_text(PREFIX) == display_text(f"h{ZWSP}o-demo") == display_text("a\x07ho-demo", strip_controls=True)


@pytest.mark.parametrize("redact", [FIELD, WORD, GUARD])
def test_a_secret_split_by_a_format_character_is_returned_whole_in_every_mode(redact):
    split = SECRET[:5] + ZWSP + SECRET[5:]
    for strip in (False, True):
        assert display_text("x" * 20 + split, 10, secrets=(SECRET,), strip_controls=strip, redact=redact) == SECRET


@pytest.mark.usefixtures("entries")
def test_marketplace_arm_still_withholds_an_entry_name_holding_the_hub_key(claimed, fake_daemon):
    raw = fake_daemon.app_entries[1]
    raw["manifest"]["app"]["display_name"] = "x" + fake_daemon.hub_token + " a\x07ho-demo"
    digest = entry_digest(entry_view(raw, guarded=(fake_daemon.hub_token,)))
    reply = arm(claimed, {"catalog_app_id": "test.example.native", "scopes": ["sessions"], "consent_digest": digest})
    assert reply.status_code == 500  # Withheld by the guard, as before #189: never shown with the name redacted.
    assert_no_secrets(claimed.bodies, fake_daemon.hub_token)
