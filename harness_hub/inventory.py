"""Server-only last good inventory, shared by Modules and future Apps/Marketplace views.

The complete daemon snapshot stays private. Consumers must project browser responses
through views.py; in particular the apps' display prefixes must never reach the browser.
"""

from __future__ import annotations

import copy
import json
import math
import time
from pathlib import Path
from typing import Any

from .state import read_json, write_private

INVENTORY_FILE = "inventory.json"
MODULE_STATES = {"present", "switched_off", "absent"}


def valid_inventory(body: Any) -> bool:
    if not isinstance(body, dict) or any(not isinstance(body.get(key), list) for key in ("modules", "apps", "entries")):
        return False
    rows = body["modules"]
    return all(isinstance(row, dict) and isinstance(row.get("name"), str) and row["name"]
               and isinstance(row.get("state"), str) and row["state"] in MODULE_STATES for row in rows) and (
                   len({row["name"] for row in rows}) == len(rows))


class InventoryCache:
    def __init__(self, state_dir: Path, daemon_url: str):
        self.path = state_dir / INVENTORY_FILE
        self.daemon_url = daemon_url
        try:
            data = read_json(self.path, None)
        except (OSError, ValueError):
            data = None
        self._good = data if isinstance(data, dict) and data.get("daemon_url") == daemon_url and (
            valid_inventory(data.get("inventory"))) and type(data.get("fetched_at")) in (int, float) and (
            math.isfinite(data["fetched_at"])) else None

    def get(self, key_id: str) -> dict | None:
        return copy.deepcopy(self._good) if self._good and self._good.get("key_id") == key_id else None

    def save(self, body: dict, key_id: str) -> dict:
        snapshot = {"daemon_url": self.daemon_url, "key_id": key_id,
                    "fetched_at": time.time(), "inventory": copy.deepcopy(body)}
        write_private(self.path, json.dumps(snapshot, allow_nan=False) + "\n")
        self._good = snapshot
        return self.get(key_id)
