"""A failing real-daemon test prints the tail of the daemon's log and of each Hub log it started (#138)."""

from __future__ import annotations

from pathlib import Path

import pytest

LOG_TAIL = 4000
# The logs the running test started; the daemon's is added per test from its fixture.
started_logs: list[Path] = []


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
    daemon = item.funcargs.get("daemon")
    paths = ([daemon["root"] / "daemon.log"] if isinstance(daemon, dict) else []) + list(started_logs)
    for path in dict.fromkeys(paths):
        report.sections.append((f"{path.name} (last {LOG_TAIL} characters)", log_tail(path)))
