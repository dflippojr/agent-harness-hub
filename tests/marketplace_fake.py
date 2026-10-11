"""Synthetic local Hub entries and the owner's arm route, matching agent-harness harness_modules/hub (#520) and
harness/pairing_requests.py `arm_pairing_request` (#519, flow 4)."""
import secrets
import time

from fastapi import Request
from fastapi.responses import JSONResponse

from harness_hub.views import SCOPE_DISCLOSURES

from .pairing_fake import ELEVATED, refusal

SCOPES = {"sessions", "sessions:all", "approvals", "images", "inference", "remote_control", "models:warm",
          "memory_library", "homelab"}


def manifest_entry(app_id, name, scopes=("sessions",), origins=(), state="not_paired", paired=(), **app):
    """One entry as `GET /hub` reports it: the full manifest (abridged to what the schema requires of the Hub's
    fields, plus URLs and e-mail the Hub must never show or fetch), `verified: false` and the pairing state."""
    return {"catalog_app_id": app_id, "verified": False, "state": state, "paired": list(paired),
            "pending_pairing": False, "unverified_origin": {"paired": [], "pending_pairing": False},
            "manifest": {"manifest_schema_version": "1.0", "app": {
                "app_id": app_id, "version": "1.0.0", "display_name": name,
                "summary": f"Synthetic local entry for {name}.",
                "publisher": {"id": "pub-0a1b2c3d4e5f", "name": "Synthetic publisher",
                              "website_url": "https://publisher.example.invalid/",
                              "security_email": "security@example.invalid",
                              "support_email": "support@example.invalid"},
                "urls": {"source": f"https://example.invalid/{app_id}/source",
                         "install": f"https://example.invalid/{app_id}/install"},
                "scopes": [{"scope": scope, "risk_tier": "elevated" if scope in ELEVATED else "standard",
                            "justification": f"Synthetic justification for {scope}, long enough for the schema."}
                           for scope in scopes],
                "browser_origins": list(origins), **app},
                "review": {"decision": "approved", "evidence_urls": [f"https://example.invalid/{app_id}/review"]}}}


def with_pairing_state(fake, entries):
    """`pending_pairing` as the daemon derives it: an active request for the id from a listed origin; native ones
    are reported under `unverified_origin`."""
    active = [row for row in fake.pairing_rows.values() if row["state"] in ("pending", "armed", "claimed", "approved")]
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("manifest"), dict):
            continue  # a malformed row a test feeds the Hub as-is
        origins = set(entry["manifest"]["app"].get("browser_origins") or [])
        mine = [row for row in active if row["catalog_app_id"] == entry["catalog_app_id"]]
        entry["pending_pairing"] = entry["pending_pairing"] or any(row["origin"] in origins for row in mine
                                                                   if row["origin"])
        unverified = entry.setdefault("unverified_origin", {"paired": [], "pending_pairing": False})
        unverified["pending_pairing"] = unverified.get("pending_pairing") or any(not row["origin"] for row in mine)
    return entries


def register_marketplace(app, fake, owner):
    fake.armed = []
    fake.arm_disclosures = dict(SCOPE_DISCLOSURES)

    @app.post("/api/admin/v1/pairing-requests")
    async def arm(request: Request):
        fake.pairing_calls.append((request.method, request.url.path))
        if not owner(request):
            return refusal(401, "owner key required")
        body = await request.json()
        fake.armed.append(body)
        scopes = body.get("scopes") or ["sessions"]
        if not body.get("catalog_app_id"):
            return refusal(400, "catalog_app_id is required", "invalid_request")
        if "admin" in scopes or any(scope not in fake.arm_disclosures for scope in scopes):
            return refusal(400, "unknown or empty scopes", "invalid_request")
        if ELEVATED.intersection(scopes) and body.get("acknowledge_elevated") is not True:
            return refusal(400, "elevated scopes need acknowledge_elevated", "acknowledgement_required")
        rid = fake.add_pairing(body.get("name") or body["catalog_app_id"], scopes, state="armed",
                               origin=body.get("origin", ""), catalog_app_id=body["catalog_app_id"],
                               text=fake.arm_disclosures)
        row = fake.pairing_rows[rid]
        row["approved_at"] = row["created_at"]
        return JSONResponse(fake.pairing_view(row), status_code=201)

    def claim_slot(rid, origin=""):
        """The App claims an armed slot with its own challenge (the Hub never does)."""
        row = fake.pairing_rows[rid]
        assert row["state"] == "armed" and row["origin"] == origin
        now = time.time()
        row.update(state="claimed", match_code=f"{secrets.randbelow(10**6):06d}", expires_at=now + 600)
        return row

    fake.claim_slot = claim_slot
