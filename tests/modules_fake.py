"""Synthetic inventory mirroring the pinned daemon's Hub module routes."""
from copy import deepcopy

from fastapi import Request
from fastapi.responses import JSONResponse


def register_inventory(app, fake, owner):
    fake.inventory_error = ""
    fake.inventory_absent = False
    fake.inventory_override = None

    @app.get("/api/admin/v1/hub")
    async def inventory(request: Request):
        if not owner(request):
            return JSONResponse({"detail": "owner key required"}, status_code=401)
        if fake.inventory_absent:
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        if not fake.values["hub.enabled"]:
            return JSONResponse({"detail": "Hub inventory is disabled"}, status_code=400)
        if fake.inventory_error:
            return JSONResponse({"detail": fake.inventory_error}, status_code=400)
        if fake.inventory_override is not None:
            return deepcopy(fake.inventory_override)
        return {"modules": [
            {"name": "hub", "state": "present", "status": {"entries": 1}},
            {"name": "notifications", "state": "present" if fake.values["notify.enabled"] else "switched_off"},
            {"name": "search", "state": "present" if fake.values["search.enabled"] else "switched_off"},
            {"name": "sample", "state": "present", "status": {"state": "error"}},
            {"name": "missing", "state": "absent"},
        ], "apps": [{"id": "synthetic-app", "prefix": fake.hub_token[:12]}],
            "entries": [{"catalog_app_id": "synthetic-entry", "verified": False}],
            "credential": "provider-secret-sentinel"}
