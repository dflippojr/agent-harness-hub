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

from . import openapi
from .endpoints import ENDPOINTS, Endpoint
from .openapi import DOCUMENT, UNCOMPARED, check, load
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


def test_a_union_reply_matches_its_object_branch():
    assert run("POST /rows", Endpoint(Obj(), {201: ROW | None})).missing == []
    assert run("POST /rows", Endpoint(Obj(), {201: ROW.extend({"more": STR}) | None})).missing == ["201 $.more"]


def test_an_undocumented_reply_is_unchecked_not_failed():
    result = run("PUT /rows/{row_id}", Endpoint(Obj(), {200: ROW}))
    assert (result.documented, result.missing, result.unchecked) == (False, [], ["200 $: undocumented reply"])
    # A status the document lacks is missing even beside an undocumented one (review round 4, #271).
    result = run("PUT /rows/{row_id}", Endpoint(Obj(), {200: ROW, 201: ROW}))
    assert (result.missing, result.unchecked) == (["201: not a documented reply status"], ["200 $: undocumented reply"])


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
        "/closed": {"get": {"responses": {"200": {"content": {"application/json": {"schema": {
            "type": "object", "additionalProperties": False}}}}}}},
    },
    "components": DOC["components"],
}


def test_a_documented_operation_is_not_undocumented_because_the_contract_names_another_status():
    result = check(REVIEWED, "POST /made", Endpoint(None, {201: ROW}))
    assert (result.documented, result.missing, result.unchecked) == (True, ["201: not a documented reply status"], [])


def test_a_closed_object_fails_every_name_it_does_not_list():
    result = check(REVIEWED, "GET /closed", Endpoint(None, {200: Obj({"a": INT}, optional={"b": STR})}))
    assert (result.missing, result.unchecked) == (["200 $.a", "200 $.b"], [])


# allOf is not compared, wherever the walk meets it (review round 2, #271) ------------------------------------------
# The daemon's document has no allOf (test_the_daemon_document_uses_only_modeled_forms pins that). A hand-merged
# allOf lost a constraint three ways in review, so the walk fails closed instead: an allOf anywhere it reaches is a
# miss, even inside one anyOf or oneOf branch while another branch would match.
ROW_REF = {"$ref": "#/components/schemas/Row"}
ALL_ROW = {"allOf": [ROW_REF]}


def _reply(schema):
    return {"get": {"responses": {"200": {"content": {"application/json": {"schema": schema}}}}}}


GRID = {
    "paths": {
        "/root": _reply(ALL_ROW),
        "/property": _reply({"type": "object", "properties": {"row": ALL_ROW}}),
        "/items": _reply({"type": "array", "items": ALL_ROW}),
        "/values": _reply({"type": "object", "additionalProperties": ALL_ROW}),
        "/any": _reply({"anyOf": [ALL_ROW, {"type": "null"}]}),
        "/one": _reply({"oneOf": [ALL_ROW, ROW_REF]}),
        "/deep-any": _reply({"anyOf": [{"type": "array", "items": ALL_ROW}, {"type": "array", "items": ROW_REF}]}),
        "/ref": _reply({"$ref": "#/components/schemas/Wrapped"}),
        "/request": {"post": {
            "requestBody": {"content": {"application/json": {"schema": {"allOf": [
                {"$ref": "#/components/schemas/New"}]}}}},
            "responses": {"200": {"content": {"application/json": {"schema": ROW_REF}}}}}},
        "/plain-one": _reply({"oneOf": [{"type": "null"}, ROW_REF]}),
    },
    "components": {"schemas": {**DOC["components"]["schemas"], "Wrapped": ALL_ROW}},
}


@pytest.mark.parametrize("key,endpoint,where", [
    ("GET /root", Endpoint(None, {200: ROW}), "200 $"),
    ("GET /property", Endpoint(None, {200: Obj({"row": ROW})}), "200 $.row"),
    ("GET /items", Endpoint(None, {200: ListOf(ROW)}), "200 $[]"),
    ("GET /values", Endpoint(None, {200: MapOf(ROW)}), "200 $.*"),
    ("GET /any", Endpoint(None, {200: ROW | None}), "200 $"),
    ("GET /one", Endpoint(None, {200: ROW}), "200 $"),
    ("GET /deep-any", Endpoint(None, {200: ListOf(ROW)}), "200 $[]"),
    ("GET /ref", Endpoint(None, {200: ROW}), "200 $"),
    ("POST /request", Endpoint(Obj({"name": STR}), {200: ROW}), "request $"),
], ids=["reply-root", "property", "list-items", "map-values", "anyOf-branch", "oneOf-branch",
        "another-branch-matches", "behind-ref", "request-body"])
