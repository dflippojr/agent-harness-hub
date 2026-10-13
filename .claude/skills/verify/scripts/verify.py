"""Verify a change in the real Hub: launch, doctor, drive, evidence, cleanup (.claude/skills/verify/SKILL.md).

    python .claude/skills/verify/scripts/verify.py run [--feature NAME ...] [--evidence DIR]
    python .claude/skills/verify/scripts/verify.py cleanup --evidence DIR

`run` starts a disposable instance with scripts/ui_instance.py (docs/ui-testing.md, "Start") as its own background
process, records it in DIR/launch.json before anything else, runs the doctor, drives the main path (or the named
features) with the tools/ui-tests Playwright helpers, writes DIR/summary.md, and always cleans up: the contract's
Teardown, then every process this run recorded, by pid and start time. `cleanup` repeats that cleanup from a
launch.json when a run was killed before its own cleanup ran. DIR defaults to .verify/<UTC timestamp>/ (git-ignored).
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

REPO = Path(__file__).resolve().parents[4]
LAUNCHER = REPO / "scripts" / "ui_instance.py"
DOCTOR = Path(__file__).resolve().with_name("doctor.py")
UI_TESTS = REPO / "tools" / "ui-tests"
FEATURES = ("signin", "overview", "apps", "modules", "pairing", "settings")
START_SECONDS = 180  # docs/ui-testing.md, "Start": CI allows 180
STEP_SECONDS = 600
STEPS = ("launch", "doctor", "drive", "cleanup")  # a run passes only when every one of them ran and passed


def run_name() -> str:
    """A UTC timestamp plus a random suffix: two runs started in the same second never share a folder."""
    return datetime.now(UTC).strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(3)


def created(pid: int) -> float | None:
    import psutil
    try:
        return psutil.Process(pid).create_time()
    except psutil.Error:
        return None


def alive(entry: dict):
    """The recorded process if it still runs with its recorded start time (so a reused pid is left alone)."""
    import psutil
    try:
        proc = psutil.Process(int(entry["pid"]))
        if entry.get("created") is None or abs(proc.create_time() - float(entry["created"])) > 0.01:
            return None
        return None if proc.status() == psutil.STATUS_ZOMBIE else proc
    except (psutil.Error, KeyError, TypeError, ValueError):
        return None


def port_open(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(1)
        return s.connect_ex(("127.0.0.1", port)) == 0


class Containment:
    """Holds every process a run starts and all their descendants, including ones whose parent has already exited
    (sampling the process tree would miss those).

    - Windows: one Job Object with kill-on-close, so they also die if this script is killed.
    - Linux: this script becomes a child subreaper, so an orphan, even one that started its own session, is
      re-parented here and stays its descendant.
    - Everywhere but Windows: every step's environment carries this run's random token, so a process that left
      the tree (macOS has no subreaper, and a later `cleanup` is not the parent) is still found by it.

    Nothing is selected by process group, name or port: an id another process reused is never touched."""

    RUN_VAR = "VERIFY_RUN_ID"  # the variable holding the run token

    def __init__(self, token: str = ""):
        self.job = _windows_job() if os.name == "nt" else None
        self.reaper = self.job is None and _become_subreaper()
        self.token = token or secrets.token_hex(16)

    def popen(self, argv: list[str], **kwargs) -> subprocess.Popen:
        if self.job is None:
            kwargs["env"] = dict(kwargs.get("env") or os.environ, **{self.RUN_VAR: self.token})
            return subprocess.Popen(argv, **kwargs)  # noqa: S603
        flags = subprocess.CREATE_NEW_PROCESS_GROUP | 0x4  # CREATE_SUSPENDED: in the job before it starts anything
        proc = subprocess.Popen(argv, creationflags=flags, **kwargs)  # noqa: S603
        self.job.assign(int(proc._handle))  # the process handle Popen holds
        return proc

    def _members(self) -> list:
        """POSIX: this script's own descendants (with the subreaper, every orphan of the run), plus every process
        that carries this run's token."""
        import psutil
        found = {p.pid: p for p in psutil.Process().children(recursive=True)} if self.reaper else {}
        for proc in psutil.process_iter():
            with contextlib.suppress(psutil.Error, OSError):
                if proc.pid != os.getpid() and proc.environ().get(self.RUN_VAR) == self.token:
                    found[proc.pid] = proc
        live = []
        for proc in found.values():
            with contextlib.suppress(psutil.Error):
                if proc.status() != psutil.STATUS_ZOMBIE:
                    live.append(proc)
        return live

    def kill(self) -> int:
        """Kill everything still contained, each process by pid; return how many there were."""
        if self.job is not None:
            left = self.job.active()
            self.job.terminate()
            return left
        import psutil
        members = self._members()
        for proc in members:
            with contextlib.suppress(psutil.Error):
                proc.kill()
        psutil.wait_procs(members, timeout=15)
        with contextlib.suppress(psutil.Error):  # reap the re-parented orphans so none stays a zombie
            psutil.wait_procs(psutil.Process().children(), timeout=1)
        return len(members)

    def remaining(self) -> int:
        return self.job.active() if self.job is not None else len(self._members())


