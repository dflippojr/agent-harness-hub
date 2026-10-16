"""The response guard's two failure modes, checked against generated pages instead of a few hand-picked strings.

A page built from ordinary text (dictionary words, tailnet and LAN host names, package names, version strings,
UUID-like ids) is never withheld; the same page with a real secret inserted always is (#84).

Generated names stay shorter than a secret's shape: a prefix followed by a pairing code's 24 token characters (`hp-`,
`hrp-`) or a key's 43 (`ha-`, `ho-`, `hk-`) is withheld by design, even when it is a long hyphenated host name (#149).
"""

from __future__ import annotations

import asyncio
import json
import re

import pytest
from hypothesis import example, given
from hypothesis import strategies as st

from harness_hub.guard import TOKEN_SHAPE, WITHHELD, ResponseGuard, leaks

TOKEN_CHARS = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-"
KEY_PREFIXES = ("ha-", "ho-", "hk-")  # + token_urlsafe(32): 43 characters
CODE_PREFIXES = ("hp-", "hrp-")  # + token_urlsafe(18): 24 characters

# Words people really type next to these prefixes (#84), and plain dictionary words.
PREFIX_WORDS = ("ha-core", "ha-addons", "ha-supervisor", "hp-laserjet", "hp-laserjet-m404", "hp-instruct-7b",
                "ho-ho-ho", "hk-island", "hrp-team", "ha-jung.kim@example.test", "hp-envy-6055e")
DICTIONARY = ("the", "office", "printer", "assistant", "home", "kitchen", "is", "offline", "update", "model",
              "backup", "runner", "approved", "error", "Owner", "Settings", "memory", "profile", "café", "naïve")
SEPARATORS = (" ", "  ", "\n", "\t", ", ", "; ", ": ", " = ", " (", ") ", " / ", "|", '"', "'", "<", ">")

lower = st.text(alphabet="abcdefghijklmnopqrstuvwxyz", min_size=1, max_size=10)
digits = st.integers(0, 9999).map(str)
word = st.sampled_from(DICTIONARY) | st.sampled_from(PREFIX_WORDS) | lower
# A prefix and a short tail: shaped like a key's display prefix (`ho-LW5TqUX`), well short of a key.
short_prefixed = st.builds(lambda p, tail: p + tail, st.sampled_from(KEY_PREFIXES + CODE_PREFIXES),
                           st.text(alphabet=TOKEN_CHARS, min_size=1, max_size=20))
# A host name label of up to three words; one as long as a secret's shape is out of scope here (#149).
label = st.lists(st.sampled_from(DICTIONARY[:16]) | st.sampled_from(PREFIX_WORDS[:8]) | lower | digits,
                 min_size=1, max_size=3).map("-".join).filter(lambda s: "@" not in s and not TOKEN_SHAPE.search(s))
hostname = st.builds(lambda labels, zone: ".".join([*labels, zone]), st.lists(label, min_size=1, max_size=3),
                     st.sampled_from(("tail1234.ts.net", "tailnet-name.ts.net", "local", "lan", "home.arpa",
                                      "example.test", "corp.example")))
ipv4 = st.tuples(*(st.integers(0, 255),) * 3).map(lambda t: "100.{}.{}.{}".format(*t))
ipv6 = st.ip_addresses(v=6).map(str)
package = st.builds(lambda name, op, version: name + op + version,
                    st.sampled_from(("ha-mqtt-discoverable", "hp-proxy", "hk-utils", "hrp-tools", "home-assistant",
                                     "@ha/core", "httpx", "uvicorn", "python-hpilo")),
                    st.sampled_from(("==", "@", " ", "-", ">=")),
                    st.from_regex(r"v?[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}(-(rc|beta|alpha)\.[0-9]{1,2})?(\+[a-z0-9]{1,8})?",
                                  fullmatch=True))
version = st.from_regex(r"[0-9]{1,4}(\.[0-9]{1,4}){1,3}(-[a-z0-9]{1,6})?", fullmatch=True)
uuid_like = st.uuids().map(str) | st.uuids().map(lambda u: u.hex) | st.from_regex(
    r"(k|pr|req|run|job)-[0-9a-f]{8,24}", fullmatch=True)
piece = word | short_prefixed | hostname | ipv4 | ipv6 | package | version | uuid_like


@st.composite
def ordinary_text(draw) -> list[str]:
    """Pieces and the separators between them, as one list so a secret can go between any two."""
    pieces = draw(st.lists(piece, min_size=1, max_size=12))
    out = [pieces[0]]
    for item in pieces[1:]:
        out += [draw(st.sampled_from(SEPARATORS)), item]
    return out


@st.composite
def page(draw, text: st.SearchStrategy[list[str]] | None = None) -> dict:
    """A JSON page: fields holding generated text, some in lists or nested objects, some as keys."""
    text = ordinary_text() if text is None else text
    fields = {}
    for name in draw(st.lists(st.sampled_from(("detail", "model", "profile", "login", "warning", "name", "host",
                                                "problems", "labels", "status")), min_size=1, max_size=5,
                              unique=True)):
        shape = draw(st.sampled_from(("text", "list", "nested", "key")))
        value = "".join(draw(text))
        fields[name] = {"text": value, "list": [value, "fine"], "nested": {"last": value},
                        "key": {value: 1}}[shape]
    return fields


@st.composite
def encoded(draw, body: dict) -> bytes:
    """How the Hub's JSON might spell a page: compact or spaced, raw UTF-8 or ASCII escapes, letters `\\uXXXX`."""
    text = json.dumps(body, ensure_ascii=draw(st.booleans()), separators=draw(st.sampled_from(((",", ":"), None))))
    # Every letter in this output is inside a string (no true/false/null), so any of them may be escaped; the
    # escapes json.dumps already wrote stay as they are.
    escaped = draw(st.sets(st.sampled_from("abhkoprBEGINPRIVATEKY"), max_size=4))
    return ESCAPE.sub(lambda m: f"\\u{ord(m[0]):04x}" if m[0] in escaped else m[0], text).encode()


