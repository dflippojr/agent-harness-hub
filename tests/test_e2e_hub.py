"""Exercise the real-daemon Hub helper using lightweight synthetic children, without servers."""

import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
from unittest.mock import DEFAULT, Mock, call

import httpx
import pytest

from .e2e import test_real_daemon as e2e


@pytest.fixture
def resources(monkeypatch, tmp_path):
    children, processes, logs = [], [], []
    real_popen = subprocess.Popen
    client = Mock(spec=httpx.Client)
    client.get.return_value = httpx.Response(200)
    client.headers = {}

    def launch(*args, **kwargs):
        logs.append(kwargs["stdout"])
        child = real_popen([sys.executable, "-c", "import time; time.sleep(120)"], **kwargs)
        children.append(child)
        process = Mock(wraps=child)
        processes.append(process)
        return process

    monkeypatch.setattr(e2e, "free_port", lambda: 8120)
    monkeypatch.setattr(e2e.subprocess, "Popen", launch)
    monkeypatch.setattr(e2e.httpx, "Client", Mock(return_value=client))
    monkeypatch.setattr(e2e, "wait", lambda check, *args: check())
    with TemporaryDirectory(dir=tmp_path) as directory:
        root = e2e.Path(directory)

        def assert_clean():
            assert all(child.poll() is not None for child in children)
            assert all(log.closed for log in logs)
            # In particular, Windows must be able to remove the open-log directory now.
            shutil.rmtree(root)

        try:
            yield root, client, children, processes, logs, assert_clean
        finally:
            # Keep failed regressions from leaking their own synthetic resources.
            for child in children:
                if child.poll() is None:
                    child.kill()
                child.wait(timeout=15)
            for log in logs:
                log.close()


@pytest.mark.parametrize("stage", ["spawn", "client", "readiness"])
def test_startup_failure_closes_every_acquired_resource(resources, monkeypatch, stage):
    root, client, children, processes, logs, assert_clean = resources
    failure = RuntimeError(f"{stage} failed")
    if stage == "spawn":
        def fail_spawn(*args, **kwargs):
            logs.append(kwargs["stdout"])
            raise failure
        monkeypatch.setattr(e2e.subprocess, "Popen", fail_spawn)
    elif stage == "client":
        e2e.httpx.Client.side_effect = failure
    else:
        monkeypatch.setattr(e2e, "wait", Mock(side_effect=failure))

    hub = e2e.Hub.__new__(e2e.Hub)
    with pytest.raises(RuntimeError) as raised:
        hub.__init__({"url": "http://daemon.test"}, root / "state")
    assert raised.value is failure
    assert client.close.call_count == (1 if stage == "readiness" else 0)
    if processes:
        processes[0].wait.assert_called_once_with(timeout=15)
    hub.stop()
    assert_clean()


def test_startup_failure_keeps_original_error_when_cleanup_fails(resources, monkeypatch):
    root, client, children, processes, logs, assert_clean = resources
    failure = AssertionError("readiness timed out")
    monkeypatch.setattr(e2e, "wait", Mock(side_effect=failure))
    client.close.side_effect = RuntimeError("client close failed")
    with pytest.raises(AssertionError) as raised:
        e2e.Hub({"url": "http://daemon.test"}, root / "state")
    assert raised.value is failure
    assert "client close failed" in failure.__notes__[0]
    assert_clean()


@pytest.mark.parametrize("force_kill", [False, True])
def test_stop_reaps_child_and_is_repeatable(resources, force_kill):
    root, client, children, processes, logs, assert_clean = resources
    hub = e2e.Hub({"url": "http://daemon.test"}, root / "state")
    process = processes[0]
    if force_kill:
        process.terminate.side_effect = lambda: None
        process.wait.side_effect = [subprocess.TimeoutExpired("synthetic child", 15), DEFAULT]
    hub.stop()
    process.terminate.assert_called_once_with()
    assert process.wait.call_args_list == [call(timeout=15)] * (2 if force_kill else 1)
    assert process.kill.call_count == int(force_kill)
    client.close.assert_called_once_with()
    assert_clean()
    hub.stop()
    client.close.assert_called_once_with()
    process.terminate.assert_called_once_with()


def test_client_close_failure_still_reaps_child_and_closes_log(resources):
    root, client, children, processes, logs, assert_clean = resources
    hub = e2e.Hub({"url": "http://daemon.test"}, root / "state")
    client.close.side_effect = RuntimeError("client close failed")
    with pytest.raises(RuntimeError, match="client close failed"):
        hub.stop()
    assert_clean()
    hub.stop()
    client.close.assert_called_once_with()


def test_forced_wait_failure_is_bounded_and_other_resources_close(resources):
    root, client, children, processes, logs, assert_clean = resources
    hub = e2e.Hub({"url": "http://daemon.test"}, root / "state")
    # No unkillable real process: report timeouts even after reaping our synthetic child.
    def timeout_after_exit(*args, **kwargs):
        children[0].wait(timeout=15)
        raise subprocess.TimeoutExpired("synthetic child", 15)
    process = processes[0]
    process.wait.side_effect = timeout_after_exit
    with pytest.raises(subprocess.TimeoutExpired):
        hub.stop()
    assert process.wait.call_args_list == [call(timeout=15), call(timeout=15)]
    client.close.assert_called_once_with()
    assert_clean()
    hub.stop()


@pytest.mark.parametrize("failure", [subprocess.TimeoutExpired("signin-link", 60),
                                   subprocess.CalledProcessError(1, "signin-link")])
def test_signin_cli_failure_is_bounded_and_caller_can_cleanup(resources, monkeypatch, failure):
    root, client, children, processes, logs, assert_clean = resources
    hub = e2e.Hub({"url": "http://daemon.test"}, root / "state")
    cli = Mock(side_effect=failure)
    monkeypatch.setattr(e2e.subprocess, "run", cli)
    with pytest.raises(type(failure)) as raised:
        try:
            hub.sign_in()
        finally:
            hub.stop()
    assert raised.value is failure
    assert cli.call_args.kwargs["timeout"] == 60
    assert cli.call_args.kwargs["check"] is True
    client.request.assert_not_called()
    client.close.assert_called_once_with()
    assert_clean()
