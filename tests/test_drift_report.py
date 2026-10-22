"""The nightly drift report (scripts/drift_report.py): which issue it opens, refreshes or closes, with a fake `gh`."""

import importlib.util
import json
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("drift_report", ROOT / "scripts" / "drift_report.py")
drift = importlib.util.module_from_spec(spec)
sys.modules["drift_report"] = drift  # dataclasses look their module up by name
spec.loader.exec_module(drift)

PIN = "a" * 40
HEAD = "b" * 40
OLDER = "c" * 40
RUN = "https://github.com/dflippojr/agent-harness-hub/actions/runs/1"
GREEN = {"daemon": "success", "ui": "success"}


class FakeGh:
    """Answers the reads from `issues` (title -> number, state) and records every write."""

    def __init__(self, issues=(), days_old=3, ahead=5, comments=()):
        self.issues = list(issues)
        self.days_old, self.ahead, self.comments = days_old, ahead, list(comments)
        self.calls: list[tuple[list[str], str | None]] = []

    def __call__(self, args, stdin=None):
        self.calls.append((args, stdin))
        if args[:2] == ["issue", "list"]:
            state = args[args.index("--state") + 1]
            return json.dumps([{"number": n, "title": t, "state": s} for t, n, s in self.issues
                               if state == "all" or s.lower() == state])
        if args[:2] == ["issue", "view"]:
            return json.dumps({"comments": [{"body": body} for body in self.comments]})
        if args[0] == "api" and "/compare/" in args[1]:
            return json.dumps({"ahead_by": self.ahead})
        if args[0] == "api":
            stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - self.days_old * 86400))
            return json.dumps({"sha": HEAD, "commit": {"committer": {"date": stamp}}})
        return ""

    def writes(self):
        return [(args, stdin) for args, stdin in self.calls if args[0] == "issue" and args[1] in
                ("create", "edit", "comment", "close")]


def run(fake, outcomes=GREEN, failed=None, dry_run=False):
    gh = drift.GitHub("dflippojr/agent-harness-hub", dry_run, run=fake)
    drift.report(gh, HEAD, PIN, outcomes, failed or {}, RUN)
    return gh


def test_green_opens_one_bump_issue_with_its_labels_and_the_run():
    fake = FakeGh()
    run(fake)
    (args, body), = fake.writes()
    assert args[:2] == ["issue", "create"] and drift.BUMP_PREFIX + HEAD in args
    assert {"--label=ready", "--label=tier:mechanical", "--label=verification-uplift"} <= set(args)
    assert RUN in body and drift.compare_url(PIN, HEAD) in body


def test_green_refreshes_the_open_bump_issue_and_closes_the_drift_bug():
    fake = FakeGh(issues=[(drift.BUMP_PREFIX + OLDER, 7, "OPEN"), (drift.DRIFT_PREFIX + "ccccccc fails", 9, "OPEN")])
    run(fake)
    writes = [args[:3] for args, _ in fake.writes()]
    assert ["issue", "edit", "7"] in writes and ["issue", "create"] not in [w[:2] for w in writes]
    edit = next(args for args, _ in fake.writes() if args[:2] == ["issue", "edit"])
    assert edit[edit.index("--title") + 1] == drift.BUMP_PREFIX + HEAD
    assert ["issue", "comment", "9"] in writes and ["issue", "close", "9"] in writes


def test_green_at_the_pin_opens_nothing():
    fake = FakeGh(ahead=0)
    run(fake)
    assert fake.writes() == []


def test_red_opens_one_bug_naming_the_failing_cases_and_the_range_since_the_last_green(tmp_path):
    (tmp_path / "contract-ubuntu-latest.xml").write_text(
        '<testsuites><testsuite><testcase classname="tests.contract.test_apps_contract" name="test_keys[daemon]">'
        '<failure message="drift"/></testcase><testcase classname="x" name="ok"/></testsuite></testsuites>',
        encoding="utf-8")
    fake = FakeGh(issues=[(drift.BUMP_PREFIX + OLDER, 4, "CLOSED")])
    run(fake, {"daemon": "failure", "ui": "success"}, drift.failures(tmp_path))
    (args, body), = fake.writes()
    assert args[:2] == ["issue", "create"] and "--label=bug" in args
    assert "tests.contract.test_apps_contract::test_keys[daemon]" in body and "::ok" not in body
    assert drift.compare_url(OLDER, HEAD) in body and RUN in body


def test_red_without_a_green_bump_counts_from_the_pin_and_refreshes_the_open_bug():
    fake = FakeGh(issues=[(drift.DRIFT_PREFIX + "ccccccc fails", 9, "OPEN")])
    run(fake, {"daemon": "success", "ui": "cancelled"})
    writes = fake.writes()
    assert [args[:3] for args, _ in writes] == [["issue", "edit", "9"], ["issue", "comment", "9"]]
    assert drift.compare_url(PIN, HEAD) in writes[0][1] and "a job failed before or around its tests" in writes[0][1]


@pytest.mark.parametrize("already", [False, True])
def test_an_old_pin_is_commented_once_per_pinned_commit(already):
    marker = f"<!-- pin-age:{PIN} -->"
    fake = FakeGh(issues=[(drift.BUMP_PREFIX + OLDER, 7, "OPEN")], days_old=20, comments=[marker] if already else [])
    run(fake)
    aged = [body for args, body in fake.writes() if args[1] == "comment" and body and marker in body]
    assert len(aged) == (0 if already else 1)
    assert already or "20 days old" in aged[0]


