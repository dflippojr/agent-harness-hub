"""The verify skill's doctor checks and cleanup (.claude/skills/verify).

The full run needs an agent-harness checkout and a browser; these check the doctor's verdicts on synthetic answers,
and that cleanup leaves nothing behind after a failed drive step, with a stand-in launcher.
"""

import importlib.util
import json
import socket
import sys
import time
from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / ".claude" / "skills" / "verify" / "scripts"


def load(name):
    spec = importlib.util.spec_from_file_location(f"verify_{name}", SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses look their module up
    spec.loader.exec_module(module)
    return module


doctor = load("doctor")
HUB, DAEMON = 50101, 50102
HANDLE = {"status": "ready", "hub": f"http://127.0.0.1:{HUB}", "daemon": f"http://127.0.0.1:{DAEMON}"}
LISTENING = [("127.0.0.1", HUB), ("127.0.0.1", DAEMON)]
ANY = "0.0.0.0"  # noqa: S104 - a listener the doctor must refuse
SEEDED = {"/api/claim": {"state": "claimed"}, "/api/apps": {"apps": [{"name": "Synthetic App"}]},
          "/api/pairing": {"pending": [{"name": "Synthetic notes"}, {"name": "Synthetic approver"}]},
          "/api/manage/accounts": {"users": [{"login": "member@example.test"}]}}


def client(answers: dict, base_url: str = "http://127.0.0.1:50101") -> httpx.Client:
    def answer(request):
        body = answers.get(request.url.path)
        if isinstance(body, int):
            return httpx.Response(body)
        return httpx.Response(404) if body is None else httpx.Response(200, json=body)
    return httpx.Client(transport=httpx.MockTransport(answer), base_url=base_url)


@pytest.fixture
def instance(tmp_path):
    root = tmp_path / "instance"
    (root / "daemon" / "config").mkdir(parents=True)
    (root / "daemon" / "data").mkdir()
    (root / "daemon" / "data" / "fake-model-instance").write_text("x", encoding="utf-8")
    (root / "daemon" / "config" / "harness.yaml").write_text(json.dumps(
        {"data_dir": (root / "daemon" / "data").as_posix(), "repos_dir": (root / "daemon" / "repos").as_posix()}),
        encoding="utf-8")
    return root


def isolated_env(root):
    home = str(root / "home")
    return {"PATH": "/usr/bin", "HOME": home, "USERPROFILE": home, "HARNESS_CONFIG_DIR": str(root / "daemon")}


# Healthy ------------------------------------------------------------------------------------------------------------
def test_a_healthy_instance_passes_every_check(instance):
    assert doctor.check_ready(HANDLE, client({"/healthz": {"ok": True}, "/health": {}})).ok
    assert doctor.check_loopback(HANDLE, LISTENING, expect_port=HUB).ok
    assert doctor.check_seed(client(SEEDED)).ok
    hub_argv = ["python", "-m", "harness_hub", "--state-dir", str(instance / "hub")]
    assert doctor.check_isolation(instance, [("hub", isolated_env(instance), hub_argv),
                                             ("daemon", isolated_env(instance), [])]).ok


# Not ready ----------------------------------------------------------------------------------------------------------
def test_not_ready_until_the_handle_says_so_and_both_servers_answer():
    healthy = client({"/healthz": {"ok": True}, "/health": {}})
    starting = doctor.check_ready(dict(HANDLE, status="starting"), healthy)
    assert not starting.ok and "'starting'" in starting.detail
    down = doctor.check_ready(HANDLE, client({"/healthz": {"ok": True}, "/health": 503}))
    assert not down.ok and "HTTP 503" in down.detail

    def refuse(request):
        raise httpx.ConnectError("refused", request=request)
    gone = doctor.check_ready(HANDLE, httpx.Client(transport=httpx.MockTransport(refuse)))
    assert not gone.ok and "did not answer" in gone.detail


@pytest.mark.parametrize("handle", [dict(HANDLE, hub="http://127.0.0.1:8100"), dict(HANDLE, daemon="http://example.ts.net")])
def test_unsafe_urls_are_never_contacted_or_signed_in_to(tmp_path, monkeypatch, handle):
    requests = []

    def record(request):
        requests.append(str(request.url))
        return httpx.Response(200, json={})
    real = httpx.Client
    monkeypatch.setattr(doctor.httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(record)))
    monkeypatch.setattr(doctor, "recorded_processes", lambda handle: [])
    monkeypatch.setattr(doctor, "signed_in_client", Mock(side_effect=AssertionError("signed in")))
    (tmp_path / "instance.json").write_text(json.dumps(handle), encoding="utf-8")
    lines = []
    assert doctor.run(tmp_path, out=lines.append) == 1
    assert requests == []
    assert lines[0].startswith("FAIL ready: not contacted")
    assert lines[-1].startswith("FAIL seed: skipped")


