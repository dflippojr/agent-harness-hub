"""Every field the contract lists is a field of the daemon's OpenAPI document at the pinned commit (#269).

The `[daemon]` cases hold the contract to what a disposable daemon answers, but only in the states that daemon can
reach: a reply it cannot produce (`daemon_cannot`), or a field it sends only sometimes (a preflight that is null until
configured), is never compared. agent-harness publishes `tools/openapi/openapi.json` with a documented model per route
(agent-harness#1017), so every field can be held to the daemon's own description, reached or not.

The e2e cases read the document from the checkout HUB_E2E_DAEMON_SRC names (CI's `e2e` job at the pin, `drift.yml`
nightly at main). A path the document still leaves undocumented, and a place inside a reply it leaves open, are listed
as unchecked (an UncheckedContractField warning in the run's summary), not failed. The cases below the e2e ones run in
the unit suite on a small synthetic document and pin how the walk reads OpenAPI.
"""

from __future__ import annotations

import copy
import warnings

import pytest

from .endpoints import ENDPOINTS, Endpoint
from .openapi import DOCUMENT, check, load
from .shapes import INT, STR, ListOf, MapOf, Obj
from .targets import daemon_source


class UncheckedContractField(UserWarning):
    """A contract field on a path, or beneath a place, the daemon's OpenAPI document leaves undocumented."""


@pytest.fixture(scope="module")
def document():
    src = daemon_source()
    if src is None:
        pytest.skip("set HUB_E2E_DAEMON_SRC to an agent-harness checkout")
    assert (src / DOCUMENT).is_file(), f"{src / DOCUMENT} is missing: the daemon publishes it since agent-harness#1017"
    return load(src)


@pytest.mark.e2e
@pytest.mark.parametrize("key", list(ENDPOINTS))
def test_every_contract_field_is_in_the_daemon_openapi_document(document, key):
    missing = check(document, key, ENDPOINTS[key]).missing
    assert not missing, (
        f"{key}: the contract (tests/contract/endpoints.py) lists fields the daemon's OpenAPI document does not. Fix "
        "the contract from the daemon's code, or report the document on agent-harness; never drop the check:\n  "
        + "\n  ".join(missing))


@pytest.mark.e2e
def test_the_unchecked_fields_are_listed(document):
    """The paths still undocumented, and the open places in documented replies, by endpoint."""
    unchecked = {key: result for key, endpoint in ENDPOINTS.items()
                 if (result := check(document, key, endpoint)).unchecked}
    for key, result in unchecked.items():
        state = "undocumented path" if not result.documented else "open in the document"
        warnings.warn(UncheckedContractField(f"{key} ({state}): " + "; ".join(result.unchecked)), stacklevel=1)
    print("\n".join(f"unchecked  {key}: {', '.join(result.unchecked)}" for key, result in unchecked.items()))


@pytest.mark.e2e
def test_a_field_the_document_lacks_fails(document):
    """The deliberate miss: take one field the contract lists out of the real document, at each place it is read."""
    doc = copy.deepcopy(document)
    schemas = doc["components"]["schemas"]
    del schemas["HealthResponse"]["properties"]["release"]
    del schemas["CreatePairingRequest"]["properties"]["code_challenge"]
    del schemas["PairingRequestView"]["properties"]["elevated"]
    audit = doc["paths"]["/api/admin/v1/accounts/audit"]["get"]
    audit["parameters"] = [p for p in audit["parameters"] if p["name"] != "limit"]

    def missing(key):
        return check(doc, key, ENDPOINTS[key]).missing

    assert missing("GET /health") == ["200 $.release"]
    assert missing("POST /api/v1/pair/requests") == ["request $.code_challenge"]
    assert missing("GET /api/admin/v1/pairing-requests") == ["200 $[].elevated"]
    assert missing("GET /api/admin/v1/accounts/audit") == ["query limit"]
    assert check(document, "GET /health", ENDPOINTS["GET /health"]).missing == []


# The walk itself, on a synthetic document (unit suite) ----------------------------------------------------------------
ROW = Obj({"id": STR}, optional={"tags": ListOf(STR)})
DOC = {
    "paths": {
        "/rows": {
            "get": {"parameters": [{"name": "limit", "in": "query"}, {"name": "x", "in": "header"}],
                    "responses": {"200": {"content": {"application/json": {"schema": {
                        "type": "array", "items": {"$ref": "#/components/schemas/Row"}}}}}}},
            "post": {"requestBody": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/New"}}}},
                     "responses": {"201": {"content": {"application/json": {"schema": {
                         "anyOf": [{"$ref": "#/components/schemas/Row"}, {"type": "null"}]}}}}}},
        },
        "/rows/{rid}": {
            "get": {"responses": {"200": {"content": {"application/json": {"schema": {
                "allOf": [{"$ref": "#/components/schemas/Row"}, {"properties": {"extra": {"type": "object"}}}]}}}}}},
            "delete": {"responses": {"204": {"description": "No Content"}}},
            "put": {"responses": {"200": {"content": {"application/json": {"schema": {}}}}}},
        },
        "/stats": {"get": {"responses": {"200": {"content": {"application/json": {"schema": {
            "type": "object", "properties": {
                "by_kind": {"type": "object", "additionalProperties": {"$ref": "#/components/schemas/Row"}},
                "open": {"type": "object", "additionalProperties": True},
                "named": {"type": "object", "additionalProperties": {"type": "integer"}},
                "count": {"type": "integer"}}}}}}}}},
    },
    "components": {"schemas": {
        "Row": {"type": "object", "properties": {"id": {"type": "string"},
                                                 "tags": {"type": "array", "items": {"type": "string"}}}},
        "New": {"type": "object", "properties": {"name": {"type": "string"}}},
    }},
}


