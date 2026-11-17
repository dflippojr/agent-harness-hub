"""Browser projection for pairing requests."""

from __future__ import annotations

import time
from typing import Any

from .text import _app_id, _dict, _finite, _list, _names, display_text

# App pairing requests (#519). The Hub keeps its own copy of the elevated scopes so a daemon that forgets to flag one
# still cannot have it approved without the owner's separate acknowledgement.
PAIRING_STATES = ("pending", "armed", "claimed", "approved", "redeemed", "denied", "expired")
PAIRING_NEEDS = ("approve", "claim", "confirm", "redeem")
ELEVATED_SCOPES = frozenset({"sessions:all", "approvals", "remote_control", "memory_library", "homelab"})


def pairing_request_view(row: Any, secrets: tuple[str, ...] = ()) -> dict:
    row = _dict(row)
    kind = row.get("kind") if row.get("kind") in ("app", "hub") else "unknown"
    state = row.get("state") if row.get("state") in PAIRING_STATES else "unknown"
    scopes = [display_text(s, 64, secrets=secrets) for s in _list(row.get("scopes"))[:32] if isinstance(s, str)]
    disclosures = []
    for item in _list(row.get("disclosures"))[:32]:
        if isinstance(item, dict) and isinstance(item.get("scope"), str):
            disclosures.append({"scope": display_text(item["scope"], 64, secrets=secrets),
                                "tier": "elevated" if item.get("tier") == "elevated" else "standard",
                                "text": display_text(item.get("text"), 4000, secrets=secrets)})
    flagged = set(_names(row.get("elevated"))) | {d["scope"] for d in disclosures if d["tier"] == "elevated"}
    elevated = [s for s in scopes if s in ELEVATED_SCOPES or s in flagged or s == "admin"]
    for item in disclosures:
        if item["scope"] in elevated:
            item["tier"] = "elevated"
    # Owner decision: codes come only from the App's screen, never from an owner listing (#519 step 2).
    needs = row.get("needs") if row.get("needs") in PAIRING_NEEDS else ""
    return {"id": _app_id(row.get("id")), "kind": kind, "state": state, "needs": needs,
            "name": display_text(row.get("name"), 120, secrets=secrets),
            "catalog_app_id": display_text(row.get("catalog_app_id"), 200, secrets=secrets),
            "origin": display_text(row.get("origin"), 500, secrets=secrets),
            "browser": row.get("browser") is True, "armed": row.get("armed") is True,
            "scopes": scopes, "elevated": elevated, "disclosures": disclosures,
            "key_id": _app_id(row.get("key_id")),
            **{key: _finite(row.get(key)) for key in ("created_at", "expires_at", "approved_at", "finished_at")}}


def pairing_attention(body: Any, now: float) -> dict | None:
    """How many unexpired App requests wait on an owner decision. Counts only: no ids, names, origins or codes.

    Hub claims are decided on the daemon host, and `claim`/`redeem` are the App's next step, so neither is counted."""
    if not isinstance(body, list):
        return None
    counts = {"approve": 0, "confirm": 0}
    for raw in body:
        row = pairing_request_view(raw)
        if row["kind"] == "app" and row["needs"] in counts and (row["expires_at"] or 0) > now:
            counts[row["needs"]] += 1
    return counts


def pairing_view(body: Any, secrets: tuple[str, ...] = ()) -> dict:
    rows = [pairing_request_view(row, secrets) for row in _list(body) if isinstance(row, dict)]
    rows = [row for row in rows if row["id"]]
    rows.sort(key=lambda row: row["created_at"] or 0, reverse=True)
    return {"requests": rows, "now": time.time()}
