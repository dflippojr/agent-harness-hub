"""Checks a request or an answer against the contract, with nothing else loaded.

The unit suite's fake daemon transport (tests/conftest.py) checks every request the Hub sends with `check_sent`, so this
module imports only the contract itself, not the real-daemon machinery in targets.py.
"""

from __future__ import annotations

import json
import re
from typing import Any

from .endpoints import ENDPOINTS, ERROR


def check_request(key: str, body: Any, params: dict | None = None) -> list[str]:
    """What is wrong with a request the Hub (or a case) sends to `key`, against the contract."""
    endpoint = ENDPOINTS[key]
    if endpoint.request is None:
        problems = [] if body is None else [f"{key} takes no body, got {body!r}"]
    else:
        problems = endpoint.request.errors(body, "request")
    if params:
        problems += endpoint.params.errors(params, "query") if endpoint.params else [f"{key} takes no query string"]
    return problems


_ROUTES = [(key, re.compile(re.sub(r"\\{[^}]*\\}", "[^/]+", re.escape(key.split(" ", 1)[1])) + "$"))
           for key in ENDPOINTS]


def endpoint_for(method: str, path: str) -> str | None:
    """The contract key a request on the wire is for, or None."""
    return next((key for key, pattern in _ROUTES if key.startswith(method + " ") and pattern.match(path)), None)


def check_sent(method: str, path: str, raw: bytes, query: dict[str, str]) -> list[str]:
    """What is wrong with one request the Hub sent: the route, the JSON body and the query field names."""
    key = endpoint_for(method, path)
    if key is None:
        return [f"the Hub called {method} {path}, which is not a contract endpoint"]
    try:
        body = json.loads(raw) if raw else None
    except ValueError:
        return [f"{key}: the body is not JSON"]
    problems = check_request(key, body)
    params = ENDPOINTS[key].params
    unknown = set(query) - (params.fields() if params else set())
    return problems + ([f"{key}: unexpected query fields {sorted(unknown)}"] if unknown else [])


def check_response(key: str, status: int, body: Any) -> list[str]:
    endpoint = ENDPOINTS[key]
    allowed = set(endpoint.responses) | set(endpoint.common)
    if status not in allowed:
        return [f"{key} answered HTTP {status}, which the contract does not list ({sorted(allowed)}): {body!r:.300}"]
    shape = endpoint.responses.get(status, ERROR)
    if shape is None:
        return []
    return [f"{key} HTTP {status} {problem}" for problem in shape.errors(body)]
