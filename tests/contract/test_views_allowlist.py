"""The browser allowlists in harness_hub/views.py are held to the daemon contract (#252).

`views.SCHEMAS` says which reply fields the Hub passes on to the browser; tests/contract/endpoints.py says which ones
the daemon sends. Nothing else compares the two, and they drift: #88's audit rows showed no time because the view
allowlisted `at`, `timestamp` and `created_at` while the daemon sends `ts`, and every test passed because each one
checked only that the projection keeps what the fake sends.

This test maps each operation through `daemon.ADMIN_ROUTES` to its contract entry and walks the view's schema beside
the 2xx reply shapes. A views schema shared by several operations (SKILL serves proposals and installed skills;
BACKUP serves the backup run and the maintenance summary) may list any field that one of the replies it serves lists.
A reply the contract describes loosely (`MapOf(ANY)`, `ANY`) names no fields, so the view beneath it is neither
checked nor counted. Every other allowlisted name must be a contract field, or sit in ALLOWED_EXTRA with its reason.
"""

from __future__ import annotations

from collections.abc import Mapping

from harness_hub import daemon, views

from .endpoints import ADMIN, ENDPOINTS
from .shapes import ANY, NULL, ListOf, MapOf, Obj, Shape, Union

# Allowlisted names the contract does not list, kept on purpose. Each key is a views schema (by its name in views.py,
# then the nested keys) and a field; each value says why the view keeps it.
#
# Every entry below is a field the daemon sends, at the pin and at the README's minimum daemon (aa5a904), in a reply
# no disposable daemon can produce: its `[daemon]` case is a `daemon_cannot` skip or reads the module switched off. A
# contract field is one a `[daemon]` case proves, so these wait here. Move one into tests/contract/endpoints.py when a
# case can reach its reply.
_REMOTE = ("harness_modules/remote_control/service.py RemoteControl.status() sends it in folder rows; Remote Control "
           "needs Claude Code on the daemon host, so the [daemon] case skips")
_PROPOSAL = ("harness_modules/skills/service.py public_proposal() sends it in every proposal; a proposal comes from "
             "an agent session, so the [daemon] case skips")
ALLOWED_EXTRA: dict[str, str] = {
    "FOLDER.invalid": _REMOTE + " (a managed folder that is unusable, or 'slug_conflict').",
    "FOLDER.owner_only": _REMOTE + " (true for a folder discovery added).",
    "FOLDER.pid": _REMOTE + " (the running Remote Control process, null when stopped).",
    "FOLDER.started_at": _REMOTE + " (when Remote Control last started there).",
    "FOLDER.trust_prompt_open": _REMOTE + " (every row).",
    "SKILL.created_at": _PROPOSAL + ".",
    "SKILL.updated_at": _PROPOSAL + "; SkillStore._public_installed() sends it for installed skills too.",
    "SKILL.target_slug": _PROPOSAL + " (the installed skill a proposal updates, or '').",
    "SKILL.example_count": _PROPOSAL + ".",
    "SKILL.reference_count": _PROPOSAL + ".",
    "SKILL.manifest": _PROPOSAL.replace("in every proposal", "in the proposal read (include_body)")
    + "; its keys are the ones service.py builds the manifest from.",
    "MAINTENANCE.image_archive.bytes": "harness_modules/images/archive.py ImageArchive.health() sends it; the "
    "disposable daemon runs without the images module, so GET /maintenance answers only {\"enabled\": false} there.",
}


def _options(shape: Shape | None) -> list[Shape]:
    if isinstance(shape, Union):
        return [leaf for option in shape.options for leaf in _options(option)]
    return [] if shape is None or shape is NULL else [shape]


def _loose(shape: Shape) -> bool:
    return shape is ANY or (isinstance(shape, MapOf) and shape.value is ANY)


def _names() -> dict[int, str]:
    return {id(value): name for name, value in vars(views).items()
            if name.isupper() and isinstance(value, Mapping)}


def _replies(operation: str) -> list[Shape]:
    method, path = daemon.ADMIN_ROUTES[operation]
    endpoint = ENDPOINTS[f"{method} {ADMIN}{path}"]
    return [shape for status, shape in endpoint.responses.items() if 200 <= status < 300 and shape is not None]


def _walk(schema, shape: Shape, label: str, seen: dict[int, tuple[str, Mapping, set[str]]],
          names: dict[int, str]) -> None:
    """Record, for each views mapping, the contract fields of every reply object it projects."""
    options = _options(shape)
    if isinstance(schema, list):
        for option in options:
            if isinstance(option, ListOf):
                _walk(schema[0], option.item, label + "[]", seen, names)
        return
    if not isinstance(schema, Mapping):
        return
    objects = [option for option in options if isinstance(option, Obj)]
    if not objects or any(_loose(option) for option in options):
        return  # described loosely or not an object: no field list to hold the view to
    label = names.get(id(schema), label)
    _, _, fields = seen.setdefault(id(schema), (label, schema, set()))
    for option in objects:
        fields |= option.fields()
    for key, inner in schema.items():
        for option in objects:
            if key in option.fields():
                _walk(inner, (option.required | option.optional)[key], f"{label}.{key}", seen, names)


def unlisted() -> dict[str, list[str]]:
    """Each allowlisted name no reply its schema serves lists, with the operations that schema serves."""
    names, seen = _names(), {}
    served: dict[int, set[str]] = {}
    for operation, schema in views.SCHEMAS.items():
        for reply in _replies(operation):
            _walk(schema, reply, operation, seen, names)
        _mark(schema, operation, served)
    out = {}
    for key, (label, schema, fields) in seen.items():
        for name in schema:
            if name not in fields:
                out[f"{label}.{name}"] = sorted(served.get(key, ()))
    return out


def _mark(schema, operation: str, served: dict[int, set[str]]) -> None:
    if isinstance(schema, list):
        _mark(schema[0], operation, served)
    elif isinstance(schema, Mapping):
        served.setdefault(id(schema), set()).add(operation)
        for inner in schema.values():
            _mark(inner, operation, served)


def test_every_views_allowlisted_name_is_a_contract_field_or_explained():
    found = unlisted()
    unexplained = {name: ops for name, ops in found.items() if name not in ALLOWED_EXTRA}
    assert not unexplained, (
        f"{len(unexplained)} names views.SCHEMAS passes to the browser that no daemon reply in "
        "tests/contract/endpoints.py lists (#252). Add each to the contract (with a [daemon] case), drop it from "
        "the view, or explain it in ALLOWED_EXTRA:\n" + "\n".join(
            f"  {name}  (served by: {', '.join(ops)})" for name, ops in sorted(unexplained.items())))


def test_allowed_extra_names_only_names_the_contract_still_lacks():
    stale = set(ALLOWED_EXTRA) - set(unlisted())
    assert not stale, f"ALLOWED_EXTRA names that are now contract fields or left the view: {sorted(stale)}"


def test_every_views_schema_operation_has_a_contract_reply():
    assert set(views.SCHEMAS) <= set(daemon.ADMIN_ROUTES)
    assert all(_replies(operation) for operation in views.SCHEMAS)


def test_a_name_the_daemon_never_sends_is_caught(monkeypatch):
    """#88 again: an audit time under a name the daemon does not send fails here, at its path."""
    monkeypatch.setitem(views.AUDIT, "at", views.NUMBER)
    monkeypatch.setitem(views.AUDIT["metadata"], "when", views.NUMBER)  # below MapOf(ANY): nothing to hold it to
    assert unlisted()["AUDIT.at"] == ["account_audit", "audit"]
    assert "AUDIT.metadata.when" not in unlisted()
