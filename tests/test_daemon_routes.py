"""Every operation in the daemon's route tables, as the Hub sends it: method, URL and timeout.

`DaemonClient` has four public calls (`apps`, `pairing`, `settings`, `administration`), one per table. This pins what
each operation sends through a recording transport, so a change to how the tables are looked up shows up here and not
only in the daemon's contract suite.
"""

from __future__ import annotations

import asyncio
import re

import httpx
import pytest

from harness_hub.daemon import ADMIN, ADMIN_ROUTES, APPS_ROUTES, PAIRING_ROUTES, SETTINGS_ROUTES, DaemonClient

BASE = "https://daemon.test"
TOKEN = "synthetic-token"
DEFAULT = httpx.Timeout(10.0, connect=5.0).as_dict()
LONG = httpx.Timeout(310.0).as_dict()
# The operations the daemon may take minutes over: runner updates, backups and cleanups.
LONG_OPERATIONS = {"runner_update", "backup", "cleanup"}
TABLES = {"apps": APPS_ROUTES, "pairing": PAIRING_ROUTES, "settings": SETTINGS_ROUTES, "admin": ADMIN_ROUTES}
CASES = [(family, operation) for family, table in TABLES.items() for operation in table]


def targets_for(path: str) -> dict[str, str]:
    """A distinct synthetic value for each `{name}` placeholder of the route template."""
    return {name: f"synthetic-{name}" for name in re.findall(r"{(\w+)}", path)}


def expected_path(path: str) -> str:
    return re.sub(r"{(\w+)}", lambda match: f"synthetic-{match.group(1)}", path)


async def call(client: DaemonClient, family: str, operation: str, targets: dict[str, str]) -> None:
    body = None if TABLES[family][operation][0] == "GET" else {"synthetic": True}
    if family == "apps":
        await client.apps(TOKEN, operation, targets.get("app_id", ""), body)
    elif family == "pairing":
        await client.pairing(TOKEN, operation, targets.get("rid", ""), body)
    elif family == "settings":
        await client.settings(TOKEN, operation, body, targets.get("name", ""))
    else:
        await client.administration(TOKEN, operation, targets, body, {"synthetic": "1"})


def sent_by(family: str, operation: str) -> list[tuple]:
    """Run one operation against a recording transport; return what it sent as (method, URL, timeout)."""
    seen: list[tuple] = []

    def answer(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, str(request.url), request.extensions["timeout"]))
        return httpx.Response(200, json={})

    async def run() -> None:
        client = DaemonClient(BASE, transport=httpx.MockTransport(answer))
        try:
            await call(client, family, operation, targets_for(TABLES[family][operation][1]))
        finally:
            await client.aclose()

    asyncio.run(run())
    return seen


@pytest.mark.parametrize(("family", "operation"), CASES, ids=[f"{f}.{o}" for f, o in CASES])
def test_each_operation_is_sent_to_its_route_with_its_timeout(family, operation):
    method, path = TABLES[family][operation]
    timeout = LONG if family == "admin" and operation in LONG_OPERATIONS else DEFAULT
    url = BASE + ADMIN + expected_path(path) + ("?synthetic=1" if family == "admin" else "")
    assert sent_by(family, operation) == [(method, url, timeout)]


def test_the_long_timeout_is_held_by_exactly_runner_update_backup_and_cleanup():
    long_ones = {operation for operation in ADMIN_ROUTES if sent_by("admin", operation)[0][2] == LONG}
    assert long_ones == {"runner_update", "backup", "cleanup"}
