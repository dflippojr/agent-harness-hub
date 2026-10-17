"""A small shape language for the daemon contract: field names, types, required or optional, and nulls.

    Obj({"id": STR, "revoked_at": NUM | None}, optional={"store": STORE})

Extra fields are allowed everywhere: the daemon may send more than the Hub reads, and the fakes deliberately add
private fields the Hub must never pass through. `errors()` returns one line per mismatch, with a JSON-path-like
location, so a failing contract case says which field drifted.
"""

from __future__ import annotations

from typing import Any


class Shape:
    def errors(self, value: Any, where: str = "$") -> list[str]:
        raise NotImplementedError

    def __or__(self, other: Shape | None) -> Shape:
        return Union(self, NULL if other is None else other)

    def __ror__(self, other: Shape | None) -> Shape:
        return Union(NULL if other is None else other, self)


class _Scalar(Shape):
    def __init__(self, name: str, check):
        self.name, self.check = name, check

    def errors(self, value, where="$"):
        return [] if self.check(value) else [f"{where}: expected {self.name}, got {_kind(value)}"]

    def __repr__(self):
        return self.name


STR = _Scalar("string", lambda v: isinstance(v, str))
BOOL = _Scalar("boolean", lambda v: isinstance(v, bool))
INT = _Scalar("integer", lambda v: type(v) is int)
NUM = _Scalar("number", lambda v: isinstance(v, int | float) and not isinstance(v, bool))
NULL = _Scalar("null", lambda v: v is None)
ANY = _Scalar("any JSON value", lambda v: True)


class Union(Shape):
    def __init__(self, *options: Shape):
        self.options = options

    def errors(self, value, where="$"):
        found = [option.errors(value, where) for option in self.options]
        if any(not errs for errs in found):
            return []
        return [f"{where}: expected {self!r}, got {_kind(value)}"] if all(
            isinstance(o, _Scalar | Enum) for o in self.options) else min(found, key=len)

    def __repr__(self):
        return " | ".join(repr(o) for o in self.options)


class Enum(Shape):
    def __init__(self, *values: Any):
        self.values = values

    def errors(self, value, where="$"):
        return [] if value in self.values and type(value) in {type(v) for v in self.values} else [
            f"{where}: expected one of {list(self.values)}, got {value!r}"]

    def __repr__(self):
        return f"one of {list(self.values)}"


class ListOf(Shape):
    def __init__(self, item: Shape):
        self.item = item

    def errors(self, value, where="$"):
        if not isinstance(value, list):
            return [f"{where}: expected a list, got {_kind(value)}"]
        return [e for i, row in enumerate(value) for e in self.item.errors(row, f"{where}[{i}]")]

    def __repr__(self):
        return f"list of {self.item!r}"


class MapOf(Shape):
    """An object whose keys are data (setting keys, module names), each value of one shape."""

    def __init__(self, value: Shape):
        self.value = value

    def errors(self, value, where="$"):
        if not isinstance(value, dict):
            return [f"{where}: expected an object, got {_kind(value)}"]
        return [e for key, item in value.items() for e in self.value.errors(item, f"{where}.{key}")]

    def __repr__(self):
        return f"object of {self.value!r}"


class Obj(Shape):
    def __init__(self, required: dict[str, Shape] | None = None, optional: dict[str, Shape] | None = None):
        self.required, self.optional = dict(required or {}), dict(optional or {})

    def errors(self, value, where="$"):
        if not isinstance(value, dict):
            return [f"{where}: expected an object, got {_kind(value)}"]
        out = [f"{where}.{key}: missing required field" for key in self.required if key not in value]
        for key, shape in (self.required | self.optional).items():
            if key in value:
                out += shape.errors(value[key], f"{where}.{key}")
        return out

    def fields(self) -> set[str]:
        return set(self.required) | set(self.optional)

    def extend(self, required: dict[str, Shape] | None = None, optional: dict[str, Shape] | None = None) -> Obj:
        return Obj(self.required | (required or {}), self.optional | (optional or {}))

    def __repr__(self):
        return "object"


def _kind(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return f"boolean {value}"
    if isinstance(value, int | float):
        return f"number {value!r}"
    if isinstance(value, str):
        return f"string {value[:40]!r}"
    return type(value).__name__ if not isinstance(value, list | dict) else ("a list" if isinstance(value, list)
                                                                           else "an object")
