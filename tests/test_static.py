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


def test_tap_targets_are_at_least_44px():
    css = text(STATIC / "style.css")
    assert re.search(r"--tap:\s*44px", css)
    for selector in (".btn {", ".nav-link {", ".tab {", ".brand {", ".command {", "details summary {"):
        block = css[css.index(selector):]
        block = block[:block.index("}")]
        assert "min-height: var(--tap)" in block, selector


def test_colours_come_from_tokens():
    css = text(STATIC / "style.css")
    body = css[css.index("* { box-sizing"):]
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b|rgba?\(|hsla?\(", body)


def test_local_storage_holds_only_the_host_commands_switch():
    """Decision 3 of #65: the Show host commands switch is remembered per browser, and nothing else is."""
    app = text(STATIC / "app.mjs")
    assert re.findall(r"localStorage\.(\w+)\(([^,)]*)", app) == [
        ("getItem", "HOST_COMMANDS_KEY"), ("setItem", "HOST_COMMANDS_KEY"), ("removeItem", "HOST_COMMANDS_KEY")]
    assert 'const HOST_COMMANDS_KEY = "hub.hostCommands";' in app
    assert 'setItem(HOST_COMMANDS_KEY, "on")' in app


def test_rail_groups_and_footer():
    """Hub design 2 (#69): Overview, then Access, then Daemon; the footer holds the chip, the switch and Sign out."""
    app = text(STATIC / "app.mjs")
    order = [app.index(f'id: "{section}"') for section in
             ("overview", "pairing", "apps", "marketplace", "settings", "modules", "admin")]
    assert order == sorted(order)
    marks = ('label: "Access"', 'id: "pairing"', 'label: "Daemon"', 'id: "settings"')
    assert [app.index(mark) for mark in marks] == sorted(app.index(mark) for mark in marks)
    html = text(STATIC / "index.html")
    start = html.index('<nav id="nav"')
    nav = html[start:html.index("</nav>", start)]
    for marker in ('id="daemon-chip"', 'role="status"', 'id="host-commands"', 'role="switch"', 'id="signout-button"'):
        assert marker in nav, marker
    assert '<label id="host-commands-row" class="switch host-commands">' in nav
    assert "topbar" not in html and "menu-button" not in html


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


def custom_properties(block: str) -> dict[str, str]:
    return dict(re.findall(r"(--[a-z0-9-]+):\s*([^;]+);", block))


def test_stables_tokens_in_light_and_dark():
    """Hub design 1 (#68): the Stables tokens the Hub lacked, and the dark primary #2F66D8 with white text."""
    css = text(STATIC / "style.css")
    light = custom_properties(css[css.index(":root {"):css.index("@media (prefers-color-scheme: dark)")])
    dark = custom_properties(css[css.index("@media (prefers-color-scheme: dark)"):css.index("* { box-sizing")])
    for name in ("--primary", "--on-primary", "--info-bg", "--ok-solid", "--shadow"):
        assert name in light and name in dark, name
    assert light["--primary"] == "#2457d9" and light["--on-primary"] == "#ffffff"
    assert dark["--primary"] == "#2f66d8" and dark["--on-primary"] == "#ffffff"
    assert ".btn.primary { background: var(--primary)" in css


def test_component_styles_exist():
    css = text(STATIC / "style.css")
    for selector in (".pill.run", ".pill.ok", ".pill.warn", ".pill.bad", ".pill.muted", ".section-head {",
                     ".count.warn", ".banner.bad", ".banner.warn", ".banner.info", ".switch {", ".table {",
                     ".countdown {", ".countdown.warn", ".danger-zone {", ".btn.danger"):
        assert selector in css, selector
    switch = css[css.index(".switch {"):]
    assert "min-height: var(--tap)" in switch[:switch.index("}")]


def test_host_commands_follow_the_switch():
    """Hub design 3 (#70): a CLI-parity command waits behind its Host command button, and Show host commands shows
    every one; a step only the daemon host can take (claiming, signing in) is never hidden."""
    css = text(STATIC / "style.css")
    assert ".host-command-lines { display: none; }" in css
    shown = ".host-command.open .host-command-lines, body.show-host-commands .host-command-lines { display: block; }"
    assert shown in css
    assert "body.show-host-commands .host-command-toggle { display: none; }" in css
    assert "body.classList.toggle(\"show-host-commands\", on)" in text(STATIC / "app.mjs")
    for page in ("admin", "apps", "marketplace", "modules", "pairing", "settings"):
        assert "{ parity: true" in text(STATIC / "pages" / f"{page}.mjs"), page
    for path in (STATIC / "pages" / "overview.mjs", STATIC / "pages" / "signin.mjs", STATIC / "lib" / "pairing.mjs"):
        assert "command(" in text(path) and "parity" not in text(path), path
