"""The contract's fields against the daemon's published OpenAPI document (#269, agent-ops#637 stage 5).

agent-harness publishes `tools/openapi/openapi.json`: every route it serves, most of them with a documented model.
`check()` walks one contract endpoint (endpoints.py) beside its operation in that document and names every field the
contract lists that the document does not: a request field, a query field, or a field of a success reply, at any
depth. Only names are compared. Types, nullability and required-ness stay with the `[daemon]` cases, which hold the
contract to the real daemon's answers.

Two things are not failures but *unchecked*, and `check()` lists them apart:

- a path whose success replies are still undocumented (an empty schema, `{}`): agent-harness#1017 left some;
- a place inside a documented reply that the document leaves open (an object with no properties, or a map whose value
  has no schema) while the contract names fields beneath it.

Error replies are not compared: the document has only FastAPI's 422 there, and every refusal shares ERROR.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .endpoints import Endpoint
from .shapes import Enum, ListOf, MapOf, Obj, Shape, Union, _Scalar

DOCUMENT = Path("tools") / "openapi" / "openapi.json"
METHODS = ("get", "put", "post", "delete", "patch")
UNCOMPARED = "allOf is not compared; extend tests/contract/openapi.py"


@dataclass
class Result:
    documented: bool = True  # False: the path's success replies have no schema yet
    missing: list[str] = field(default_factory=list)  # contract fields the document lacks: each one fails
    unchecked: list[str] = field(default_factory=list)  # contract fields beneath a place the document leaves open
    # Places the walk met an allOf: it does not compare one, so each fails (check() adds them to `missing`), whichever
    # anyOf or oneOf branch the rest of the walk settles on.
    uncompared: list[str] = field(default_factory=list)


def load(src: Path) -> dict[str, Any]:
    """The OpenAPI document of an agent-harness checkout."""
    return json.loads((src / DOCUMENT).read_text(encoding="utf-8"))


def normal(path: str) -> str:
    """The Hub and the daemon name path parameters differently; only their places matter."""
    return re.sub(r"{[^}]*}", "{}", path)


def operation(doc: dict[str, Any], key: str) -> dict[str, Any] | None:
    method, path = key.split(" ", 1)
    for doc_path, item in doc.get("paths", {}).items():
        if normal(doc_path) == normal(path) and method.lower() in item:
            return item[method.lower()]
    return None


def check(doc: dict[str, Any], key: str, endpoint: Endpoint) -> Result:
    op = operation(doc, key)
    if op is None:
        return Result(missing=[f"{key}: not in the daemon's OpenAPI document"])
    result = Result()
    walker = _Walker(doc.get("components", {}).get("schemas", {}), result)
    if endpoint.request is not None:
        body = op.get("requestBody", {}).get("content", {}).get("application/json", {}).get("schema")
        if body is None:
            result.missing += [f"request $.{name}: the operation takes no body"
                               for name in sorted(_names(endpoint.request))]
        else:
            walker.compare(endpoint.request, body, "request $")
    if isinstance(endpoint.params, Obj):
        query = {p["name"] for p in op.get("parameters", []) if p.get("in") == "query"}
        result.missing += [f"query {name}" for name in sorted(endpoint.params.fields() - query)]
    replies = {status: _reply_schema(reply) for status, reply in op.get("responses", {}).items()}
    success = {status: shape for status, shape in endpoint.responses.items() if status < 400}
    bodies = {status: shape for status, shape in success.items() if shape is not None}
    if bodies and not any(schema for status, schema in replies.items() if status.startswith("2")):
        # Still undocumented (agent-harness#1017): the operation describes no success reply, whatever the contract
        # expects, so there is nothing to hold the reply to, and no reply status either.
        result.documented = False
        result.unchecked += [f"{status} $: undocumented reply" for status in bodies]
        return result
    for status, shape in success.items():
        if str(status) not in replies:
            result.missing.append(f"{status}: not a documented reply status")
        elif shape is not None:
            walker.compare(shape, replies[str(status)], f"{status} $")
    result.missing += [entry for entry in dict.fromkeys(result.uncompared) if entry not in result.missing]
    return result


def _reply_schema(reply: dict[str, Any]) -> dict[str, Any]:
    return reply.get("content", {}).get("application/json", {}).get("schema") or {}


def _names(shape: Shape | None) -> set[str]:
    """The field names a shape lists at its top level."""
    if isinstance(shape, Obj):
        return shape.fields()
    if isinstance(shape, Union):
        return set().union(*(_names(o) for o in shape.options))
    return set()


def _names_fields(shape: Shape | None) -> bool:
    """Whether the shape lists any field name at any depth: only then can the document be missing one."""
    if isinstance(shape, Obj):
        return bool(shape.fields())
    if isinstance(shape, Union):
        return any(_names_fields(o) for o in shape.options)
    if isinstance(shape, ListOf):
        return _names_fields(shape.item)
    if isinstance(shape, MapOf):
        return _names_fields(shape.value)
    return False


class _Walker:
    def __init__(self, schemas: dict[str, Any], result: Result):
        self.schemas, self.result = schemas, result
        self.dropped = False

    def resolve(self, schema: Any) -> dict[str, Any]:
        seen = set()
        while isinstance(schema, dict) and "$ref" in schema:
            ref = schema["$ref"]
            if ref in seen:
                raise ValueError(f"circular $ref {ref}")
            seen.add(ref)
            schema = self.schemas[ref.rsplit("/", 1)[-1]]
        return schema if isinstance(schema, dict) else {}

    def alternatives(self, schema: Any, where: str) -> list[dict[str, Any]]:
        """Each schema a value may match: every anyOf or oneOf branch (for field names a oneOf is an anyOf).

        The one place the walk meets an allOf. It does not compare one: a hand merge of allOf parts lost a constraint
        three ways in review (#271), and the daemon's document has none (test_the_daemon_document_has_no_allof). It is
        recorded as uncompared, which fails the endpoint, and offers no alternative to compare against."""
        schema = self.resolve(schema)
        if "allOf" in schema:
            self.result.uncompared.append(f"{where}: {UNCOMPARED}")
            return []
        for word in ("anyOf", "oneOf"):
            if word in schema:
                return [alt for part in schema[word] for alt in self.alternatives(part, where)]
        return [schema]

    def compare(self, shape: Shape | None, schema: Any, where: str) -> None:
        if shape is None or isinstance(shape, _Scalar | Enum) or not _names_fields(shape):
            return
        if isinstance(shape, Union):
            for option in shape.options:
                self.compare(option, schema, where)
            return
        before = len(self.result.uncompared)
        alts = self.alternatives(schema, where)
        # A branch here was an allOf: the value may well be that branch, so the others' kind is no mismatch.
        self.dropped = len(self.result.uncompared) > before
        if not alts:
            return  # an allOf, already recorded as uncompared
        if isinstance(shape, Obj):
            self._obj(shape, alts, where)
        elif isinstance(shape, ListOf):
            items = [alt["items"] for alt in alts if alt.get("type") == "array" and "items" in alt]
            self._branch(shape.item, items, alts, "array", f"{where}[]")
        elif isinstance(shape, MapOf):
            values = [_map_value(alt) for alt in alts if _map_value(alt)]
            self._branch(shape.value, values, alts, "object", f"{where}.*")

    def _branch(self, inner: Shape, schemas: list[Any], alts: list[dict[str, Any]], kind: str, where: str) -> None:
        """Into a list's items or a map's values: the alternative that misses fewest wins."""
        if not schemas:
            if any(_open(alt, kind) for alt in alts):
                self.result.unchecked.append(f"{where}: the document leaves it open")
            elif not self.dropped:
                self.result.missing.append(f"{where.rstrip('.*[]')}: not an {kind} in the document")
            return
        self._best([lambda r, s=s: _Walker(self.schemas, r).compare(inner, s, where) for s in schemas])

    def _obj(self, shape: Obj, alts: list[dict[str, Any]], where: str) -> None:
        objects = [alt for alt in alts if alt.get("properties") or _map_value(alt) or _closed(alt)]
        if not objects:
            if any(_open(alt, "object") for alt in alts):
                names = ", ".join(sorted(shape.fields()))
                self.result.unchecked.append(f"{where}: the document leaves it open ({names})")
            elif not self.dropped:
                self.result.missing.append(f"{where}: not an object in the document")
            return

        def against(alt: dict[str, Any]):
            props, rest = alt.get("properties", {}), _map_value(alt)

            def run(result: Result) -> None:
                walker = _Walker(self.schemas, result)
                for name in sorted(shape.fields()):
                    schema = props.get(name, rest)
                    if schema is None:
                        result.missing.append(f"{where}.{name}")
                    else:
                        walker.compare((shape.required | shape.optional)[name], schema, f"{where}.{name}")
            return run

        self._best([against(alt) for alt in objects])

    def _best(self, runs) -> None:
        results = []
        for run in runs:
            result = Result()
            run(result)
            results.append(result)
        best = min(results, key=lambda r: (len(r.missing), len(r.unchecked)))
        self.result.missing += best.missing
        self.result.unchecked += best.unchecked
        self.result.uncompared += [entry for r in results for entry in r.uncompared]  # every branch's, not the best's


def _map_value(schema: dict[str, Any]) -> dict[str, Any] | None:
    """The schema of every value of an object whose keys are data, if the document gives one."""
    value = schema.get("additionalProperties")
    return value if isinstance(value, dict) and value else None


def _closed(schema: dict[str, Any]) -> bool:
    """An object the document closes: it names every field it may have, and may name none."""
    return schema.get("additionalProperties") is False


def _open(schema: dict[str, Any], kind: str) -> bool:
    """A place the document leaves open: no schema at all, or a value of the right kind with nothing inside."""
    if not schema:
        return True
    if schema.get("type") not in (kind, None):
        return False
    if kind == "array":
        return not schema.get("items")
    return not schema.get("properties") and not _map_value(schema) and not _closed(schema)
