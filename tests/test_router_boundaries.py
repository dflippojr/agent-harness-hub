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


def _imported_modules(source: str, package: tuple[str, ...] = ("routes",)) -> set[tuple[str, ...]]:
    """Every module the source imports, as a path under `harness_hub` (`("routes", "settings")`, `("server",)`).

    Relative imports resolve against `package` (the file's own package under `harness_hub`); absolute ones drop the
    `harness_hub` prefix. `from X import name` also yields `X + (name,)`, since `name` may be a module."""
    found: set[tuple[str, ...]] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom):
            if node.level:
                base = package[: len(package) - (node.level - 1)]
            else:
                module = node.module or ""
                if module != "harness_hub" and not module.startswith("harness_hub."):
                    continue
                base = ()
            prefix = base + tuple(node.module.split(".")) if node.module else base
            if node.module and not node.level:
                prefix = tuple(node.module.split("."))[1:]
            found.add(prefix)
            found.update(prefix + (alias.name,) for alias in node.names)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "harness_hub" or alias.name.startswith("harness_hub."):
                    found.add(tuple(alias.name.split("."))[1:])
    return found


def _forbidden(path: tuple[str, ...], router: str) -> bool:
    """A router must not reach a sibling router's module, or the server, in any import form."""
    if path[:1] == ("server",):
        return True
    return len(path) >= 2 and path[0] == "routes" and path[1] in set(ROUTER_MODULES) - {router}


ROUTE_FILES = sorted(path.stem for path in ROUTES.glob("*.py"))  # every file, the package's __init__ included


@pytest.mark.parametrize("module", ROUTE_FILES)
def test_no_routes_module_imports_a_sibling_router_or_the_server(module):
    imported = _imported_modules((ROUTES / f"{module}.py").read_text(encoding="utf-8"))
    bad = sorted(".".join(path) for path in imported if _forbidden(path, module))
    assert not bad, f"routes/{module}.py imports {bad}"


@pytest.mark.parametrize("source", [
    "from .marketplace import observe_browser_slots\n",
    "from .settings import observe_recovery\n",
    "from . import settings\n",
    "from harness_hub.routes.settings import settings_health\n",
    "from harness_hub.routes import marketplace\n",
    "import harness_hub.routes.marketplace\n",
    "from harness_hub import server\n",
    "from harness_hub.server import create_app\n",
    "from .. import server\n",
    "from ..server import create_app\n",
    "def tuning():\n    from .. import server\n",
])
def test_the_checker_catches_every_import_form_of_a_forbidden_module(source):
    assert any(_forbidden(path, "overview") for path in _imported_modules(source))


@pytest.mark.parametrize("source", [
    "from .common import HubContext\n",
    "from ..restart import observe_restart\n",
    "from ..views import apps_view\n",
    "from harness_hub.routes.common import error\n",
    "from harness_hub import slots\n",
])
def test_the_checker_allows_shared_modules(source):
    assert not any(_forbidden(path, "overview") for path in _imported_modules(source))