def run(key, endpoint):
    return check(DOC, key, endpoint)


def test_a_documented_list_reply_and_its_query_pass():
    result = run("GET /rows", Endpoint(None, {200: ListOf(ROW)}, params=Obj(optional={"limit": INT})))
    assert (result.missing, result.unchecked, result.documented) == ([], [], True)


def test_a_missing_field_is_named_at_its_depth():
    result = run("GET /rows", Endpoint(None, {200: ListOf(ROW.extend({"name": STR}))}))
    assert result.missing == ["200 $[].name"]


def test_a_query_field_and_a_header_are_told_apart():
    assert run("GET /rows", Endpoint(None, {200: ListOf(ROW)}, params=Obj({"x": STR}))).missing == ["query x"]


def test_a_request_field_is_held_to_the_request_body():
    assert run("POST /rows", Endpoint(Obj({"name": STR}), {201: ROW, 400: Obj({"detail": STR})})).missing == []
    assert run("POST /rows", Endpoint(Obj({"title": STR}), {201: ROW})).missing == ["request $.title"]


def test_a_request_body_the_operation_does_not_take_fails():
    assert run("GET /rows", Endpoint(Obj({"q": STR}), {200: ListOf(ROW)})).missing == [
        "request $.q: the operation takes no body"]


def test_a_union_reply_matches_its_object_branch_and_allof_merges():
    assert run("POST /rows", Endpoint(Obj(), {201: ROW | None})).missing == []
    assert run("GET /rows/{row_id}", Endpoint(None, {200: ROW.extend({"extra": Obj()})})).missing == []
    assert run("GET /rows/{row_id}", Endpoint(None, {200: ROW.extend({"more": STR})})).missing == ["200 $.more"]


def test_an_undocumented_reply_is_unchecked_not_failed():
    result = run("PUT /rows/{row_id}", Endpoint(Obj(), {200: ROW, 201: ROW}))
    assert (result.documented, result.missing) == (False, [])
    assert result.unchecked == ["200 $: undocumented reply", "201 $: undocumented reply"]


def test_a_reply_status_the_document_lacks_fails():
    assert run("DELETE /rows/{row_id}", Endpoint(None, {204: None})).missing == []
    assert run("GET /rows", Endpoint(None, {200: ListOf(ROW), 202: None})).missing == [
        "202: not a documented reply status"]


def test_an_operation_the_document_lacks_fails():
    assert run("PATCH /rows", Endpoint(None, {200: None})).missing == [
        "PATCH /rows: not in the daemon's OpenAPI document"]


def test_maps_open_places_and_kind_mismatches():
    stats = Obj({"by_kind": MapOf(ROW), "open": Obj({"a": INT}), "named": Obj({"total": INT}), "count": INT})
    result = run("GET /stats", Endpoint(None, {200: stats}))
    assert (result.missing, result.unchecked) == ([], ["200 $.open: the document leaves it open (a)"])
    wrong = Obj({"count": Obj({"n": INT}), "by_kind": MapOf(Obj({"nope": STR})), "open": ListOf(ROW),
                 "named": MapOf(ROW)})
    assert sorted(run("GET /stats", Endpoint(None, {200: wrong})).missing) == [
        "200 $.by_kind.*.nope", "200 $.count: not an object in the document",
        "200 $.named.*: not an object in the document", "200 $.open: not an array in the document"]
    loose = Obj({"open": MapOf(ROW)})
    assert run("GET /stats", Endpoint(None, {200: loose})).unchecked == ["200 $.open.*: the document leaves it open"]


# Review round 1 (#271): each place the walk could pass a missing field --------------------------------------------
REVIEWED = {
    "paths": {
        "/made": {"post": {"responses": {"200": {"content": {"application/json": {"schema": {
            "$ref": "#/components/schemas/Row"}}}}}}},
        "/wrapped": {"get": {"responses": {"200": {"content": {"application/json": {"schema": {"allOf": [
            {"type": "object", "additionalProperties": {"$ref": "#/components/schemas/Row"}}]}}}}}}},
        "/listed": {"get": {"responses": {"200": {"content": {"application/json": {"schema": {"allOf": [
            {"type": "array", "items": {"$ref": "#/components/schemas/Row"}}]}}}}}}},
        "/closed": {"get": {"responses": {"200": {"content": {"application/json": {"schema": {
            "type": "object", "additionalProperties": False}}}}}}},
    },
    "components": DOC["components"],
}


def test_a_documented_operation_is_not_undocumented_because_the_contract_names_another_status():
    result = check(REVIEWED, "POST /made", Endpoint(None, {201: ROW}))
    assert (result.documented, result.missing, result.unchecked) == (True, ["201: not a documented reply status"], [])


def test_allof_keeps_map_values_and_list_items():
    assert check(REVIEWED, "GET /wrapped", Endpoint(None, {200: MapOf(Obj({"nope": STR}))})).missing == [
        "200 $.*.nope"]
    assert check(REVIEWED, "GET /wrapped", Endpoint(None, {200: MapOf(ROW)})).missing == []
    assert check(REVIEWED, "GET /listed", Endpoint(None, {200: ListOf(Obj({"nope": STR}))})).missing == [
        "200 $[].nope"]


def test_a_closed_object_fails_every_name_it_does_not_list():
    result = check(REVIEWED, "GET /closed", Endpoint(None, {200: Obj({"a": INT}, optional={"b": STR})}))
    assert (result.missing, result.unchecked) == (["200 $.a", "200 $.b"], [])