def test_doctor_fails_without_an_instance_handle(tmp_path):
    lines = []
    assert doctor.run(tmp_path, out=lines.append) == 1
    assert lines[0].startswith("FAIL ready")


# Non-loopback -------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize(("handle", "listeners", "port", "why"), [
    (HANDLE, [(ANY, HUB), ("127.0.0.1", DAEMON)], HUB, "beyond loopback"),
    (HANDLE, [*LISTENING, ("::", 50999)], HUB, "beyond loopback"),
    (dict(HANDLE, hub=f"http://{ANY}:{HUB}"), LISTENING, HUB, "is not http://127.0.0.1"),
    (dict(HANDLE, hub="http://example.ts.net"), LISTENING, None, "is not http://127.0.0.1"),
    (dict(HANDLE, daemon="http://127.0.0.1:8100"), LISTENING, HUB, "live port 8100"),
    (HANDLE, LISTENING, HUB + 7, f"expected {HUB + 7}"),
    (HANDLE, [("127.0.0.1", DAEMON)], HUB, f"no recorded process listens on {HUB}"),
])
def test_loopback_fails_beyond_loopback_or_off_the_expected_port(handle, listeners, port, why):
    check = doctor.check_loopback(handle, listeners, expect_port=port)
    assert not check.ok
    assert why in check.detail


# Missing seed -------------------------------------------------------------------------------------------------------
def test_seed_fails_when_unclaimed_or_a_seed_row_is_missing():
    unclaimed = doctor.check_seed(client(dict(SEEDED, **{"/api/claim": {"state": "unclaimed"}})))
    assert not unclaimed.ok and "not claimed" in unclaimed.detail
    no_app = doctor.check_seed(client(dict(SEEDED, **{"/api/apps": {"apps": []}})))
    assert not no_app.ok and "Synthetic App (/api/apps)" in no_app.detail
    refused = doctor.check_seed(client(dict(SEEDED, **{"/api/manage/accounts": 502})))
    assert not refused.ok and "member@example.test" in refused.detail


# Isolation ----------------------------------------------------------------------------------------------------------
def test_isolation_fails_on_credentials_or_data_paths_outside_the_instance(instance, tmp_path):
    leaked = doctor.check_isolation(instance, [("hub", dict(isolated_env(instance), GITHUB_TOKEN="x"), [])])
    assert not leaked.ok and "GITHUB_TOKEN" in leaked.detail
    real_home = doctor.check_isolation(instance, [("daemon", dict(isolated_env(instance), HOME=str(tmp_path)), [])])
    assert not real_home.ok and "HOME" in real_home.detail
    real_state = doctor.check_isolation(instance, [("hub", isolated_env(instance),
                                                    ["-m", "harness_hub", "--state-dir", str(tmp_path / "s")])])
    assert not real_state.ok and "--state-dir" in real_state.detail
    assert not doctor.check_isolation(instance, []).ok
    (instance / "daemon" / "data" / "fake-model-instance").unlink()
    unmarked = doctor.check_isolation(instance, [("hub", isolated_env(instance), [])])
    assert not unmarked.ok and "marker" in unmarked.detail


