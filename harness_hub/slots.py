"""The browser slots and consent screens the Marketplace and the pairing listings share (#255): the backstop for an
observed browser claim that skipped the confirmation gate, and the copies of a consent screen the owner may still
approve. Both live on `HubContext`, so each Hub keeps its own."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, NamedTuple

from .daemon import DaemonUnreachable
from .views import entry_digest, ownership_view, pairing_request_view

if TYPE_CHECKING:
    from .routes.common import HubContext


class ConsentCopy(NamedTuple):
    """A consent screen the owner was shown, kept for the Hub's ten-minute window: the entry's identity when it was
    shown, the scope disclosures, whether it needs the elevated acknowledgement, and when it stops being reused
    (`expires`, on the monotonic clock)."""

    identity: str
    expires: float
    disclosures: list[dict]
    acknowledge: bool


def observe_browser_slots(ctx: HubContext, rows: list, key_id: str, *, confirmed_id: str = "") -> bool:
    """Whether an observed browser claim looks like it skipped confirmation; `skipped_confirmation` rules on it."""
    states, warning = ctx.browser_slot_states, False
    for raw in rows:
        row = pairing_request_view(raw)
        if not (row["id"] and row["kind"] == "app" and row["armed"] and row["browser"]):
            continue
        identity = (key_id, row["id"])
        previous, bypassed = states.get(identity, ("", False))
        bypassed = bypassed or (previous == "armed" and row["state"] in ("approved", "redeemed")
                                and row["id"] != confirmed_id)
        if identity not in states and len(states) >= 256:
            states.pop(next(iter(states)))
        states[identity] = (row["state"], bypassed)
        warning = warning or bypassed
    return warning


async def skipped_confirmation(ctx: HubContext, key: dict, suspected: bool) -> str:
    """The warning for a suspected bypass, only from a daemon that does not enforce confirmation.

    Owner API 1.24 and later enforce typed-code confirmation, so from them an `armed` slot that turned `approved`
    between listings was confirmed outside the Hub (the host CLI, another Hub process, a Hub restart): no warning,
    and the stale suspicion is dropped. A daemon whose version is old or cannot be read keeps it."""
    if not suspected:
        return ""
    try:
        reply = await ctx.daemon.admin(key["token"])
    except DaemonUnreachable:
        reply = None
    if reply is not None and reply.ok and ownership_view(
            reply.body, {}, "")["compatibility"] in ("supported", "no_tailnet_signin"):
        states = ctx.browser_slot_states
        for identity, (state, _) in list(states.items()):
            if identity[0] == key["key_id"]:
                states[identity] = (state, False)
        return ""
    return "This daemon approved the claim without confirmation; update the daemon."


def consent_identity(entry: dict) -> str:
    return entry_digest({k: v for k, v in entry.items() if k not in {
        "state", "paired", "pending_pairing", "unverified_origin", "consent_digest", "slots",
        "acknowledgement_required"}})


def consent_entry(ctx: HubContext, entry: dict, key_id: str) -> dict:
    copied = ctx.consent_copies.get((key_id, entry["catalog_app_id"]))
    if copied and copied.identity == consent_identity(entry) and copied.expires > time.monotonic():
        entry["scopes"] = [{**item, **disclosure} for item, disclosure in zip(
            entry["scopes"], copied.disclosures, strict=True)]
        entry["elevated"] = sorted(set(entry["elevated"]) | {
            item["scope"] for item in copied.disclosures if item["tier"] == "elevated"})
        if copied.acknowledge:
            entry["acknowledgement_required"] = True
    entry["consent_digest"] = entry_digest(entry)
    return entry


def remember_consent(ctx: HubContext, entry: dict, key_id: str, disclosures: list[dict], *, acknowledge: bool = False):
    consent_copies = ctx.consent_copies
    if len(consent_copies) >= 256:
        consent_copies.pop(next(iter(consent_copies)))
    consent_copies[(key_id, entry["catalog_app_id"])] = ConsentCopy(
        consent_identity(entry), time.monotonic() + 600, disclosures, acknowledge)
