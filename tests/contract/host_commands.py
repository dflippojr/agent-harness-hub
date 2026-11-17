"""Every `harness` command line the Hub can print, where it comes from, and how to check one against the daemon's
own CLI parser (#251).

A command is a template: `<name>` and `{name}` stand for one word the owner fills in. To parse one, each placeholder
becomes exactly one synthetic word (so a missing or extra argument still counts), and the daemon's parser at the
checkout reads the words in a subprocess (parse_host_commands.py). No command is run against a daemon.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import subprocess
from pathlib import Path

from harness_hub import claim
from harness_hub.administration import OPERATIONS
from harness_hub.routes.settings import administration_catalog

PARSER = Path(__file__).with_name("parse_host_commands.py")
PLACEHOLDER = re.compile(r"<([A-Za-z_][\w.-]*)>|\{([A-Za-z_]\w*)\}")
# A placeholder that fills a typed argument gets a value of that type; the others stay as `<name>`, so a parsed
# positional shows which placeholder filled it.
TYPED = {"integer": "1", "days": "30", "enabled": "true"}
# A placeholder may be named for what the owner reads off the page rather than the daemon's argument name.
ALIASES = {
    "id": {"rid", "app_id"},
    "key_id": {"kid"},
    "key": {"changes"},
    "value": {"changes"},
    "project": {"projects"},
    "marker": {"confirmed_markers"},
}


def server_commands() -> list[tuple[str, str]]:
    """(source, template) for each command the Hub's server writes: the administration catalogue, the claim
    commands and the catalogue's not-offered rows."""
    rows = [(f"harness_hub/administration.py OPERATIONS[{name!r}]", spec.cli) for name, spec in OPERATIONS.items()]
    rows += [("harness_hub/claim.py APPROVE_COMMAND", claim.APPROVE_COMMAND),
             ("harness_hub/claim.py RELEASE_COMMAND", claim.RELEASE_COMMAND)]
    not_offered = asyncio.run(administration_catalog())["not_offered"]
    rows += [(f"harness_hub/routes/settings.py not_offered[{row['label']!r}]", row["cli"]) for row in not_offered]
    return rows


def hub_commands() -> list[tuple[str, str]]:
    return server_commands()


def argv_for(template: str) -> list[str]:
    """The words after `harness`, each placeholder replaced by one word."""
    words = shlex.split(template)
    assert words and words[0] == "harness", f"not a harness command: {template!r}"

    def fill(match: re.Match) -> str:
        name = match.group(1) or match.group(2)
        return TYPED.get(name, f"<{name}>")

    return [PLACEHOLDER.sub(fill, word) for word in words[1:]]


def _strings(value) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [text for item in value for text in _strings(item)]
    return []


def misplaced(positionals: dict) -> list[str]:
    """Placeholders that filled a positional argument named for something else: a swapped positional."""
    wrong = []
    for dest, value in positionals.items():
        name = dest.removeprefix("p_").removeprefix("f_")
        for text in _strings(value):
            for match in PLACEHOLDER.finditer(text):
                placeholder = match.group(1) or match.group(2)
                if placeholder != name and name not in ALIASES.get(placeholder, ()):
                    wrong.append(f"<{placeholder}> fills {name.upper()}")
    return wrong


def parse(src: Path, python: str, templates: list[str]) -> list[dict]:
    """The daemon checkout's parser's verdict on each template, in order."""
    env = dict(os.environ, PYTHONPATH=str(src))
    result = subprocess.run([python, str(PARSER)], cwd=src, env=env, input=json.dumps([argv_for(t) for t in templates]),
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, f"the daemon's parser did not load: {result.stderr[-2000:]}"
    return json.loads(result.stdout)
