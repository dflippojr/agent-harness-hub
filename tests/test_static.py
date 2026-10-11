"""Static rules over the source: what the browser code may do, which daemon routes the server may call, 44px targets."""

from __future__ import annotations

import ast
import re
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1] / "harness_hub"
STATIC = PACKAGE / "static"
WEB = sorted(p for p in STATIC.rglob("*") if p.suffix in (".mjs", ".js", ".html", ".css", ".svg"))
PYTHON = sorted(PACKAGE.glob("*.py"))
TOKEN_SHAPE = re.compile(r"(?<![A-Za-z0-9])(?:ha|ho|hp)-[A-Za-z0-9_\-]{4,}")


def text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_browser_code_talks_only_to_the_hub():
    for path in WEB:
        source = text(path)
        assert "/api/v1" not in source and "/api/admin" not in source, path
        assert "Authorization" not in source and "Bearer" not in source, path
        assert "verifier" not in source.lower(), path
        assert not TOKEN_SHAPE.search(source), path


def test_browser_code_never_stores_or_injects():
    for path in WEB:
        source = text(path)
        assert "localStorage" not in source, path
        if path.name != "api.mjs":
            assert "sessionStorage" not in source, path
        assert "innerHTML" not in source and "outerHTML" not in source and "insertAdjacentHTML" not in source, path
        assert "eval(" not in source and "new Function" not in source, path


def strings(path: Path) -> list[str]:
    """The string literals in a module, docstrings excluded."""
    tree = ast.parse(text(path))
    docstrings = {id(node.body[0].value) for node in ast.walk(tree)
                  if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
                  and node.body and isinstance(node.body[0], ast.Expr)}
    return [node.value for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings]


def test_only_the_daemon_client_names_daemon_routes():
    for path in PYTHON:
        if path.name != "daemon.py":
            for value in strings(path):
                assert "/api/v1" not in value and "/api/admin" not in value, (path, value)


def test_the_hub_never_claims_or_redeems_an_app_request():
    """App tokens go from the daemon straight to the App (agent-harness #519): the Hub only redeems its own claim."""
    daemon = text(PACKAGE / "daemon.py")
    assert daemon.count("/token") == 1
    assert re.search(r'"kind": "hub"', daemon)
    assert "/claim" not in daemon  # the App-side claim of an armed slot
    assert "pairing-requests" not in daemon  # the owner API's App requests come with the Pairing section
    claim = text(PACKAGE / "claim.py")
    assert 'app.get("role") != "hub"' in claim


def test_tap_targets_are_at_least_44px():
    css = text(STATIC / "style.css")
    assert re.search(r"--tap:\s*44px", css)
    for selector in (".btn {", ".nav-link {", ".brand {", ".command {", "details summary {"):
        block = css[css.index(selector):]
        block = block[:block.index("}")]
        assert "min-height: var(--tap)" in block, selector


def test_colours_come_from_tokens():
    css = text(STATIC / "style.css")
    body = css[css.index("* { box-sizing"):]
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b|rgba?\(|hsla?\(", body)


def test_phone_and_desktop_layouts():
    css = text(STATIC / "style.css")
    html = text(STATIC / "index.html")
    assert 'name="viewport" content="width=device-width' in html
    assert "@media (min-width: 900px)" in css
    assert "body.menu-open .nav" in css


def test_every_module_is_preloaded_and_exists():
    html = text(STATIC / "index.html")
    for href in re.findall(r'href="/static/([^"]+\.mjs)"', html) + re.findall(r'src="/static/([^"]+)"', html):
        assert (STATIC / href).is_file(), href
    for module in STATIC.rglob("*.mjs"):
        if module.name != "app.mjs":
            assert f"/static/{module.relative_to(STATIC).as_posix()}" in html, module