def test_an_allof_anywhere_fails(key, endpoint, where):
    result = check(GRID, key, endpoint)
    assert (result.missing, result.unchecked) == ([f"{where}: allOf {UNCOMPARED}"], [])


def test_oneof_branches_like_anyof():
    assert check(GRID, "GET /plain-one", Endpoint(None, {200: ROW})).missing == []
    assert check(GRID, "GET /plain-one", Endpoint(None, {200: ROW.extend({"more": STR})})).missing == ["200 $.more"]


# Only the schema forms the walk models are compared (owner decision on #271, option A) ----------------------------
# The allowlist: a $ref alone, anyOf or oneOf alone, an object (type, properties, additionalProperties, required), an
# array (type, items), a scalar, or an untyped schema, each with annotations and value-only constraints beside it.
# Any other form the walk reaches fails, wherever it sits; test_the_daemon_document_uses_only_modeled_forms pins the
# daemon's document at none.
CLOSED_ROW = {"type": "object", "properties": {"id": {"type": "string"}, "title": {"type": "string"}},
              "additionalProperties": False}
FORMS = {
    "paths": {
        # review round 3's repro: a closed object whose anyOf branches carry only `required`
        "/sibling-any": _reply({**CLOSED_ROW, "anyOf": [{"required": ["id"]}, {"required": ["title"]}]}),
        "/sibling-one": _reply({"type": "object", "properties": {
            "row": {"oneOf": [ROW_REF], "additionalProperties": False}}}),
        "/both": _reply({"type": "array", "items": {"anyOf": [ROW_REF], "oneOf": [ROW_REF]}}),
        "/not": _reply({"type": "object", "additionalProperties": {"not": {"type": "string"}}}),
        "/type-list": _reply({"anyOf": [{"type": ["object", "null"], "properties": {"id": {"type": "string"}}},
                                        {"type": "null"}]}),
        "/pattern": _reply({"$ref": "#/components/schemas/Patterned"}),
        "/object-items": {"post": {
            "requestBody": {"content": {"application/json": {"schema": {
                "type": "object", "properties": {"name": {"type": "string"}}, "items": {"type": "string"}}}}},
            "responses": {"200": {"content": {"application/json": {"schema": ROW_REF}}}}}},
        "/array-properties": _reply({"type": "array", "items": ROW_REF, "properties": {"id": {"type": "string"}}}),
        "/ref-sibling": _reply({"$ref": "#/components/schemas/Row", "properties": {"extra": {"type": "string"}}}),
        "/unknown": _reply({"type": "object", "properties": {"id": {"type": "string"}}, "if": {}}),
        "/deep-not": _reply({"anyOf": [{"type": "array", "items": {"not": {}}}, {"type": "array", "items": ROW_REF}]}),
        "/modeled": _reply({"anyOf": [{"$ref": "#/components/schemas/Row", "title": "A row"}, {"type": "null"}],
                            "title": "Row or nothing", "discriminator": {"propertyName": "id"}}),
    },
    "components": {"schemas": {
        **DOC["components"]["schemas"],
        "Patterned": {"type": "object", "patternProperties": {"^x": ROW_REF}},
    }},
}


@pytest.mark.parametrize("key,endpoint,entry", [
    ("GET /sibling-any", Endpoint(None, {200: Obj({"id": STR, "nope": STR})}),
     "200 $: anyOf beside additionalProperties, properties, type"),
    ("GET /sibling-one", Endpoint(None, {200: Obj({"row": ROW})}), "200 $.row: oneOf beside additionalProperties"),
    ("GET /both", Endpoint(None, {200: ListOf(ROW)}), "200 $[]: anyOf beside oneOf"),
    ("GET /not", Endpoint(None, {200: MapOf(ROW)}), "200 $.*: not in an untyped schema"),
    ("GET /type-list", Endpoint(None, {200: ROW | None}), '200 $: type ["object", "null"]'),
    ("GET /pattern", Endpoint(None, {200: Obj({"a": ROW})}), "200 $: patternProperties in an object schema"),
    ("POST /object-items", Endpoint(Obj({"name": STR}), {200: ROW}), "request $: items in an object schema"),
    ("GET /array-properties", Endpoint(None, {200: ListOf(ROW)}), "200 $: properties in an array schema"),
    ("GET /ref-sibling", Endpoint(None, {200: ROW}), "200 $: $ref beside properties"),
    ("GET /unknown", Endpoint(None, {200: Obj({"id": STR})}), "200 $: if in an object schema"),
    ("GET /deep-not", Endpoint(None, {200: ListOf(ROW)}), "200 $[]: not in an untyped schema"),
], ids=["anyOf-siblings-r3", "oneOf-sibling", "anyOf-and-oneOf", "not", "type-list", "patternProperties-behind-ref",
        "object-with-items-request", "array-with-properties", "ref-with-sibling", "unknown-keyword",
        "another-branch-matches"])
