"""Browser projection for the Modules page."""

from __future__ import annotations

import math
from typing import Any

from .text import GUARD, WORD, _dict, display_text


def module_view(body: Any) -> list[dict]:
    """Only module identity, presence and up to eight scalar status fields reach the browser."""
    rows = _dict(body).get("modules", [])
    out = []
    for row in rows:
        shown = {"name": display_text(row.get("name"), 200, redact=GUARD), "state": row.get("state")}
        detail = row.get("status")
        if isinstance(detail, dict):
            fields = {}
            for key, value in list(detail.items())[:8]:
                if not isinstance(key, str) or len(key) > 200:
                    continue
                if isinstance(value, str):
                    fields[key] = display_text(value, 200, redact=WORD)
                elif value is None or type(value) is bool or type(value) is int and value.bit_length() <= 64 or (
                        type(value) is float and math.isfinite(value)):
                    fields[key] = value
            shown["status"] = fields
        out.append(shown)
    return out
