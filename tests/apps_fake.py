"""Synthetic Apps metadata and owner routes matching the pinned daemon."""
import time
from copy import deepcopy

from fastapi import Request
from fastapi.responses import JSONResponse, Response

from .marketplace_fake import manifest_entry


def register_apps(app, fake, owner):
    now = time.time()
    fake.apps_calls = []
    fake.apps_error = None
    fake.apps_rows = []
    fake.app_limits = {}
    for label, used in (("never", None), ("minute", now - 60), ("twenty", now - 1200),
                        ("three", now - 3 * 86400), ("thirty", now - 30 * 86400),
                        ("revoked", now - 60), ("pending", now - 60), ("errors", now - 60), ("old", now - 60)):
        row = {"id": "k-" + label, "name": "Synthetic " + label, "kind": "app", "role": "app",
               "scopes": "sessions inference", "origins": ["https://app.test"],
               "prefix": "ha-synthetic-prefix", "catalog_app_id": "catalog-web" if label == "minute" else "",
               "created_at": now - (100 if label == "old" else 1) * 86400, "last_used_at": used,
               "revoked_at": now if label in ("revoked", "pending") else None,
               "erase_after": now + 86400 if label == "pending" else None,
               "erased_at": now if label == "revoked" else None, "retention_days": 30,
               "store": {"sessions": {"running": 2, "waiting_app": 1, "done": 3},
                         "usage": {"requests": 7, "prompt_tokens": 100, "completion_tokens": 40, "cost_usd": 0.5},
                         "errors": 1 if label == "errors" else 0,
                         "last_error": "provider_limit" if label == "errors" else "",
                         "last_error_at": now - 7200 if label == "errors" else None}}
        fake.apps_rows.append(row)
        fake.app_limits[row["id"]] = {"max_running": None, "max_queued": 10}
    fake.app_entries = [
        manifest_entry("catalog-web", "Web client", origins=["https://app.test"], state="paired", paired=["k-minute"]),
        manifest_entry("catalog-financial", "financial-planner")]

    def rows():
        result = deepcopy(fake.apps_rows)
        if fake.hub:
            result.append({"id": fake.hub["key_id"], "name": "Synthetic Hub", "kind": "owner", "role": "hub",
                           "prefix": fake.hub_token[:12], "scopes": "admin", "origins": [], "catalog_app_id": "",
                           "created_at": fake.hub["claimed_at"], "last_used_at": time.time(), "revoked_at": None,
                           "retention_days": None, "erase_after": None, "erased_at": None})
        return result

    fake.key_rows = rows

    def error(request):
        fake.apps_calls.append((request.method, request.url.path))
        if not owner(request):
            return JSONResponse({"detail": "owner key required"}, status_code=401)
        if fake.apps_error:
            return JSONResponse(fake.apps_error[1], status_code=fake.apps_error[0])
        return None

    def find(app_id):
        return next((row for row in fake.apps_rows if row["id"] == app_id), None)

    @app.get("/api/admin/v1/keys")
    async def keys(request: Request):
        failed = error(request)
        return failed if failed else rows()

    @app.get("/api/admin/v1/apps/erasures")
    async def erasures(request: Request):
        failed = error(request)
        if failed:
            return failed
        return [{key: row[key] for key in ("id", "name", "kind", "revoked_at", "erase_after")}
                for row in fake.apps_rows if row["erase_after"] is not None and row["erased_at"] is None]

    @app.delete("/api/admin/v1/keys/{app_id}")
    async def revoke(app_id: str, request: Request):
        failed = error(request)
        if failed:
            return failed
        if fake.hub and app_id == fake.hub["key_id"]:
            return JSONResponse({"detail": "Release with harness hub release --confirm",
                                 "error": {"code": "hub_key"}}, status_code=409)
        row = find(app_id)
        if not row or row["revoked_at"] is not None:
            return JSONResponse({"detail": "no such active key"}, status_code=404)
        row.update(revoked_at=time.time(), erase_after=time.time() + 86400)
        return Response(status_code=204)

    def limits(app_id):
        configured = fake.app_limits[app_id]
        return {"app_id": app_id, "configured": configured,
                "effective": {"max_running": configured["max_running"] or 2,
                              "max_queued": configured["max_queued"] or 20},
                "running": 2, "queued": 0, "parked": 1}

    @app.api_route("/api/admin/v1/apps/{app_id}/limits", methods=["GET", "PUT"])
    async def session_limits(app_id: str, request: Request):
        failed = error(request)
        if failed:
            return failed
        row = find(app_id)
        if not row or row["kind"] != "app" or row["erased_at"] is not None:
            return JSONResponse({"detail": "no App has that id"}, status_code=404)
        if request.method == "PUT":
            fake.app_limits[app_id].update(await request.json())
        return deepcopy(limits(app_id))

    @app.put("/api/admin/v1/apps/{app_id}/retention")
    async def retention(app_id: str, request: Request):
        failed = error(request)
        if failed:
            return failed
        row = find(app_id)
        if not row:
            return JSONResponse({"detail": "no App or device key has that id"}, status_code=404)
        row["retention_days"] = (await request.json())["retention_days"]
        return deepcopy(row)

    @app.post("/api/admin/v1/apps/{app_id}/restore")
    async def forbidden_restore(app_id: str, request: Request):
        raise AssertionError("The Hub must never request an App token response")


def inventory_apps(fake):
    now = time.time()
    result = fake.key_rows()
    for row in result:
        used = row.get("last_used_at")
        row["state"] = ("erasure_pending" if row.get("erase_after") is not None and row.get("erased_at") is None else
                        "revoked" if row.get("revoked_at") is not None else "never_used" if used is None else
                        "active" if now - used <= 900 else "idle" if now - used <= 7 * 86400 else "stale")
        row["token_age_seconds"] = max(0, now - row["created_at"])
        store = row.get("store", {})
        at = store.get("last_error_at")
        row["errors"] = at is not None and 0 <= now - at <= 86400
        row.pop("retention_days", None)  # The real /hub omits this; detail reads the key list.
    return result