# Cleanup after a failed drive ---------------------------------------------------------------------------------------
FAKE_LAUNCHER = """
import importlib.util, json, os, socket, subprocess, sys, time
from pathlib import Path
spec = importlib.util.spec_from_file_location("ui", {real!r})
ui = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ui)
if sys.argv[1] == "stop":
    sys.exit(ui.main(sys.argv[1:]))
root = Path(sys.argv[sys.argv.index("--dir") + 1])
root.mkdir(parents=True, exist_ok=True)  # verify.py hands over a new empty directory
(root / ui.MARKER).write_text("stand-in", encoding="utf-8")
with socket.socket() as s:
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
server = subprocess.Popen([sys.executable, "-c", "import socket, time; s = socket.socket(); "
                           f"s.bind(('127.0.0.1', {{port}})); s.listen(); time.sleep(600)"])
url = f"http://127.0.0.1:{{port}}"
handle = {{"status": "ready", "dir": str(root), "pid": os.getpid(), "pid_created": ui.process_created(os.getpid()),
          "children": [{{"name": "hub", "pid": server.pid, "created": ui.process_created(server.pid)}}],
          "hub": url, "daemon": url}}
ui.write_json(root / ui.HANDLE, handle)
for _ in range(200):
    try:
        socket.create_connection(("127.0.0.1", port), timeout=1).close()
        break
    except OSError:
        time.sleep(0.05)
print("READY " + json.dumps({{"hub": url, "daemon": url, "dir": str(root), "pid": os.getpid()}}), flush=True)
time.sleep(600)
"""

FAILING_DRIVE = """
import pathlib, subprocess, sys
# A browser the drive step leaves behind, then it fails at once: no sampling of its tree can see the orphan.
orphan = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"],
                          start_new_session=sys.argv[2] == "own-session")
pathlib.Path(sys.argv[1]).write_text(str(orphan.pid), encoding="utf-8")
sys.exit(3)
"""


@pytest.mark.skipif(sys.version_info < (3, 12), reason="ui_instance.py needs Python 3.12")
@pytest.mark.parametrize("session", [
    "same-session",
    pytest.param("own-session", marks=pytest.mark.skipif(
        sys.platform == "win32", reason="start_new_session is POSIX only; same as same-session on Windows")),
])
def test_cleanup_after_a_failed_drive_leaves_nothing_running(tmp_path, monkeypatch, session):
    psutil = pytest.importorskip("psutil")
    verify = load("verify")
    launcher = tmp_path / "launcher.py"
    launcher.write_text(FAKE_LAUNCHER.format(real=str(ROOT / "scripts" / "ui_instance.py")), encoding="utf-8")
    monkeypatch.setattr(verify.tempfile, "gettempdir", lambda: str(tmp_path))
    run = verify.Run(tmp_path / "evidence", launcher=launcher)
    run.doctor_argv = lambda: [sys.executable, "-c", "print('PASS stand-in')"]
    orphan_file = tmp_path / "orphan.pid"
    run.drive_argv = lambda features: [sys.executable, "-c", FAILING_DRIVE, str(orphan_file), session]

    assert run.run(["settings"]) == 1

    steps = {name: ok for name, ok, _ in run.steps}
    assert steps == {"launch": True, "doctor": True, "drive": False, "cleanup": True}
    record = json.loads((tmp_path / "evidence" / "launch.json").read_text(encoding="utf-8"))
    assert {"launcher", "doctor", "drive"} <= {p["name"] for p in record["processes"]}
    orphan = int(orphan_file.read_text(encoding="utf-8"))
    assert not psutil.pid_exists(orphan) or psutil.Process(orphan).status() == psutil.STATUS_ZOMBIE
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and any(psutil.pid_exists(p["pid"]) and verify.alive(p)
                                              for p in record["processes"]):
        time.sleep(0.2)
    assert not [p for p in record["processes"] if verify.alive(p)]
    assert not Path(record["instance_dir"]).exists()
    with socket.socket() as s:
        s.settimeout(0.2)  # the listener is gone; Windows would retry a refused loopback connect for about a second
        assert s.connect_ex(("127.0.0.1", record["hub_port"])) != 0
    summary = (tmp_path / "evidence" / "summary.md").read_text(encoding="utf-8")
    assert "# Verify: FAIL" in summary
    assert "| drive | FAIL |" in summary and "| cleanup | PASS |" in summary
    assert verify.main(["cleanup", "--evidence", str(tmp_path / "evidence")]) == 0  # safe to repeat


def test_each_run_gets_its_own_instance_directory(tmp_path, monkeypatch):
    verify = load("verify")
    monkeypatch.setattr(verify.tempfile, "gettempdir", lambda: str(tmp_path))
    exited = Mock(pid=0, returncode=2, **{"poll.return_value": 2})

    def spawn(name, argv, log, env=None):
        log.write_text("refused", encoding="utf-8")
        return exited
    dirs = []
    for parent in ("run-a", "run-b"):  # the same evidence basename under different parents
        run = verify.Run(tmp_path / parent / "evidence")
        run.spawn, run.track_tree = spawn, lambda name, pid: None
        assert not run.launch()
        dirs.append(run.record["instance_dir"])
    assert dirs[0] != dirs[1]


