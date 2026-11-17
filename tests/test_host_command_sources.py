"""Every host command the Hub prints comes from a place the `[daemon]` parse check reads (#251,
tests/contract/test_host_commands.py): the pages' from the one table in harness_hub/static/lib/commands.mjs, the
server's from tests/contract/host_commands.py's sources. No other static file, and no other string in harness_hub,
writes a `harness` command line of its own."""

from __future__ import annotations

import ast
import re
from pathlib import Path

from .contract.host_commands import TABLE, page_commands, server_commands

PACKAGE = Path(__file__).resolve().parents[1] / "harness_hub"
COMMAND = re.compile(r"(?<![\w-])harness [a-z][\w-]*")
STATIC = PACKAGE / "static"
# Prose about the harness, not a command: (file, the two words).
STATIC_PROSE = {("pages/admin.mjs", "harness through")}
PROSE = {
    ("views.py", "harness and"),
    ("views.py", "harness queue"),
    ("views.py", "harness approvals"),
}


def _docstrings(tree: ast.AST) -> set[int]:
    nodes = (tree, *(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)))
    return {id(node.body[0].value) for node in nodes
            if node.body and isinstance(node.body[0], ast.Expr) and isinstance(node.body[0].value, ast.Constant)}


def python_command_literals() -> list[tuple[str, str]]:
    """(file, string) for each string constant in harness_hub that reads like a `harness` command, docstrings aside."""
    found = []
    for path in sorted(PACKAGE.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        skip = _docstrings(tree)
        for node in ast.walk(tree):
            if (isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in skip
                    and COMMAND.search(node.value)):
                found.append((path.relative_to(PACKAGE).as_posix(), node.value))
    return found


def test_every_server_command_literal_is_one_the_parse_check_reads():
    templates = {template for _, template in server_commands()}
    stray = [(name, text) for name, text in python_command_literals() if text not in templates
             and any((name, phrase) not in PROSE for phrase in COMMAND.findall(text))]
    assert not stray, ("harness_hub strings that print a host command the [daemon] parse check does not read; use "
                       f"a command from tests/contract/host_commands.py's sources instead: {stray}")


def test_the_scan_sees_the_server_commands_and_flags_a_stray_one():
    literals = {text for _, text in python_command_literals()}
    assert {template for _, template in server_commands()} <= literals | {""}
    assert COMMAND.search("On the daemon host run harness hub release --confirm, then start")
    assert not COMMAND.search("agent-harness aa5a904") and not COMMAND.search("harness_hub")


def static_command_literals(skip: Path = TABLE) -> list[tuple[str, int, str]]:
    """(file, line, `harness <word>`) for each command-like text in harness_hub/static outside the table."""
    found = []
    for path in sorted(STATIC.rglob("*")):
        if path.is_file() and path != skip and path.suffix in (".mjs", ".js", ".html", ".css"):
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                found += [(path.relative_to(STATIC).as_posix(), number, phrase) for phrase in COMMAND.findall(line)]
    return found


def table_names() -> list[str]:
    return re.findall(r'^  (\w+): "harness ', TABLE.read_text(encoding="utf-8"), re.MULTILINE)


def test_no_static_file_but_the_table_writes_a_host_command():
    stray = [row for row in static_command_literals() if (row[0], row[2]) not in STATIC_PROSE]
    assert not stray, ("host commands written outside harness_hub/static/lib/commands.mjs; add them to its COMMANDS "
                       f"and print them with hostCommand(): {stray}")


def test_the_static_scan_reads_the_table_when_it_is_not_skipped():
    names = {phrase for name, _, phrase in static_command_literals(skip=STATIC / "none") if name == "lib/commands.mjs"}
    assert {"harness hub", "harness pairing-requests", "harness config"} <= names


def test_every_table_command_is_printed_by_a_page():
    pages = "".join(path.read_text(encoding="utf-8") for path in STATIC.rglob("*.mjs") if path != TABLE)
    unused = [name for name in table_names() if f'"{name}"' not in pages]
    assert table_names() and not unused, f"commands.mjs entries no page prints: {unused}"


def test_the_parse_check_reads_every_table_command_with_and_without_each_option():
    sources = [source for source, _ in page_commands()]
    prefix = "harness_hub/static/lib/commands.mjs COMMANDS."
    assert {source.removeprefix(prefix).split(" ")[0] for source in sources} == set(table_names())
    arm = [line for source, line in page_commands() if source.startswith(prefix + "pairingArm")]
    assert len(arm) == 4 and all("[" not in line for line in arm)
    assert ("harness pairing-requests arm <catalog_app_id> --scopes <scopes> --origin <origin> "
            "--acknowledge-elevated") in arm