def test_an_unmodeled_form_anywhere_fails(key, endpoint, entry):
    result = check(FORMS, key, endpoint)
    assert (result.missing, result.unchecked) == ([f"{entry} {UNCOMPARED}"], [])


def test_the_modeled_forms_with_annotations_and_value_constraints_pass():
    assert check(FORMS, "GET /modeled", Endpoint(None, {200: ROW | None})).missing == []
    assert check(FORMS, "GET /modeled", Endpoint(None, {200: ROW.extend({"more": STR}) | None})).missing == [
        "200 $.more"]


def test_unmodeled_forms_counts_every_form_at_every_depth():
    counts = openapi.unmodeled_forms(FORMS)
    assert counts == {
        "anyOf beside additionalProperties, properties, type": 1, "oneOf beside additionalProperties": 1,
        "anyOf beside oneOf": 1, "not in an untyped schema": 2, 'type ["object", "null"]': 1,
        "patternProperties in an object schema": 1, "items in an object schema": 1,
        "properties in an array schema": 1, "$ref beside properties": 1, "if in an object schema": 1}
    assert openapi.unmodeled_forms(GRID) == {"allOf": 9}
    assert openapi.unmodeled_forms(DOC) == {"allOf": 1}


@pytest.mark.e2e
def test_the_daemon_document_uses_only_modeled_forms(document):
    """The walk compares only the allowlisted forms; this pins the daemon's document at none outside it (allOf, an
    anyOf or oneOf with structural siblings, a type list, an unknown keyword, ...). When it fails, the daemon emits a
    new form: teach tests/contract/openapi.py to compare it, with a case in the grids above."""
    assert openapi.unmodeled_forms(document) == {}



# Boolean schemas (review round 4, F1) -------------------------------------------------------------------------------
# Every schema node goes through the allowlist: `true` is open (it is `{}`), `false` admits no value, so every contract
# name beneath it is missing; any other value where a schema belongs is unmodeled.
BOOLS = {
    "paths": {
        "/false-root": _reply(False),
        "/false-property": _reply({"type": "object", "properties": {"row": False, "id": {"type": "string"}}}),
        "/false-items": _reply({"type": "array", "items": False}),
        "/false-values": _reply({"type": "object", "additionalProperties": False}),
        "/false-branches": _reply({"anyOf": [False, False]}),
        "/false-beside-row": _reply({"anyOf": [False, ROW_REF]}),
        "/false-ref": _reply({"$ref": "#/components/schemas/Never"}),
        "/false-request": {"post": {
            "requestBody": {"content": {"application/json": {"schema": False}}},
            "responses": {"200": {"content": {"application/json": {"schema": ROW_REF}}}}}},
        "/true-root": _reply(True),
        "/true-property": _reply({"type": "object", "properties": {"row": True}}),
        "/true-items": _reply({"type": "array", "items": True}),
        "/true-ref": _reply({"$ref": "#/components/schemas/Anything"}),
        "/string-items": _reply({"type": "array", "items": "string"}),
    },
    "components": {"schemas": {**DOC["components"]["schemas"], "Never": False, "Anything": True}},
}


