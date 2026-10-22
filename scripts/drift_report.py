"""The nightly daemon drift report (.github/workflows/drift.yml): one issue that says what the run found.

    python scripts/drift_report.py pinned
    python scripts/drift_report.py pin-age --mode warn|fail [--daemon-sha SHA]
    python scripts/drift_report.py report --daemon-sha SHA --results DIR --outcome daemon=success --outcome ui=success
                                          [--run-url URL] [--dry-run]

`pinned` prints `sha=<the agent-harness commit ci.yml pins>` for $GITHUB_OUTPUT. `report` reads the JUnit XML in DIR
and the job outcomes, then, with the `gh` CLI and the workflow's GITHUB_TOKEN:

- green (every job succeeded): opens or refreshes one issue, "Bump agent-harness pin to <sha>", labelled ready,
  tier:mechanical and verification-uplift, and closes an open drift bug, since main is green again;
- red: opens or refreshes one `bug` issue naming the failing cases and the agent-harness commits since the last green
  (the newest bump issue's sha, else the pin);
- in both, when the pin is older than PIN_MAX_DAYS (AGENTS.md) and main has moved past it, comments on that issue once
  per pinned commit.

--dry-run writes nothing to GitHub: it prints each action (the pull request check of this workflow uses it).

`pin-age` is AGENTS.md's pin rule as a check: when the pin is more than PIN_MAX_DAYS old and agent-harness main
(--daemon-sha, else main's HEAD read from the API) has commits after it, it says so, naming the open bump issue.
`--mode warn` (pull requests) prints a ::warning:: and exits 0, even when GitHub can't be read; `--mode fail` (pushes
to main and the nightly run) prints an ::error:: and exits 1. It reads only public data, with the default token.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DAEMON_REPO = "dflippojr/agent-harness"
PIN_MAX_DAYS = 14
BUMP_PREFIX = "Bump agent-harness pin to "
DRIFT_PREFIX = "Daemon drift: agent-harness main "
BUMP_LABELS = ("ready", "tier:mechanical", "verification-uplift")
DRIFT_LABELS = ("bug",)
SHA = re.compile(r"[0-9a-f]{40}")


def pinned_sha(ci: str) -> str:
    """The one agent-harness commit ci.yml checks out (every daemon checkout must name the same one)."""
    refs = set(re.findall(r"repository: dflippojr/agent-harness\s+ref: ([0-9a-f]{40})", ci))
    if len(refs) != 1:
        raise SystemExit(f"ci.yml must pin exactly one agent-harness commit, found {sorted(refs)}")
    return refs.pop()


def failures(results: Path) -> dict[str, list[str]]:
    """Failing test names per JUnit file stem (contract-ubuntu-latest, e2e-windows-latest, ui, ...)."""
    out: dict[str, list[str]] = {}
    for path in sorted(results.glob("**/*.xml")):
        try:
            tree = ET.parse(path)
        except ET.ParseError:
            out.setdefault(path.stem, []).append(f"(unreadable results file {path.name})")
            continue
        for case in tree.iter("testcase"):
            if case.find("failure") is not None or case.find("error") is not None:
                name = case.get("name", "?")
                where = case.get("classname", "")
                out.setdefault(path.stem, []).append(f"{where}::{name}" if where else name)
    return out


@dataclass
class Issue:
    number: int
    title: str
    state: str = "OPEN"


class GitHub:
    """The few `gh` calls the report makes. `dry_run` prints the writes instead of sending them."""

    def __init__(self, repo: str, dry_run: bool, run: Callable[..., str] | None = None):
        self.repo, self.dry_run = repo, dry_run
        self._run = run or self._gh
        self.writes: list[str] = []

    @staticmethod
    def _gh(args: list[str], stdin: str | None = None) -> str:
        return subprocess.run(["gh", *args], input=stdin, capture_output=True, text=True, check=True).stdout

    def read(self, *args: str) -> str:
        return self._run([*args, "-R", self.repo] if args[0] == "issue" else list(args))

    def write(self, args: list[str], body: str | None = None) -> None:
        self.writes.append(" ".join(args[:3]))
        if self.dry_run:
            print(f"[dry run] gh {' '.join(args)}" + (f"\n{body}\n" if body else ""), flush=True)
            return
        self._run([*args, "-R", self.repo, *(["--body-file", "-"] if body is not None else [])], body)

    def issues(self, prefix: str, state: str = "open") -> list[Issue]:
        found = json.loads(self.read("issue", "list", "--state", state, "--limit", "50", "--search",
                                     f'"{prefix.strip()}" in:title', "--json", "number,title,state"))
        rows = [Issue(row["number"], row["title"], row["state"]) for row in found if row["title"].startswith(prefix)]
        return sorted(rows, key=lambda row: row.number, reverse=True)

    def commented(self, number: int, marker: str) -> bool:
        comments = json.loads(self.read("issue", "view", str(number), "--json", "comments"))["comments"]
        return any(marker in (comment.get("body") or "") for comment in comments)

    def commit(self, sha: str) -> dict:
        return json.loads(self.read("api", f"repos/{DAEMON_REPO}/commits/{sha}"))

    def compare(self, base: str, head: str) -> dict:
        return json.loads(self.read("api", f"repos/{DAEMON_REPO}/compare/{base}...{head}"))

    def main_sha(self) -> str:
        return self.commit("main")["sha"]


def short(sha: str) -> str:
    return sha[:7]


def compare_url(base: str, head: str) -> str:
    return f"https://github.com/{DAEMON_REPO}/compare/{base}...{head}"


def pin_age(gh: GitHub, pinned: str, head: str) -> tuple[float, int]:
    """Days since the pinned commit, and how many commits main has that the pin lacks (0: the pin is main)."""
    ahead = gh.compare(pinned, head).get("ahead_by", 0) if pinned != head else 0
    stamp = gh.commit(pinned)["commit"]["committer"]["date"]
    then = datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
    return (time.time() - then) / 86400, ahead


def bump_body(head: str, pinned: str, ahead: int, run_url: str) -> str:
    return (f"The contract suite, the e2e tests and the real-daemon UI journeys all pass against agent-harness "
            f"main at `{head}` ([nightly run]({run_url})).\n\n"
            f"Main is {ahead} commit(s) past the pin `{short(pinned)}`: [{short(pinned)}...{short(head)}]"
            f"({compare_url(pinned, head)}).\n\n"
            "To bump: replace the pinned `ref:` in `.github/workflows/ci.yml` (the e2e and ui jobs) with this sha, "
            "update the matching assertion in `tests/test_supply_chain.py`, and refresh `requirements-daemon.in` "
            "and `requirements-e2e.lock` if the daemon's `requirements.txt` changed. The PR's CI is the proof.\n\n"
            "<!-- opened by .github/workflows/drift.yml -->")


def drift_body(head: str, last_green: str, failed: dict[str, list[str]], outcomes: dict[str, str],
               run_url: str) -> str:
    lines = [f"agent-harness main at `{head}` fails the Hub's daemon tests ([nightly run]({run_url})).", "",
             "| Job | Result |", "| --- | --- |"]
    lines += [f"| {job} | {result} |" for job, result in sorted(outcomes.items())]
    lines += ["", "**Failing cases**", ""]
    if failed:
        for source, names in failed.items():
            lines += [f"- `{source}`"] + [f"  - `{name}`" for name in names[:40]]
            if len(names) > 40:
                lines.append(f"  - ... and {len(names) - 40} more")
    else:
        lines.append("- No test failed: a job failed before or around its tests. Read the run's logs.")
    lines += ["", f"**Commits since the last green** (`{short(last_green)}`): "
                  f"[{short(last_green)}...{short(head)}]({compare_url(last_green, head)})", "",
              "Decide per case whether the daemon broke the Hub (fix the Hub, or report it on agent-harness) or the "
              "contract is out of date (update `tests/contract/endpoints.py` and the fakes to the daemon's answer).",
              "", "<!-- opened by .github/workflows/drift.yml -->"]
    return "\n".join(lines)


def report(gh: GitHub, head: str, pinned: str, outcomes: dict[str, str], failed: dict[str, list[str]],
           run_url: str) -> int:
    green = bool(outcomes) and all(result == "success" for result in outcomes.values())
    age, ahead = pin_age(gh, pinned, head)
    if green:
        title = BUMP_PREFIX + head
        body = bump_body(head, pinned, ahead, run_url)
        target = None
        if ahead:
            target = next(iter(gh.issues(BUMP_PREFIX)), None)
            if target:
                gh.write(["issue", "edit", str(target.number), "--title", title], body)
                gh.write(["issue", "comment", str(target.number)], f"Refreshed: still green at `{head}` ({run_url}).")
            else:
                gh.write(["issue", "create", "--title", title, *[f"--label={label}" for label in BUMP_LABELS]], body)
        for drift in gh.issues(DRIFT_PREFIX):
            gh.write(["issue", "comment", str(drift.number)],
                     f"agent-harness main is green again at `{head}` ({run_url}). Closing.")
            gh.write(["issue", "close", str(drift.number), "--reason", "completed"])
    else:
        bumps = gh.issues(BUMP_PREFIX, state="all")
        last_green = next((row.title.removeprefix(BUMP_PREFIX) for row in bumps
                           if SHA.fullmatch(row.title.removeprefix(BUMP_PREFIX))), pinned)
        title = f"{DRIFT_PREFIX}{short(head)} fails the Hub's daemon tests"
        body = drift_body(head, last_green, failed, outcomes, run_url)
        target = next(iter(gh.issues(DRIFT_PREFIX)), None)
        if target:
            gh.write(["issue", "edit", str(target.number), "--title", title], body)
            gh.write(["issue", "comment", str(target.number)], f"Refreshed: still failing at `{head}` ({run_url}).")
        else:
            gh.write(["issue", "create", "--title", title, *[f"--label={label}" for label in DRIFT_LABELS]], body)
    if ahead and age > PIN_MAX_DAYS:
        target = target or next(iter(gh.issues(DRIFT_PREFIX if not green else BUMP_PREFIX)), None)
        marker = f"<!-- pin-age:{pinned} -->"
        if target and not gh.commented(target.number, marker):
            gh.write(["issue", "comment", str(target.number)],
                     f"The agent-harness pin `{short(pinned)}` is {age:.0f} days old, past the {PIN_MAX_DAYS}-day "
                     f"limit in AGENTS.md, and main is {ahead} commit(s) ahead. Bump it.\n\n{marker}")
    print(f"{'green' if green else 'red'} at {head}; pin {short(pinned)} is {age:.1f} days old, {ahead} behind")
    return 0


def check_pin(gh: GitHub, pinned: str, head: str, fail: bool) -> int:
    """0 unless the pin breaks the rule and `fail` is set; a broken rule is a ::warning:: or an ::error:: annotation."""
    age, ahead = pin_age(gh, pinned, head)
    days = int(age)
    if not ahead or days <= PIN_MAX_DAYS:
        print(f"The agent-harness pin {short(pinned)} is {days} days old and {ahead} commit(s) behind main: within "
              f"the {PIN_MAX_DAYS}-day limit in AGENTS.md.")
        return 0
    bump = next(iter(gh.issues(BUMP_PREFIX)), None)
    message = (f"The agent-harness pin {short(pinned)} breaks the {PIN_MAX_DAYS}-day limit in AGENTS.md: pin is "
               f"{days} days behind agent-harness main ({ahead} commits). "
               + (f"Bump it: #{bump.number} {bump.title}." if bump else
                  f"Bump it to {head}; no \"{BUMP_PREFIX}<sha>\" issue is open (drift.yml opens one when main "
                  "passes the daemon tests)."))
    print(f"::{'error' if fail else 'warning'}::{message}", flush=True)
    return 1 if fail else 0


def pin_age_check(gh: GitHub, pinned: str, head: str | None, fail: bool) -> int:
    """check_pin against main's HEAD. GitHub being unreadable fails the check only in fail mode."""
    try:
        return check_pin(gh, pinned, head or gh.main_sha(), fail)
    except (subprocess.CalledProcessError, OSError, ValueError, KeyError) as exc:
        detail = (getattr(exc, "stderr", None) or str(exc)).strip().replace("\n", " ")
        print(f"::{'error' if fail else 'warning'}::Could not read the agent-harness pin's age: {detail}", flush=True)
        return 1 if fail else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python scripts/drift_report.py", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="command", required=True)
    sub.add_parser("pinned", help="print sha=<pinned agent-harness commit> for $GITHUB_OUTPUT")
    a = sub.add_parser("pin-age", help="warn or fail when the pin is past the limit in AGENTS.md")
    a.add_argument("--mode", choices=("warn", "fail"), required=True,
                   help="warn: pull requests, always exit 0; fail: main and the nightly run, exit 1")
    a.add_argument("--daemon-sha", help="agent-harness main's HEAD (default: read from the API)")
    r = sub.add_parser("report", help="open or refresh the drift issue")
    r.add_argument("--daemon-sha", required=True)
    r.add_argument("--results", type=Path, required=True, help="directory of JUnit XML files")
    r.add_argument("--outcome", action="append", default=[], help="job=result, as needs.<job>.result reports it")
    r.add_argument("--run-url", default="")
    r.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    pinned = pinned_sha((ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))
    if args.command == "pinned":
        print(f"sha={pinned}")
        return 0
    gh = GitHub(os.environ.get("GITHUB_REPOSITORY", "dflippojr/agent-harness-hub"), getattr(args, "dry_run", False))
    if args.command == "pin-age":
        return pin_age_check(gh, pinned, args.daemon_sha, args.mode == "fail")
    if not SHA.fullmatch(args.daemon_sha):
        ap.error("--daemon-sha must be a full commit sha")
    outcomes = dict(item.split("=", 1) for item in args.outcome)
    return report(gh, args.daemon_sha, pinned, outcomes, failures(args.results), args.run_url)


if __name__ == "__main__":
    sys.exit(main())
