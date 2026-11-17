"""`[daemon]` check of every host command the Hub can print (the server's and the pages' commands.mjs table), against
the daemon's own CLI parser at the checkout HUB_E2E_DAEMON_SRC names (#251). An unknown subcommand, a wrong number of
positionals, an unknown or abbreviated flag or a placeholder in another argument's place fails, naming the command and
where it came from."""

from __future__ import annotations

import os
import shutil
import sys

import pytest

from .host_commands import hub_commands, misplaced, parse, server_commands
from .targets import daemon_source

pytestmark = pytest.mark.e2e

# The pages' commands are read through node; without it, only the server's are collected and
# test_node_reads_the_page_table fails, so a missing node never passes silently.
NODE = shutil.which("node")
COMMANDS = hub_commands() if NODE else server_commands()


def test_node_reads_the_page_table():
    assert NODE, "the host command check reads harness_hub/static/lib/commands.mjs through Node.js; put node on PATH"


@pytest.fixture(scope="module")
def verdicts():
    src = daemon_source()
    if src is None:
        pytest.skip("set HUB_E2E_DAEMON_SRC to an agent-harness checkout")
    python = os.environ.get("HUB_E2E_DAEMON_PYTHON", sys.executable)
    return dict(zip(COMMANDS, parse(src, python, [template for _, template in COMMANDS]), strict=True))


@pytest.mark.parametrize("command", COMMANDS, ids=[f"{source}: {template}" for source, template in COMMANDS])
def test_host_command_parses_with_the_daemon_cli(verdicts, command):
    source, template = command
    verdict = verdicts[command]
    assert verdict["error"] is None, f"{source} prints {template!r}, which the daemon's CLI refuses: {verdict['error']}"
    wrong = misplaced(verdict["positionals"])
    assert not wrong, f"{source} prints {template!r} with arguments out of place: {', '.join(wrong)}"
