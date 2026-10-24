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


# TEMP #138 instrumentation: removed before the PR is ready.
read_timings: list[str] = []


def pytest_terminal_summary(terminalreporter):
    import subprocess
    import time
    tw = terminalreporter
    if not read_timings:
        return
    tw.section("TEMP #138 timings")
    for line in startup_timings + read_timings:
        tw.write_line(line)
    for args in (["docker", "version", "--format", "{{.Server.Os}} {{.Server.Version}}"],
                 ["docker", "image", "ls"],
                 ["docker", "run", "--rm", "--network", "harness-cli-claude", "-e",
                  "HTTPS_PROXY=http://harness-egress-claude:8888", "agent-harness-cli:1", "claude", "auth", "status"]):
        started = time.monotonic()
        try:
            done = subprocess.run(args, capture_output=True, text=True, timeout=90)
            result = f"rc={done.returncode} out={done.stdout[-300:]!r} err={done.stderr[-300:]!r}"
        except (OSError, subprocess.TimeoutExpired) as error:
            result = repr(error)
        tw.write_line(f"{' '.join(args[:3])}: {time.monotonic() - started:.1f} s {result}")


startup_timings: list[str] = []  # TEMP #138


def pytest_sessionstart(session):  # TEMP #138: is the first docker start of the job slow on its own?
    import os
    import subprocess
    import time
    from concurrent.futures import ThreadPoolExecutor
    if not os.environ.get("HUB_E2E_DAEMON_SRC"):
        return
    env = dict(os.environ, DOCKER_HOST="npipe:////./pipe/agent-harness-hub-e2e-no-docker" if os.name == "nt"
               else "unix:///nonexistent/no-docker.sock")

    def once(_):
        started = time.monotonic()
        try:
            subprocess.run(["docker", "version"], env=env, capture_output=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired) as error:
            return f"{error!r}"
        return f"{time.monotonic() - started:.2f}"

    for label in ("cold, 1", "warm, 3 parallel", "warm, 3 parallel"):
        with ThreadPoolExecutor(3) as pool:
            startup_timings.append(f"{label}: {list(pool.map(once, range(1 if label.startswith('cold') else 3)))}")
