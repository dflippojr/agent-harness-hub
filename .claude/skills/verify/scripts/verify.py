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


def now() -> str:
    return datetime.now(UTC).strftime("%Y%m%d-%H%M%S")


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


class Run:
    def __init__(self, evidence: Path, python: str = sys.executable, launcher: Path = LAUNCHER):
        self.evidence = evidence.resolve()
        self.python, self.launcher = python, launcher
        self.record: dict = {"evidence": str(self.evidence), "processes": []}
        self.steps: list[tuple[str, bool, str]] = []

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
        flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        with log.open("w", encoding="utf-8") as out:
            proc = subprocess.Popen(argv, cwd=REPO, stdout=out, stderr=subprocess.STDOUT,  # noqa: S603
                                    stdin=subprocess.DEVNULL, env=env, creationflags=flags)
        self.track(name, proc.pid)
        return proc

    def wait_tracking(self, name: str, proc: subprocess.Popen, seconds: float) -> int:
        """Wait for a step, recording every process it starts (npm, node, browsers) while it runs."""
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
        instance = Path(tempfile.gettempdir()) / f"hub-verify-{self.evidence.name}"
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
        failed = [line for line in out.read_text(encoding="utf-8").splitlines() if line.startswith("FAIL")]
        return self.step("doctor", code == 0, "all checks pass" if code == 0 else "; ".join(failed) or f"exit {code}")

    def drive_argv(self, features: list[str]) -> list[str]:
        npm = shutil.which("npm") or "npm"
        argv = [npm, "--prefix", str(UI_TESTS), "exec", "--", "playwright", "test", "--config",
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
            result = subprocess.run([self.python, str(self.launcher), "stop", "--dir", instance],  # noqa: S603
                                    cwd=REPO, capture_output=True, text=True, timeout=180)
            lines.append(f"teardown: exit {result.returncode} {(result.stdout + result.stderr).strip()}")
        import psutil
        procs = [p for p in map(alive, self.record["processes"]) if p is not None]
        for proc in procs:
            with contextlib.suppress(psutil.Error):
                proc.kill()
        psutil.wait_procs(procs, timeout=15)
        lines.append(f"killed {len(procs)} recorded processes still running" if procs else
                     "no recorded process was still running")
        problems = []
        left = [f"{p['name']} {p['pid']}" for p in self.record["processes"] if alive(p) is not None]
        if left:
            problems.append("still running: " + ", ".join(left))
        for key in ("hub_port", "daemon_port"):
            if self.record.get(key) and port_open(self.record[key]):
                problems.append(f"port {self.record[key]} still accepts connections")
        if instance and Path(instance).exists():
            problems.append(f"{instance} still exists")
        lines += problems or ["no recorded process, port or instance directory is left"]
        (self.evidence / "cleanup.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
        return self.step("cleanup", not problems, "; ".join(problems) or "nothing left behind")

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

    def summary(self, features: list[str]) -> Path:
        ok = all(passed for _, passed, _ in self.steps)
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
        try:
            if self.launch() and self.doctor():
                self.drive(features)
        finally:
            self.cleanup()
            print(f"evidence: {self.summary(features)}", flush=True)
        return 0 if all(passed for _, passed, _ in self.steps) else 1


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
        return Run(args.evidence or REPO / ".verify" / now()).run(args.feature)
    run = Run(args.evidence)
    run.record = json.loads((run.evidence / "launch.json").read_text(encoding="utf-8"))
    return 0 if run.cleanup() else 1


if __name__ == "__main__":
    sys.exit(main())
