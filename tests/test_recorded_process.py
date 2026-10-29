"""Which recorded {pid, created} entries each dev script still treats as the same process (a reused pid is not).

ui_instance.kill_recorded and the verify skill's verify.alive refuse an entry with no recorded start time; the
doctor's recorded_processes accepts it. These pin that down for a live child, a wrong start time, a missing one and
an exited pid.
"""

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

psutil = pytest.importorskip("psutil")

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / ".claude" / "skills" / "verify" / "scripts"


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses look their module up
    spec.loader.exec_module(module)
    return module


ui = load("recorded_ui_instance", ROOT / "scripts" / "ui_instance.py")
verify = load("recorded_verify", SCRIPTS / "verify.py")
doctor = load("recorded_doctor", SCRIPTS / "doctor.py")
KINDS = ("right", "off by 1 s", "missing", "exited")


def sleeper() -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])


@pytest.fixture
def children():
    procs = []

    def spawn():
        procs.append(sleeper())
        return procs[-1]
    yield spawn
    for proc in procs:
        proc.kill()
        proc.wait()


def entry(kind: str, proc: subprocess.Popen) -> dict:
    """An entry of the given kind; for "exited", proc is a child that has already been reaped."""
    started = psutil.Process(proc.pid).create_time() if kind != "exited" else 0.0
    return {"name": kind, "pid": proc.pid, **({"right": {"created": started}, "off by 1 s": {"created": started + 1},
                                               "exited": {"created": started}}.get(kind, {}))}


def exited_child() -> subprocess.Popen:
    proc = sleeper()
    proc.kill()
    proc.wait()
    return proc


def test_verify_alive_accepts_only_the_right_start_time(children):
    live = children()
    accepted = {kind for kind in KINDS
                if verify.alive(entry(kind, exited_child() if kind == "exited" else live)) is not None}
    assert accepted == {"right"}


def test_doctor_also_accepts_an_entry_without_a_start_time(children):
    live = children()
    handle = {"children": [entry(kind, exited_child() if kind == "exited" else live) for kind in KINDS]}
    found = doctor.recorded_processes(handle)
    assert {name for name, _ in found} == {"right", "missing"}
    assert {proc.pid for _, proc in found} == {live.pid}


@pytest.mark.parametrize("kind", KINDS)
def test_kill_recorded_kills_only_the_right_start_time(children, kind):
    proc = exited_child() if kind == "exited" else children()
    assert ui.kill_recorded({"children": [entry(kind, proc)]})
    if kind == "right":
        assert proc.wait(timeout=15) is not None
    elif kind != "exited":
        assert proc.poll() is None, f"kill_recorded killed the process of an entry with {kind} start time"
