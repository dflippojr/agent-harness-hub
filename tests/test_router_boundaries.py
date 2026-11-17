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


def _imported_modules(source: str) -> set[str]:
    """The module names a source imports, relative ones resolved against `harness_hub.routes` (`..server` is
    `server`, `.marketplace` is `marketplace`), plus each name imported from a package (`from .. import server`)."""
    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom):
            if node.level:  # relative: `.marketplace` or `..server` name a module; `from .. import server` names one
                if node.module:
                    found.add(node.module.split(".")[0])
                else:
                    found.update(alias.name for alias in node.names)
            elif (node.module or "").startswith("harness_hub."):
                found.add(node.module.removeprefix("harness_hub.").split(".")[0])
        elif isinstance(node, ast.Import):
            found.update(alias.name.removeprefix("harness_hub.") for alias in node.names)
    return found


@pytest.mark.parametrize("router", ROUTER_MODULES)
def test_router_imports_no_sibling_router_or_server(router):
    forbidden = {"server", *ROUTER_MODULES} - {router}
    imported = _imported_modules((ROUTES / f"{router}.py").read_text(encoding="utf-8"))
    assert not (imported & forbidden), f"routes/{router}.py imports {sorted(imported & forbidden)}"


def test_common_imports_no_router_or_server():
    imported = _imported_modules((ROUTES / "common.py").read_text(encoding="utf-8"))
    assert not (imported & {"server", *ROUTER_MODULES}), f"routes/common.py imports {sorted(imported)}"


def test_the_checker_sees_the_imports_it_forbids():
    # Guards the checker itself: each form of a sibling or server import is reported, and `routes.common` is not.
    assert "marketplace" in _imported_modules("from ..marketplace import observe_browser_slots\n")
    assert "settings" in _imported_modules("from .settings import observe_recovery\n")
    assert "server" in _imported_modules("def tuning():\n    from .. import server\n")
    assert "server" in _imported_modules("from ..server import create_app\n")
    assert "common" in _imported_modules("from .common import HubContext\n")
    assert "restart" in _imported_modules("from ..restart import observe_restart\n")
