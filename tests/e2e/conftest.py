"""A failing real-daemon test prints the tail of the daemon's log and of each Hub log it started (#138). Every run
prints the fixture's notes in its summary, such as how long the Docker CLI took to start (#215)."""

from __future__ import annotations

from pathlib import Path

import pytest

LOG_TAIL = 4000
# The running daemon's log (for every test while the module's daemon is up, whether or not the test uses it), and
# the Hub logs the running test started.
daemon_logs: list[Path] = []
started_logs: list[Path] = []
# Measurements worth keeping on a green run too: a slow start that passes today is tomorrow's timeout.
notes: list[str] = []


def pytest_terminal_summary(terminalreporter):
    for note in notes:
        terminalreporter.write_line(note)


def log_tail(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[-LOG_TAIL:]
    except OSError as error:
        return f"(unreadable: {error})"


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    if call.when == "setup":
        started_logs.clear()
    outcome = yield
    report = outcome.get_result()
    if not report.failed:
        return
    for path in dict.fromkeys(daemon_logs + started_logs):
        report.sections.append((f"{path.name} (last {LOG_TAIL} characters)", log_tail(path)))
