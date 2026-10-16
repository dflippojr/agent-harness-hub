"""Every contract case runs once per target: `[fake]` in the unit suite, `[daemon]` (marked e2e) against a real
disposable daemon from HUB_E2E_DAEMON_SRC."""

from __future__ import annotations

import os
import sys

import pytest

from .targets import DaemonTarget, FakeTarget, daemon_source


@pytest.fixture(scope="session")
def real_daemon(tmp_path_factory):
    src = daemon_source()
    if src is None:
        pytest.skip("set HUB_E2E_DAEMON_SRC to an agent-harness checkout")
    target = DaemonTarget(src, tmp_path_factory.mktemp("contract-daemon"),
                          python=os.environ.get("HUB_E2E_DAEMON_PYTHON", sys.executable))
    try:
        target.claim()
        yield target
    finally:
        target.close()


@pytest.fixture(params=["fake", pytest.param("daemon", marks=pytest.mark.e2e)])
def target(request):
    if request.param == "daemon":
        yield request.getfixturevalue("real_daemon")
        return
    fake = FakeTarget()
    try:
        fake.claim()
        yield fake
    finally:
        fake.close()


def daemon_cannot(target, reason: str) -> None:
    """A case whose state a disposable fake model daemon cannot produce: it runs on the fake only, with the reason."""
    if target.name == "daemon":
        pytest.skip(f"Daemon cannot produce this state: {reason}")
