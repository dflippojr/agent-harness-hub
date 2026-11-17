"""Parse host command lines with the daemon's own CLI parser, never running one (#251).

Run from an agent-harness checkout with that checkout on PYTHONPATH. Reads a JSON list of argv lists (the words after
`harness`) on stdin and writes one JSON object per argv: `error`, argparse's complaint or null, and `positionals`, the
value each positional argument of the chosen subcommand received. Abbreviated flags are refused, unlike the daemon's
own parser: a page must print the flag as the daemon names it.
"""

from __future__ import annotations

import argparse
import json
import sys

from harness import cli  # the checkout's harness package


class Refused(Exception):
    pass


def _refuse(self, message):
    raise Refused(f"{self.prog}: {message}")


def _subcommands(parser: argparse.ArgumentParser):
    return [action for action in parser._actions if isinstance(action, argparse._SubParsersAction)]


def _exact(parser: argparse.ArgumentParser) -> None:
    parser.allow_abbrev = False
    for action in _subcommands(parser):
        for child in action.choices.values():
            _exact(child)


def _leaf(parser: argparse.ArgumentParser, words: list[str]) -> argparse.ArgumentParser:
    for action in _subcommands(parser):
        for index, word in enumerate(words):
            if word in action.choices:
                return _leaf(action.choices[word], words[index + 1:])
    return parser


def main() -> int:
    argparse.ArgumentParser.error = _refuse
    parser = cli._build_parser()
    _exact(parser)
    results = []
    for argv in json.load(sys.stdin):
        try:
            parsed = parser.parse_args(argv)
        except Refused as exc:
            results.append({"error": str(exc), "positionals": {}})
            continue
        leaf = _leaf(parser, argv)
        positionals = {action.dest: getattr(parsed, action.dest, None) for action in leaf._actions
                       if not action.option_strings and not isinstance(action, argparse._SubParsersAction)}
        results.append({"error": None, "positionals": positionals})
    json.dump(results, sys.stdout, default=str)
    return 0


if __name__ == "__main__":
    sys.exit(main())
