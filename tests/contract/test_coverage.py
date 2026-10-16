"""The contract covers everything on both sides: every route a fake answers, every daemon call the Hub makes, and every
contract endpoint has a case. A fake that grows a route, or a Hub that calls a new one, fails here until the route
has a contract entry (endpoints.py) and a case (test_*_contract.py)."""

from __future__ import annotations

import re
from pathlib import Path

from fastapi.routing import APIRoute

from harness_hub import daemon

from ..conftest import FakeDaemon
from .endpoints import ENDPOINTS, TRIPWIRES

HERE = Path(__file__).parent
KEY = re.compile(r'"((?:GET|POST|PUT|PATCH|DELETE) /[^"\s]*)"')


def normal(key: str) -> str:
    """Path parameter names differ between the fakes and the Hub; only their places matter."""
    return re.sub(r"{[^}]*}", "{}", key)


def fake_routes() -> set[str]:
    return {normal(f"{method} {route.path}") for route in FakeDaemon().app.routes if isinstance(route, APIRoute)
            for method in route.methods}


def hub_calls() -> set[str]:
    """Every daemon route harness_hub/daemon.py can call: its closed tables and its fixed calls."""
    a = daemon.ADMIN
    calls = {f"{method} {a}{path}" for table in (daemon.SETTINGS_ROUTES, daemon.APPS_ROUTES, daemon.PAIRING_ROUTES,
                                                 daemon.ADMIN_ROUTES) for method, path in table.values()}
    calls |= {"GET /health", f"POST {daemon.CLAIM_REQUESTS}", f"POST {daemon.CLAIM_REQUESTS}/{{}}/token",
              f"GET {a}/hub"}
    # DaemonClient.admin(token, path) reads; server.py names each path.
    server = (Path(daemon.__file__).parent / "server.py").read_text(encoding="utf-8")
    calls |= {f"GET {a}{path}" for path in re.findall(r'daemon\.admin\([^,()]+(?:\[[^]]*\])?(?:, "([^"]*)")?\)',
                                                       server)}
    return {normal(call) for call in calls}


def case_keys() -> set[str]:
    return {key for path in HERE.glob("test_*_contract.py") for key in KEY.findall(path.read_text(encoding="utf-8"))}


def test_every_route_a_fake_answers_has_a_contract_entry():
    contract = {normal(key) for key in ENDPOINTS} | set(TRIPWIRES)
    missing = fake_routes() - contract
    assert not missing, f"fake routes without a contract entry in tests/contract/endpoints.py: {sorted(missing)}"


def test_every_tripwire_is_still_a_fake_route():
    assert set(TRIPWIRES) <= fake_routes()


def test_the_contract_is_exactly_the_daemon_calls_the_hub_makes():
    contract = {normal(key) for key in ENDPOINTS}
    calls = hub_calls()
    assert not calls - contract, f"Hub calls without a contract entry: {sorted(calls - contract)}"
    assert not contract - calls, f"contract entries the Hub never calls: {sorted(contract - calls)}"


def test_every_contract_endpoint_has_a_case_and_every_case_names_one():
    keys = case_keys()
    assert not keys - set(ENDPOINTS), f"cases name endpoints the contract lacks: {sorted(keys - set(ENDPOINTS))}"
    assert not set(ENDPOINTS) - keys, f"contract endpoints with no case: {sorted(set(ENDPOINTS) - keys)}"


def test_every_contract_error_status_answers_the_error_shape():
    from .endpoints import ERROR
    for key, endpoint in ENDPOINTS.items():
        for status, shape in endpoint.responses.items():
            assert status < 400 or shape is ERROR, key