def test_a_dry_run_reads_but_writes_nothing(capsys):
    fake = FakeGh()
    gh = run(fake, dry_run=True)
    assert fake.writes() == [] and gh.writes == ["issue create --title"]
    assert "[dry run] gh issue create" in capsys.readouterr().out


def _checkout(sha: str) -> str:
    return f"""
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7
        with:
          repository: dflippojr/agent-harness
          ref: {sha}
          path: daemon
"""


def test_the_pin_is_read_from_ci_and_must_be_one_commit():
    # tests/test_supply_chain.py checks the live ci.yml's pin; this checks the parser on its own.
    assert drift.pinned_sha("jobs:" + _checkout(PIN) + _checkout(PIN)) == PIN
    with pytest.raises(SystemExit):
        drift.pinned_sha("jobs:" + _checkout(PIN) + _checkout(OLDER))
    with pytest.raises(SystemExit):
        drift.pinned_sha("jobs:\n  test:\n    runs-on: ubuntu-latest\n")
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert len(drift.pinned_sha(ci)) == 40  # every daemon checkout in the live ci.yml names one commit


def test_an_unreadable_results_file_is_reported_not_ignored(tmp_path):
    (tmp_path / "ui.xml").write_text("<testsuites", encoding="utf-8")
    assert drift.failures(tmp_path) == {"ui": ["(unreadable results file ui.xml)"]}


def check(fake, mode, pinned=PIN, head=HEAD):
    """The pin-age check against a fake `gh`: (exit code, the annotation it printed, or None)."""
    return drift.pin_age_check(drift.GitHub("dflippojr/agent-harness-hub", False, run=fake), pinned, head,
                               mode == "fail")


@pytest.mark.parametrize("mode", ["warn", "fail"])
def test_a_fresh_pin_passes_quietly(mode, capsys):
    assert check(FakeGh(days_old=3, ahead=5), mode) == 0
    out = capsys.readouterr().out
    assert "::" not in out and "within the 14-day limit" in out


@pytest.mark.parametrize("mode", ["warn", "fail"])
def test_an_old_pin_that_is_still_main_passes(mode, capsys):
    # The pin is old, but agent-harness main has nothing after it: nothing to bump to.
    assert check(FakeGh(days_old=40, ahead=0), mode) == 0
    assert "::" not in capsys.readouterr().out


def test_a_stale_pin_warns_on_a_pull_request_and_fails_on_main(capsys):
    fake = FakeGh(issues=[(drift.BUMP_PREFIX + HEAD, 12, "OPEN")], days_old=20, ahead=9)
    assert check(fake, "warn") == 0
    warned = capsys.readouterr().out
    assert warned.startswith("::warning::") and "pin is 20 days behind agent-harness main (9 commits)" in warned
    assert check(fake, "fail") == 1
    failed = capsys.readouterr().out
    assert failed.startswith("::error::") and "pin is 20 days behind agent-harness main (9 commits)" in failed
    assert f"#12 {drift.BUMP_PREFIX}{HEAD}" in failed
    assert not fake.writes()  # a check: it reads and never writes


def test_a_stale_pin_without_a_bump_issue_names_main_instead(capsys):
    assert check(FakeGh(days_old=15, ahead=2), "fail") == 1
    out = capsys.readouterr().out
    assert "pin is 15 days behind" in out and f"Bump it to {HEAD}" in out and "issue is open" in out


def test_the_limit_is_whole_days(capsys):
    assert check(FakeGh(days_old=14.9, ahead=2), "fail") == 0  # still 14 days old
    assert check(FakeGh(days_old=15.1, ahead=2), "fail") == 1


def test_a_pull_request_that_bumps_the_pin_does_not_warn(capsys):
    # The bump makes main's HEAD the pin: nothing is compared, and the commit is new.
    fake = FakeGh(days_old=1, ahead=7)
    assert check(fake, "warn", pinned=HEAD, head=HEAD) == 0
    assert "::" not in capsys.readouterr().out
    assert not any("/compare/" in args[1] for args, _ in fake.calls if args[0] == "api")


def test_main_head_is_read_from_the_api_when_not_given(capsys):
    fake = FakeGh(days_old=30, ahead=3)
    assert check(fake, "fail", head=None) == 1
    assert ["api", f"repos/{drift.DAEMON_REPO}/commits/main"] in [args for args, _ in fake.calls]


@pytest.mark.parametrize(("mode", "code", "level"), [("warn", 0, "warning"), ("fail", 1, "error")])
def test_github_being_unreadable_fails_only_on_main(mode, code, level, capsys):
    def broken(args, stdin=None):
        raise drift.subprocess.CalledProcessError(1, ["gh", *args], stderr="HTTP 502\n")

    assert check(broken, mode) == code
    assert capsys.readouterr().out.startswith(f"::{level}::Could not read the agent-harness pin's age: HTTP 502")


@pytest.mark.parametrize(("mode", "code", "level"), [("warn", 0, "warning"), ("fail", 1, "error")])
def test_the_command_line_reads_the_live_pin(mode, code, level, monkeypatch, capsys):
    fake = FakeGh(days_old=21, ahead=4)
    monkeypatch.setattr(drift.GitHub, "_gh", staticmethod(fake))
    assert drift.main(["pin-age", "--mode", mode, "--daemon-sha", HEAD]) == code
    pinned = drift.pinned_sha((ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))
    assert ["api", f"repos/{drift.DAEMON_REPO}/compare/{pinned}...{HEAD}"] in [args for args, _ in fake.calls]
    assert capsys.readouterr().out.startswith(f"::{level}::The agent-harness pin {pinned[:7]}")
