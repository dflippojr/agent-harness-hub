"""Static rules over the source: what the browser code may do, which daemon routes the server may call."""

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
        if path.name != "app.mjs":
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
    tree = ast.parse(daemon)
    assert daemon.count("/token") == 1
    assert re.search(r'"kind": "hub"', daemon)
    assert "/claim" not in daemon  # the App-side claim of an armed slot
    # The one token route is inside the Hub's own claim redemption...
    redeemers = [node.name for node in ast.walk(tree) if isinstance(node, ast.AsyncFunctionDef)
                 and "/token" in ast.unparse(node)]
    assert redeemers == ["redeem_hub_claim"]
    # ...which only claim.py calls, with the request id of the Hub's own claim; no browser route reaches it.
    for path in PYTHON:
        if path.name not in ("daemon.py", "claim.py"):
            assert "redeem_hub_claim" not in text(path), path
    claim = text(PACKAGE / "claim.py")
    assert claim.count("redeem_hub_claim(") == 1 and 'claim["request_id"]' in claim
    assert 'app.get("role") != "hub"' in claim
    # The owner API's App pairing routes are owner decisions and arming a slot, never the App-side claim or token.
    routes = next(node for node in tree.body if isinstance(node, ast.Assign)
                  and getattr(node.targets[0], "id", "") == "PAIRING_ROUTES")
    paths = [value.elts[1].value for value in routes.value.values]
    assert paths == ["/pairing-requests", "/pairing-requests", "/pairing-requests/{rid}/approve",
                     "/pairing-requests/{rid}/confirm", "/pairing-requests/{rid}/deny"]
    assert "CLAIM_REQUESTS" not in ast.unparse(next(node for node in ast.walk(tree)
                                                    if isinstance(node, ast.AsyncFunctionDef)
                                                    and node.name == "pairing"))
    # And no Python module or browser file names the App-side routes at all.
    for path in PYTHON:
        if path.name != "daemon.py":
            assert not any("/pair/requests" in value for value in strings(path)), path
    for path in WEB:
        assert "/pair/requests" not in text(path), path


def test_colours_come_from_tokens():
    css = text(STATIC / "style.css")
    body = css[css.index("* { box-sizing"):]
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b|rgba?\(|hsla?\(", body)


def test_phone_bottom_bar():
    """Hub design 13 (#80): below 900 px a bottom bar (Overview, Pairing, Apps, More) replaces the Sections drawer, and
    More opens the rest of the rail as a sheet; the bar keeps clear of the home indicator."""
    html = text(STATIC / "index.html")
    assert '<nav id="tabbar" class="tabbar" aria-label="Sections" hidden></nav>' in html
    assert html.index('id="tabbar"') < html.index('<nav id="nav"') < html.index('<main id="main"')
    app = text(STATIC / "app.mjs")
    bar = app[app.index("const BAR = ["):app.index("];", app.index("const BAR = ["))]
    assert re.findall(r'id: "(\w+)"', bar) == ["overview", "pairing", "apps"]
    assert '"aria-controls": "nav"' in app and '"More"' in app
    css = text(STATIC / "style.css")
    assert "@media (max-width: 899.98px)" in css and 'matchMedia("(max-width: 899.98px)")' in app
    assert "--tabbar-h: calc(50px + max(6px, env(safe-area-inset-bottom)));" in css
    assert ".rail-brand, .nav .on-bar { display: none; }" in css


def test_every_module_is_preloaded_and_exists():
    html = text(STATIC / "index.html")
    for href in re.findall(r'href="/static/([^"]+\.mjs)"', html) + re.findall(r'src="/static/([^"]+)"', html):
        assert (STATIC / href).is_file(), href
    for module in STATIC.rglob("*.mjs"):
        if module.name != "app.mjs":
            assert f"/static/{module.relative_to(STATIC).as_posix()}" in html, module
