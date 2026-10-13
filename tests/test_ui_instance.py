"""The disposable UI-testing instance launcher's safety guards (scripts/ui_instance.py, docs/ui-testing.md).

The full start runs in CI's `ui` job against a real fake model daemon; these check the refusals and the isolation of
what it writes and passes on, without starting anything.
"""

import importlib.util
import json
import socket
from pathlib import Path
from unittest.mock import Mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("ui_instance", ROOT / "scripts" / "ui_instance.py")
ui = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ui)


@pytest.fixture
def daemon_src(tmp_path):
    src = tmp_path / "agent-harness"
    (src / "harness").mkdir(parents=True)
    (src / "harness" / "fake_model.py").write_text("", encoding="utf-8")
    return src


@pytest.mark.parametrize("flag", ["--port", "--daemon-port"])
@pytest.mark.parametrize("port", [8100, 8120])
def test_start_refuses_the_live_ports(tmp_path, daemon_src, capsys, flag, port):
    with pytest.raises(SystemExit) as exited:
        ui.main(["start", "--dir", str(tmp_path / "i"), "--daemon-src", str(daemon_src), flag, str(port)])
    assert exited.value.code == 2
    assert "never uses it" in capsys.readouterr().err
    assert not (tmp_path / "i").exists()


def test_start_refuses_a_busy_port_and_a_used_directory(tmp_path, daemon_src, capsys):
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        with pytest.raises(SystemExit):
            ui.main(["start", "--dir", str(tmp_path / "i"), "--daemon-src", str(daemon_src),
                     "--port", str(busy.getsockname()[1])])
    assert "never reuses a running server" in capsys.readouterr().err
    used = tmp_path / "used"
    used.mkdir()
    (used / "keep.txt").write_text("not ours", encoding="utf-8")
    with pytest.raises(SystemExit):
        ui.main(["start", "--dir", str(used), "--daemon-src", str(daemon_src)])
    assert "is not empty" in capsys.readouterr().err
    assert (used / "keep.txt").read_text(encoding="utf-8") == "not ours"


def test_start_needs_a_daemon_checkout_with_fake_model_mode(tmp_path, capsys):
    with pytest.raises(SystemExit):
        ui.main(["start", "--dir", str(tmp_path / "i"), "--daemon-src", str(tmp_path)])
    assert "fake model mode" in capsys.readouterr().err


def test_daemon_config_is_loopback_disposable_and_hub_enabled(tmp_path):
    config_dir, data_dir = ui.write_daemon_config(tmp_path, 18100)
    config = json.loads((config_dir / "harness.yaml").read_text(encoding="utf-8"))
    assert config["listen"] == {"host": "127.0.0.1", "port": 18100}
    assert Path(config["data_dir"]) == data_dir and data_dir.is_relative_to(tmp_path)
    assert Path(config["repos_dir"]).is_relative_to(tmp_path)
    assert config["modules"]["hub"] is True and config["notify"] == {"enabled": False}
    assert not any(config["modules"][name] for name in ("remote_control", "backup", "homelab", "memory_library"))
    assert all(login.endswith("@example.test") for login in config["allowed_logins"])
    assert (data_dir / ui.DAEMON_MARKER).is_file()


def test_children_get_no_tokens_and_a_home_inside_the_instance(tmp_path, monkeypatch):
    for name in ("HARNESS_TOKEN", "HARNESS_LOCAL_TOKEN", "GITHUB_TOKEN", "GH_TOKEN", "ANTHROPIC_API_KEY",
                 "OPENAI_API_KEY", "TS_AUTHKEY", "HTTPS_PROXY", "HARNESS_FAKE_MODEL"):
        monkeypatch.setenv(name, "real-secret")
    monkeypatch.setenv("VERIFY_RUN_ID", "a-random-run-id")
    env = ui.child_env(tmp_path, HARNESS_URL="http://127.0.0.1:18100")
    assert "real-secret" not in env.values()
    assert env["VERIFY_RUN_ID"] == "a-random-run-id"  # the verify skill's cleanup finds the children by it
    assert Path(env["HOME"]).is_relative_to(tmp_path) and Path(env["USERPROFILE"]).is_relative_to(tmp_path)
    assert env["HARNESS_URL"] == "http://127.0.0.1:18100"
    assert ui.cli_config(tmp_path).parent == tmp_path / "home" / ".agent-harness"


def test_stop_is_repeatable_and_deletes_only_instance_directories(tmp_path):
    assert ui.stop(tmp_path / "absent") == 0
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    (foreign / "data.txt").write_text("keep", encoding="utf-8")
    assert ui.stop(foreign) == 2
    assert (foreign / "data.txt").is_file()
    owned = tmp_path / "owned"
    (owned / "daemon" / "data").mkdir(parents=True)
    (owned / ui.MARKER).write_text("", encoding="utf-8")
    (owned / ui.HANDLE).write_text(json.dumps({"status": "starting", "children": []}), encoding="utf-8")
    assert ui.stop(owned) == 0
    assert not owned.exists()
    assert ui.stop(owned) == 0


def test_a_zombie_or_vanished_process_counts_as_stopped():
    # On Linux a killed launcher stays a zombie until the test runner that started it reaps it.
    psutil = pytest.importorskip("psutil")
    assert ui.exited(Mock(status=Mock(return_value=psutil.STATUS_ZOMBIE)))
    assert ui.exited(Mock(status=Mock(side_effect=psutil.NoSuchProcess(1))))
    assert not ui.exited(Mock(status=Mock(return_value=psutil.STATUS_RUNNING)))