@pytest.mark.parametrize("key,endpoint,missing,unchecked", [
    ("GET /false-root", Endpoint(None, {200: ROW}), ["200 $.id", "200 $.tags"], []),
    ("GET /false-property", Endpoint(None, {200: Obj({"row": ROW, "id": STR})}), ["200 $.row"], []),
    ("GET /false-property", Endpoint(None, {200: Obj({"row": STR})}), ["200 $.row"], []),
    ("GET /false-items", Endpoint(None, {200: ListOf(ROW)}), ["200 $[].id", "200 $[].tags"], []),
    ("GET /false-values", Endpoint(None, {200: MapOf(ROW)}), ["200 $.*.id", "200 $.*.tags"], []),
    ("GET /false-branches", Endpoint(None, {200: ROW}), ["200 $.id", "200 $.tags"], []),
    ("GET /false-beside-row", Endpoint(None, {200: ROW}), [], []),
    ("GET /false-ref", Endpoint(None, {200: ListOf(ROW)}), ["200 $[].id", "200 $[].tags"], []),
    ("POST /false-request", Endpoint(Obj({"name": STR}), {200: ROW}), ["request $.name"], []),
    ("GET /true-root", Endpoint(None, {200: ROW}), [], ["200 $: the document leaves it open (id, tags)"]),
    ("GET /true-property", Endpoint(None, {200: Obj({"row": ROW})}), [],
     ["200 $.row: the document leaves it open (id, tags)"]),
    ("GET /true-items", Endpoint(None, {200: ListOf(ROW)}), [], ["200 $[]: the document leaves it open (id, tags)"]),
    ("GET /true-ref", Endpoint(None, {200: ROW}), [], ["200 $: the document leaves it open (id, tags)"]),
    ("GET /string-items", Endpoint(None, {200: ListOf(ROW)}),
     [f"200 $[]: a string where a schema belongs {UNCOMPARED}"], []),
], ids=["false-root", "false-property", "false-property-scalar", "false-items", "false-map-values",
        "false-every-branch", "false-beside-a-matching-branch", "false-behind-ref", "false-request-body", "true-root",
        "true-property", "true-items", "true-behind-ref", "a-string-as-a-schema"])
def test_boolean_schemas_at_every_position(key, endpoint, missing, unchecked):
    result = check(BOOLS, key, endpoint)
    assert (sorted(result.missing), result.unchecked) == (missing, unchecked)


def test_the_scan_counts_boolean_schemas_and_every_non_schema():
    assert openapi.boolean_schemas(BOOLS) == 9
    assert openapi.unmodeled_forms(BOOLS) == {"a string where a schema belongs": 1}
    assert openapi.boolean_schemas(DOC) == 0


# Each contract success status classified on its own (review round 4, F2) --------------------------------------------
# No early return: a status the document lacks is missing, a documented status with no body (a 204) is missing where
# the contract reads a body, a `{}` schema is unchecked, and the request's and query's entries always count.
KINDS = {
    "absent": ({"200": {"content": {"application/json": {"schema": ROW_REF}}}}, 201,
               ["201: not a documented reply status"], []),
    "no-content": ({"204": {"description": "No Content"}}, 204, ["204 $: documented with no body"], []),
    "empty-schema": ({"200": {"content": {"application/json": {"schema": {}}}}}, 200, [],
                     ["200 $: undocumented reply"]),
    "schema": ({"200": {"content": {"application/json": {"schema": ROW_REF}}}}, 200, [], []),
}
REQUESTS = {
    "clean-request": ({"$ref": "#/components/schemas/New"}, []),
    "unmodeled-request": ({"allOf": [{"$ref": "#/components/schemas/New"}]}, [f"request $: allOf {UNCOMPARED}"]),
}


@pytest.mark.parametrize("request_kind", list(REQUESTS))
@pytest.mark.parametrize("reply_kind", list(KINDS))
def test_each_reply_status_is_classified_and_the_request_always_counts(reply_kind, request_kind):
    responses, status, missing, unchecked = KINDS[reply_kind]
    body, request_missing = REQUESTS[request_kind]
    doc = {"paths": {"/kind": {"post": {"requestBody": {"content": {"application/json": {"schema": body}}},
                                        "responses": responses}}},
           "components": DOC["components"]}
    result = check(doc, "POST /kind", Endpoint(Obj({"name": STR}), {status: ROW}))
    assert (sorted(result.missing), result.unchecked) == (sorted(request_missing + missing), unchecked)
    assert result.documented is (reply_kind != "empty-schema")


def test_a_reply_documented_without_a_json_body_fails():
    text = {"get": {"responses": {"200": {"content": {"text/plain": {"schema": {"type": "string"}}}}}}}
    doc = {"paths": {"/text": text}, "components": DOC["components"]}
    assert check(doc, "GET /text", Endpoint(None, {200: ROW})).missing == ["200 $: documented without a JSON body"]


@pytest.mark.e2e
def test_the_daemon_document_reply_kinds_and_boolean_schemas_are_pinned(document):
    """The kinds the walk meets today, made visible: no boolean schema, and 18 bodyless success replies, every one a
    204. When a count moves, look at what the daemon added and whether the walk compares it."""
    assert openapi.boolean_schemas(document) == 0
    assert openapi.bodyless_replies(document) == {"204": 18}
