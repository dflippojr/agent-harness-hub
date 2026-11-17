"""Every host command the Hub prints comes from a place the `[daemon]` parse check reads (#251,
tests/contract/test_host_commands.py): the server's commands from tests/contract/host_commands.py's sources, so no
string elsewhere in harness_hub writes a `harness` command line of its own."""

from __future__ import annotations

import ast
import re
from pathlib import Path

from .contract.host_commands import server_commands

PACKAGE = Path(__file__).resolve().parents[1] / "harness_hub"
COMMAND = re.compile(r"(?<![\w-])harness [a-z][\w-]*")
# Prose about the harness, not a command: (file, the two words).
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