ESCAPE = re.compile(r"\\u[0-9a-fA-F]{4}|\\.|[A-Za-z]")


def respond(body: bytes, secrets: list[str]) -> tuple[int, bytes]:
    """One `/api/` response through the real middleware."""
    sent: list[dict] = []

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})

    async def send(message):
        sent.append(message)

    asyncio.run(ResponseGuard(app, lambda: secrets)({"type": "http", "path": "/api/page"}, None, send))
    return sent[0]["status"], b"".join(m.get("body", b"") for m in sent[1:])


# The Hub's own secrets in these tests: its key, and the PKCE verifier of a pending claim (43-128 unreserved).
hub_key = st.text(alphabet=TOKEN_CHARS, min_size=43, max_size=43).map(lambda t: "ho-" + t)
verifier = st.text(alphabet=TOKEN_CHARS + ".~", min_size=43, max_size=128)


@given(page(), hub_key, verifier, st.data())
@example({"login": "ha-jung.kim@example.test", "model": "hp-instruct-7b", "labels": ["ha-core", "hp-laserjet"],
          "profile": "I run Home Assistant (ha-core) and print on the hp-laserjet in the office."},
         "ho-" + "s" * 43, "v" * 43, None)  # #84: each of these blanked a page before the fix
@example({"host": "ha-core.tail1234.ts.net", "warning": "hp-laserjet-m404.lan offline",
          "detail": "100.101.102.103 fd7a:115c:a1e0::1 ha-mqtt-discoverable==0.14.0"}, "ho-" + "s" * 43, "v" * 43,
         None)
def test_an_ordinary_page_is_never_withheld(body, key, pkce, data):
    raw = json.dumps(body).encode() if data is None else data.draw(encoded(body))
    assert not leaks(raw, [key, pkce])
    assert respond(raw, [key, pkce]) == (200, raw)


def bearer(token: str) -> str:
    return "Authorization: Bearer " + token


secret_shape = st.one_of(
    st.builds(lambda p, t: p + t, st.sampled_from(KEY_PREFIXES), st.text(alphabet=TOKEN_CHARS, min_size=43,
                                                                          max_size=64)),
    st.builds(lambda p, t: p + t, st.sampled_from(CODE_PREFIXES), st.text(alphabet=TOKEN_CHARS, min_size=24,
                                                                           max_size=40)),
)
pem_header = st.builds(lambda kind, body: f"-----BEGIN {kind}-----{body}",
                       st.sampled_from(("PRIVATE KEY", "RSA PRIVATE KEY", "EC PRIVATE KEY", "OPENSSH PRIVATE KEY",
                                        "DSA PRIVATE KEY", "ENCRYPTED PRIVATE KEY", "PGP PRIVATE KEY BLOCK")),
                       st.sampled_from(("", "\n", "\nMIIEvQIBADANBgkqhkiG9w0BAQEFAASC\n", "\r\nb3BlbnNzaC1rZXk=")))


@st.composite
def secret(draw, key: str, pkce: str) -> str:
    """A real secret: the Hub's key or verifier, any full-length daemon token, either as a bearer header, or a PEM
    private key header."""
    shape = draw(secret_shape)
    return draw(st.sampled_from((key, pkce, shape, bearer(key), bearer(shape), "Bearer " + shape))
                | pem_header)


@st.composite
def with_secret(draw, key: str, pkce: str) -> list[str]:
    text = draw(ordinary_text())
    at = draw(st.integers(0, len(text)))
    # Between two pieces, with a separator on each side, as a field value or line would carry it.
    return [*text[:at], draw(st.sampled_from(SEPARATORS[:4])), draw(secret(key, pkce)),
            draw(st.sampled_from(SEPARATORS[:4])), *text[at:]]


@given(hub_key, verifier, st.data())
@example("ho-" + "s" * 43, "v" * 43, None)
def test_a_page_with_a_real_secret_is_always_withheld(key, pkce, data):
    if data is None:  # the fixed case: the Hub key pasted into a memory profile
        raw = json.dumps({"profile": "I run ha-core; key " + key}).encode()
    else:
        raw = data.draw(encoded(data.draw(page(text=with_secret(key, pkce)))))
    assert leaks(raw, [key, pkce])
    assert respond(raw, [key, pkce]) == (500, WITHHELD)


@pytest.mark.parametrize("header", ["-----BEGIN PRIVATE KEY-----", "-----BEGIN RSA PRIVATE KEY-----",
                                    "-----BEGIN OPENSSH PRIVATE KEY-----", "-----BEGIN PGP PRIVATE KEY BLOCK-----"])
def test_a_private_key_header_is_withheld(header):
    assert leaks(json.dumps({"detail": "pasted\n" + header + "\nMIIEvQ"}).encode(), [])


@pytest.mark.parametrize("text", ["-----BEGIN CERTIFICATE-----", "-----BEGIN PUBLIC KEY-----",
                                  "a private key (BEGIN PRIVATE KEY) goes in the vault", "----- PRIVATE KEY -----"])
def test_public_pem_blocks_and_prose_about_keys_pass(text):
    assert not leaks(json.dumps({"detail": text}).encode(), [])


@pytest.mark.xfail(strict=True, reason="#149: the pairing-code shape cannot tell a long hyphenated name from a code")
def test_a_long_hyphenated_name_is_not_a_pairing_code():
    assert not leaks(b'{"detail":"hp-laserjet-m404-the-office.tail1234.ts.net"}', [])