def test_an_interrupted_run_is_a_failure_and_still_cleans_up(tmp_path):
    verify = load("verify")
    run = verify.Run(tmp_path / "evidence")
    run.launch = lambda: run.step("launch", True, "stand-in")
    run.doctor = lambda: run.step("doctor", True, "stand-in")

    def interrupt(features):
        raise KeyboardInterrupt
    run.drive = interrupt
    cleaned = []
    run.cleanup = lambda: cleaned.append(True) or run.step("cleanup", True, "stand-in")

    assert run.run([]) == 1
    assert cleaned
    summary = (tmp_path / "evidence" / "summary.md").read_text(encoding="utf-8")
    assert "# Verify: FAIL" in summary and "| interrupted | FAIL |" in summary


def test_a_run_missing_a_step_does_not_pass(tmp_path):
    run = load("verify").Run(tmp_path / "evidence")
    for name in ("launch", "doctor", "cleanup"):
        run.step(name, True, "")
    assert not run.passed()
    run.step("drive", True, "")
    assert run.passed()


def test_every_run_gets_its_own_evidence_folder(tmp_path, capsys):
    verify = load("verify")
    names = {verify.run_name() for _ in range(5)}
    assert len(names) == 5
    used = tmp_path / "used"
    used.mkdir()
    (used / "launch.json").write_text("{}", encoding="utf-8")
    with pytest.raises(SystemExit):
        verify.main(["run", "--evidence", str(used)])
    assert "not empty" in capsys.readouterr().err


@pytest.mark.skipif(sys.platform == "win32", reason="the run token: the POSIX containment")
def test_a_process_without_the_run_token_is_never_touched():
    pytest.importorskip("psutil")
    verify = load("verify")
    import subprocess
    # Not this run's: not its descendant (no subreaper here) and without its token, like a process that reused an id.
    stranger = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True)
    try:
        contained = verify.Containment()
        contained.reaper = False
        assert stranger.pid not in {p.pid for p in contained._members()}
        assert contained.kill() == 0
        assert stranger.poll() is None
    finally:
        stranger.kill()
        stranger.wait()


@pytest.mark.skipif(sys.platform == "win32", reason="the run token: the POSIX containment")
def test_an_orphan_outside_the_tree_and_group_is_found_by_the_run_token(tmp_path):
    psutil = pytest.importorskip("psutil")
    verify = load("verify")
    contained = verify.Containment()
    contained.reaper = False  # as on macOS
    orphan_file = tmp_path / "orphan.pid"
    step = contained.popen([sys.executable, "-c", FAILING_DRIVE, str(orphan_file), "own-session"])
    step.wait(timeout=30)
    orphan = int(orphan_file.read_text(encoding="utf-8"))
    assert contained.kill() >= 1  # not a descendant any more: only the token finds it
    assert not psutil.pid_exists(orphan) or psutil.Process(orphan).status() == psutil.STATUS_ZOMBIE


def test_isolation_fails_when_a_process_cannot_be_inspected(instance):
    blocked = doctor.check_isolation(instance, [("hub", isolated_env(instance), []), ("daemon", None, [])])
    assert not blocked.ok and "daemon could not be inspected" in blocked.detail
    hub_only = doctor.check_isolation(instance, [("hub", isolated_env(instance), [])])
    assert not hub_only.ok and "no daemon process was inspected" in hub_only.detail


def test_ctrl_c_during_cleanup_does_not_stop_it(tmp_path):
    import signal
    verify = load("verify")
    run = verify.Run(tmp_path / "evidence")
    run.launch = lambda: run.step("launch", False, "stand-in")
    finished = []

    def cleanup():
        signal.raise_signal(signal.SIGINT)  # Ctrl+C while Teardown waits
        finished.append(True)
        return run.step("cleanup", True, "stand-in")
    run.cleanup = cleanup
    assert run.run([]) == 1
    assert finished
    assert (tmp_path / "evidence" / "summary.md").is_file()
    assert signal.getsignal(signal.SIGINT) is signal.default_int_handler