def _become_subreaper() -> bool:
    if not sys.platform.startswith("linux"):
        return False
    import ctypes
    try:
        return ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0) == 0  # PR_SET_CHILD_SUBREAPER
    except (OSError, AttributeError):
        return False


def _windows_job():
    import ctypes
    from ctypes import wintypes

    class Basic(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class Extended(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", Basic), ("IoInfo", ctypes.c_uint64 * 6),
                    ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

    class Accounting(ctypes.Structure):
        _fields_ = [("TotalUserTime", ctypes.c_int64), ("TotalKernelTime", ctypes.c_int64),
                    ("ThisPeriodTotalUserTime", ctypes.c_int64), ("ThisPeriodTotalKernelTime", ctypes.c_int64),
                    ("TotalPageFaultCount", wintypes.DWORD), ("TotalProcesses", wintypes.DWORD),
                    ("ActiveProcesses", wintypes.DWORD), ("TotalTerminatedProcesses", wintypes.DWORD)]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel32.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
                                                   ctypes.c_void_p]
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    ntdll = ctypes.WinDLL("ntdll")
    ntdll.NtResumeProcess.argtypes = [wintypes.HANDLE]

    class Job:
        def __init__(self):
            self.handle = kernel32.CreateJobObjectW(None, None)
            info = Extended()
            info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not self.handle or not kernel32.SetInformationJobObject(self.handle, 9, ctypes.byref(info),
                                                                       ctypes.sizeof(info)):
                raise OSError(ctypes.get_last_error(), "could not create a job object")

        def assign(self, process: int) -> None:
            if not kernel32.AssignProcessToJobObject(self.handle, process):
                error = ctypes.get_last_error()
                kernel32.TerminateProcess(process, 1)  # still suspended: it never ran, so nothing escaped
                raise OSError(error, "could not put the process in the run's job object")
            ntdll.NtResumeProcess(process)

        def active(self) -> int:
            info = Accounting()
            kernel32.QueryInformationJobObject(self.handle, 1, ctypes.byref(info), ctypes.sizeof(info), None)
            return info.ActiveProcesses

        def terminate(self) -> None:
            kernel32.TerminateJobObject(self.handle, 1)
            deadline = time.monotonic() + 15
            while self.active() and time.monotonic() < deadline:
                time.sleep(0.1)

    return Job()


class Run:
    def __init__(self, evidence: Path, python: str = sys.executable, launcher: Path = LAUNCHER):
        self.evidence = evidence.resolve()
        self.python, self.launcher = python, launcher
        self.record: dict = {"evidence": str(self.evidence), "processes": []}
        self.steps: list[tuple[str, bool, str]] = []
        self.contained = Containment()
        self.record["token"] = self.contained.token  # lets `cleanup` find this run's processes after a crash

    # Record -------------------------------------------------------------------------------------------------------
    def save(self) -> None:
        self.evidence.mkdir(parents=True, exist_ok=True)
        tmp = self.evidence / "launch.json.tmp"
        tmp.write_text(json.dumps(self.record, indent=1), encoding="utf-8")
        os.replace(tmp, self.evidence / "launch.json")

    def track(self, name: str, pid: int) -> None:
        if any(p["pid"] == pid for p in self.record["processes"]):
            return
        self.record["processes"].append({"name": name, "pid": pid, "created": created(pid)})
        self.save()

    def track_tree(self, name: str, pid: int) -> None:
        import psutil
        try:
            for child in psutil.Process(pid).children(recursive=True):
                self.track(f"{name} child", child.pid)
        except psutil.Error:
            pass

    def spawn(self, name: str, argv: list[str], log: Path, env: dict | None = None) -> subprocess.Popen:
        env = dict(env or os.environ, PYTHONUTF8="1")  # the logs are read back as UTF-8 whatever the code page
        with log.open("w", encoding="utf-8") as out:
            proc = self.contained.popen(argv, cwd=REPO, stdout=out, stderr=subprocess.STDOUT,
                                        stdin=subprocess.DEVNULL, env=env)
        self.track(name, proc.pid)
        return proc

    def wait_tracking(self, name: str, proc: subprocess.Popen, seconds: float) -> int:
        """Wait for a step, recording the processes it starts (npm, node, browsers) for the evidence. Containment,
        not this sampling, is what makes cleanup reach all of them."""
        deadline = time.monotonic() + seconds
        while proc.poll() is None:
            self.track_tree(name, proc.pid)
            if time.monotonic() > deadline:
                proc.kill()
                proc.wait(timeout=30)
                return -1
            time.sleep(0.25)
        return proc.returncode

    # Steps --------------------------------------------------------------------------------------------------------
    def launch(self) -> bool:
        """docs/ui-testing.md, "Start", with --log-dir in the evidence folder so the logs survive Teardown."""
        # A new empty directory per run (the launcher accepts an empty --dir), so no two runs ever share one.
        instance = Path(tempfile.mkdtemp(prefix="hub-verify-", dir=tempfile.gettempdir()))
        log_dir, out = self.evidence / "logs", self.evidence / "launcher.txt"
        self.record.update(instance_dir=str(instance), log_dir=str(log_dir), launcher_output=str(out))
        self.save()
        proc = self.spawn("launcher", [self.python, str(self.launcher), "start", "--dir", str(instance),
                                       "--log-dir", str(log_dir)], out)
        deadline = time.monotonic() + START_SECONDS
        while time.monotonic() < deadline:
            self.track_tree("launcher", proc.pid)
            ready = [line for line in out.read_text(encoding="utf-8", errors="replace").splitlines()
                     if line.startswith("READY ")]
            if ready:
                fields = json.loads(ready[0][6:])
                self.record.update(hub=fields["hub"], daemon=fields["daemon"],
                                   hub_port=urlsplit(fields["hub"]).port,
                                   daemon_port=urlsplit(fields["daemon"]).port)
                self.save()
                return self.step("launch", True, f"Hub {fields['hub']}, daemon {fields['daemon']}")
            if proc.poll() is not None:
                return self.step("launch", False, f"the launcher exited with code {proc.returncode}; see "
                                                  "launcher.txt")
            time.sleep(0.25)
        return self.step("launch", False, f"no READY line within {START_SECONDS} s; see launcher.txt")

    def doctor_argv(self) -> list[str]:
        return [self.python, str(DOCTOR), "--dir", self.record["instance_dir"],
                "--expect-port", str(self.record["hub_port"])]

    def doctor(self) -> bool:
        out = self.evidence / "doctor.txt"
        code = self.wait_tracking("doctor", self.spawn("doctor", self.doctor_argv(), out), STEP_SECONDS)
        failed = [line for line in out.read_text(encoding="utf-8", errors="replace").splitlines()
                  if line.startswith("FAIL")]
        return self.step("doctor", code == 0, "all checks pass" if code == 0 else "; ".join(failed) or f"exit {code}")

    def drive_argv(self, features: list[str]) -> list[str]:
        # Playwright's CLI under node itself: npm.cmd would pass the --grep "|" through cmd.exe on Windows.
        node = shutil.which("node") or "node"
        argv = [node, str(UI_TESTS / "node_modules" / "@playwright" / "test" / "cli.js"), "test", "--config",
                str(UI_TESTS / "verify.config.mjs"), "--workers=1"]
        return argv + (["--grep", "|".join(f"@{f}" for f in features)] if features else [])

    def drive(self, features: list[str]) -> bool:
        env = dict(os.environ, HUB_UI_URL=self.record["hub"], HUB_UI_DAEMON_URL=self.record["daemon"],
                   HUB_UI_DIR=self.record["instance_dir"], HUB_UI_PYTHON=self.python,
                   VERIFY_EVIDENCE_DIR=str(self.evidence))
        out = self.evidence / "drive.txt"
        code = self.wait_tracking("drive", self.spawn("drive", self.drive_argv(features), out, env), STEP_SECONDS)
        what = ", ".join(features) if features else "main path (" + ", ".join(FEATURES) + ")"
        return self.step("drive", code == 0, what + ("" if code == 0 else f": exit {code}; see drive.txt"))

    def cleanup(self) -> bool:
        """docs/ui-testing.md, "Teardown", then every recorded process, then the contract's leftover checks."""
        lines = []
        instance = self.record.get("instance_dir")
        if instance:
            lines.append("teardown: " + self.teardown(instance))
        import psutil
        contained = self.contained.kill()
        procs = [p for p in map(alive, self.record["processes"]) if p is not None]
        for proc in procs:
            with contextlib.suppress(psutil.Error):
                proc.kill()
        psutil.wait_procs(procs, timeout=15)
        lines.append(f"killed {contained} contained and {len(procs)} recorded processes still running")
        if instance and Path(instance).exists():  # Teardown failed, or a process it could not stop held the dir
            lines.append("teardown again: " + self.teardown(instance))
        problems = []
        left = [f"{p['name']} {p['pid']}" for p in self.record["processes"] if alive(p) is not None]
        if left:
            problems.append("still running: " + ", ".join(left))
        if remaining := self.contained.remaining():
            problems.append(f"{remaining} processes this run started still run")
        for key in ("hub_port", "daemon_port"):
            if self.record.get(key) and port_open(self.record[key]):
                problems.append(f"port {self.record[key]} still accepts connections")
        if instance and Path(instance).exists():
            problems.append(f"{instance} still exists")
        lines += problems or ["no recorded process, port or instance directory is left"]
        (self.evidence / "cleanup.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
        return self.step("cleanup", not problems, "; ".join(problems) or "nothing left behind")

    def teardown(self, instance: str) -> str:
        try:
            result = subprocess.run([self.python, str(self.launcher), "stop", "--dir", instance],  # noqa: S603
                                    cwd=REPO, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                    env=dict(os.environ, PYTHONUTF8="1"), timeout=180)
        except (subprocess.TimeoutExpired, OSError) as e:
            return f"{type(e).__name__}; the recorded processes are killed instead"
        return f"exit {result.returncode} {(result.stdout + result.stderr).strip()}"

    def step(self, name: str, ok: bool, detail: str) -> bool:
        self.steps.append((name, ok, detail))
        print(f"{'PASS' if ok else 'FAIL'} {name}: {detail}", flush=True)
        return ok

    # Evidence -----------------------------------------------------------------------------------------------------
    def log_excerpts(self) -> list[str]:
        lines = []
        for log in sorted((self.evidence / "logs").glob("*.log")):
            text = log.read_text(encoding="utf-8", errors="replace").splitlines()
            notable = [line for line in text if any(w in line for w in ("FAKE MODEL MODE", "ERROR", "Traceback"))]
            lines += [f"- `{log.name}` ({len(text)} lines): " + (f"{len(notable)} notable lines" if notable else
                                                                 "nothing notable")]
            lines += [f"  - `{line.strip()[:160]}`" for line in notable[:8]]
        return lines or ["- no instance logs"]

    def passed(self) -> bool:
        ran = {name for name, _, _ in self.steps}
        return set(STEPS) <= ran and all(ok for _, ok, _ in self.steps)

    def summary(self, features: list[str]) -> Path:
        ok = self.passed()
        shots = sorted(p.relative_to(self.evidence).as_posix() for p in self.evidence.glob("screenshots/*.png"))
        doc = [f"# Verify: {'PASS' if ok else 'FAIL'}", "",
               f"- Run: `{self.evidence.name}`; features: {', '.join(features) or 'main path (all)'}",
               f"- Instance: Hub {self.record.get('hub', '-')}, daemon {self.record.get('daemon', '-')} "
               "(fake model daemon, synthetic seed, loopback only)", "",
               "| Step | Result | Detail |", "| --- | --- | --- |"]
        doc += [f"| {name} | {'PASS' if passed else 'FAIL'} | {detail} |" for name, passed, detail in self.steps]
        doc += ["", "## Screenshots", ""] + ([f"- `{s}`" for s in shots] or ["- none"])
        doc += ["", "## Log excerpts", ""] + self.log_excerpts()
        doc += ["", "Files: `launch.json`, `launcher.txt`, `doctor.txt`, `drive.txt`, `cleanup.txt`, `logs/`."]
        path = self.evidence / "summary.md"
        path.write_text("\n".join(doc) + "\n", encoding="utf-8")
        return path

    def run(self, features: list[str]) -> int:
        self.save()
        interrupted: BaseException | None = None
        try:
            if self.launch() and self.doctor():
                self.drive(features)
        except BaseException as e:  # noqa: BLE001 - Ctrl+C included: record it, clean up, then re-raise below
            interrupted = e
            self.step("interrupted", False, f"{type(e).__name__} stopped the run before it finished")
        finally:
            self.cleanup()
            print(f"evidence: {self.summary(features)}", flush=True)
        if interrupted is not None and not isinstance(interrupted, KeyboardInterrupt):
            raise interrupted
        return 0 if self.passed() else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="verify.py", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="command", required=True)
    r = sub.add_parser("run", help="launch, doctor, drive, evidence and cleanup")
    r.add_argument("--feature", action="append", choices=FEATURES, default=[],
                   help="drive only this feature (repeatable; default: the whole main path)")
    r.add_argument("--evidence", type=Path, help="evidence folder (default: .verify/<UTC timestamp>/)")
    c = sub.add_parser("cleanup", help="clean up after a run that was killed, from its launch.json")
    c.add_argument("--evidence", type=Path, required=True)
    args = ap.parse_args(argv)
    if args.command == "run":
        evidence = args.evidence or REPO / ".verify" / run_name()
        if evidence.exists() and any(evidence.iterdir()):
            r.error(f"{evidence} is not empty: each run needs its own evidence folder")
        return Run(evidence).run(args.feature)
    run = Run(args.evidence)
    run.record = json.loads((run.evidence / "launch.json").read_text(encoding="utf-8"))
    run.contained.token = run.record.get("token") or run.contained.token
    return 0 if run.cleanup() else 1


if __name__ == "__main__":
    sys.exit(main())
