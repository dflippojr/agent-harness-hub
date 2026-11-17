"""No router imports another router or the server module (#255).

A router that imports a sibling router, or `harness_hub.server`, couples it to a page it does not own. Shared logic
lives in plain modules beside the routers (`slots.py`, `restart.py`, `owners.py`), and timing constants live on
`HubContext`, so the routers depend only on those modules and on `routes.common`.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import harness_hub

ROUTES = Path(harness_hub.__file__).parent / "routes"
ROUTER_MODULES = sorted(path.stem for path in ROUTES.glob("*.py") if path.stem not in ("__init__", "common"))


def _imported_modules(path: Path) -> set[str]:
    """The module names a file imports, relative ones resolved against `harness_hub.routes` (`..server` is
    `server`, `.marketplace` is `marketplace`), plus each name imported from a package (`from .. import server`)."""
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), filename=str(path))):
        if isinstance(node, ast.ImportFrom):
            if node.level == 0:
                module = node.module or ""
                if module.startswith("harness_hub."):
                    found.add(module.removeprefix("harness_hub."))
                continue
            base = node.module.split(".")[0] if node.module else ""
            if node.level == 1 and base:
                found.add(base)
            elif node.level >= 2 and base:
                found.add(base)
            if node.level >= 1 and not base:
                # `from . import apps` or `from .. import server`: each name is a module of the package it names.
                found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            found.update(alias.name.removeprefix("harness_hub.") for alias in node.names)
    return found


@pytest.mark.parametrize("router", ROUTER_MODULES)
def test_router_imports_no_sibling_router_or_server(router):
    forbidden = {"server", *ROUTER_MODULES} - {router}
    imported = _imported_modules(ROUTES / f"{router}.py")
    assert not (imported & forbidden), f"routes/{router}.py imports {sorted(imported & forbidden)}"


def test_common_imports_no_router_or_server():
    imported = _imported_modules(ROUTES / "common.py")
    assert not (imported & {"server", *ROUTER_MODULES}), f"routes/common.py imports {sorted(imported)}"


def test_the_checker_sees_the_known_cross_router_imports():
    # Guards the checker itself: the sibling imports the issue lists are what it must catch.
    assert "marketplace" in _imported_modules(ROUTES / "overview.py")
    assert "settings" in _imported_modules(ROUTES / "overview.py")
    assert "server" in _imported_modules(ROUTES / "common.py")  # the lazy `from .. import server` in tuning()
