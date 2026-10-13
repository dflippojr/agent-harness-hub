"""Synthetic App pairing requests and owner routes matching agent-harness harness/pairing_requests.py (#519)."""
import re
import secrets
import time

import httpx
from fastapi import Request
from fastapi.responses import JSONResponse

ELEVATED = {"sessions:all", "approvals", "remote_control", "memory_library", "homelab"}
ACTIVE = ("pending", "armed", "claimed", "approved")
DISCLOSURES = {
    "sessions": "This app can start agent tasks on your harness and read the tasks it created.",
    "approvals": "This app can **approve or deny tool calls** in its own tasks.",
    "homelab": "This app's tasks can **read your homelab services' logs, config and metrics**.",
    "admin": "This is a **Hub claim**: approve it only on the daemon host with `harness hub approve`.",
}


def refusal(status: int, detail: str, code: str = "") -> JSONResponse:
    return JSONResponse({"detail": detail, "error": {"code": code, "message": detail, "retryable": False}},
                        status_code=status)


def register_pairing(app, fake, owner):
    fake.pairing_calls = []
    fake.pairing_rows = {}
    fake.pairing_list_error = None
    fake.pairing_deny_error = None
    fake.before_pairing_deny = None

    def add(name, scopes, state="pending", origin="https://app.test", catalog_app_id="", text=None, kind="app",
            expires_in=600):
        """A request as the App made it (or as the owner armed it). Returns its id; the code is on the row."""
        now = time.time()
        rid = "pr-" + secrets.token_hex(12)
        fake.pairing_rows[rid] = {
            "id": rid, "kind": kind, "name": name, "scopes": list(scopes), "catalog_app_id": catalog_app_id,
            "origin": origin, "armed": state in ("armed", "claimed"), "state": state,
            "match_code": f"{secrets.randbelow(10**6):06d}" if state in ("pending", "claimed") else "",
            "created_at": now, "expires_at": now + expires_in, "approved_at": None, "finished_at": None, "key_id": "",
            "text": text or {}}
        return rid

    fake.add_pairing = add

    def view(row):
        scopes = row["scopes"]
        state = row["state"]
        return {"id": row["id"], "kind": row["kind"], "name": row["name"], "scopes": scopes,
                "catalog_app_id": row["catalog_app_id"], "origin": row["origin"], "browser": bool(row["origin"]),
                "armed": row["armed"], "state": state,
                "match_code": "",
                "needs": {"pending": "approve", "armed": "claim", "claimed": "confirm",
                          "approved": "redeem"}.get(state, ""),
                "created_at": row["created_at"], "expires_at": row["expires_at"], "approved_at": row["approved_at"],
                "finished_at": row["finished_at"], "key_id": row["key_id"],
                "elevated": [s for s in scopes if s in ELEVATED or s == "admin"],
                "disclosures": [{"scope": s, "tier": "elevated" if s in ELEVATED or s == "admin" else "standard",
                                 "text": row.get("text", {}).get(s, DISCLOSURES.get(s, ""))} for s in scopes]}

    fake.pairing_view = view

    def hub_rows():
        """The Hub's own claims, which the real daemon lists with every other request."""
        now = time.time()
        return [{"id": rid, "kind": "hub", "name": claim["name"], "scopes": ["admin"], "catalog_app_id": "",
                 "origin": "", "armed": False, "state": claim["state"], "match_code": claim["match_code"],
                 "created_at": now - 30, "expires_at": now + 570, "approved_at": None, "finished_at": None,
                 "key_id": ""} for rid, claim in fake.requests.items()]

    def guard(request):
        fake.pairing_calls.append((request.method, request.url.path))
        if not owner(request):
            return JSONResponse({"detail": "owner key required"}, status_code=401)
        return None

    @app.get("/api/admin/v1/pairing-requests")
    async def listing(request: Request):
        failed = guard(request)
        if failed:
            return failed
        if fake.pairing_list_error:
            return JSONResponse(fake.pairing_list_error[1], status_code=fake.pairing_list_error[0])
        rows = list(fake.pairing_rows.values()) + hub_rows()
        rows.sort(key=lambda row: row["created_at"], reverse=True)
        return [view(row) for row in rows]

    def decide(rid, states):
        row = fake.pairing_rows.get(rid)
        if row is None:
            if rid in fake.requests:
                return None, refusal(403, "a Hub claim is approved or denied only on the daemon host",
                                     "host_proof_required")
            return None, refusal(404, "no pairing request has that id", "not_found")
        if row["state"] not in states:
            return None, refusal(409, f"this pairing request is {row['state']}", "not_approvable")
        return row, None

    def same(row, given):
        return bool(row["match_code"]) and re.sub(r"[\s-]", "", given or "") == row["match_code"]

    def approved(row):
        now = time.time()
        row.update(state="approved", approved_at=now, expires_at=now + 300, match_code="")

    @app.post("/api/admin/v1/pairing-requests/{rid}/approve")
    async def approve(rid: str, request: Request):
        failed = guard(request)
        if failed:
            return failed
        body = await request.json()
        fake.pairing_bodies.append(("approve", rid, body))
        row, failed = decide(rid, ("pending",))
        if failed:
            return failed
        if ELEVATED.intersection(row["scopes"]) and body.get("acknowledge_elevated") is not True:
            return refusal(400, "elevated scopes need acknowledge_elevated", "")
        if not same(row, body.get("match")):
            return refusal(400, "the match code does not match the one the App shows", "match_mismatch")
        approved(row)
        return view(row)

    @app.post("/api/admin/v1/pairing-requests/{rid}/confirm")
    async def confirm(rid: str, request: Request):
        failed = guard(request)
        if failed:
            return failed
        body = await request.json()
        fake.pairing_bodies.append(("confirm", rid, body))
        row, failed = decide(rid, ("claimed",))
        if failed:
            return failed
        if not same(row, body.get("match")):
            return refusal(400, "the match code does not match the one the App shows", "match_mismatch")
        approved(row)
        return view(row)

    @app.post("/api/admin/v1/pairing-requests/{rid}/deny")
    async def deny(rid: str, request: Request):
        if fake.before_pairing_deny:
            await fake.before_pairing_deny()
        if fake.pairing_deny_error == "refused":
            return refusal(500, "withdrawal failed")
        if fake.pairing_deny_error == "unreachable":
            raise httpx.ConnectError("synthetic withdrawal outage")
        failed = guard(request)
        if failed:
            return failed
        fake.pairing_bodies.append(("deny", rid, None))
        row, failed = decide(rid, ACTIVE)
        if failed:
            return failed
        row.update(state="denied", finished_at=time.time(), match_code="")
        return view(row)

    fake.pairing_bodies = []

    @app.post("/api/v1/pair/requests/{rid}/claim")
    async def forbidden_claim(rid: str):
        raise AssertionError("The Hub must never claim an App's armed slot")
