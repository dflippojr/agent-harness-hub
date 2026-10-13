"""Doctor: is this disposable Hub UI-testing instance ready, loopback-only, seeded and isolated?

    python .claude/skills/verify/scripts/doctor.py --dir <instance-dir> [--expect-port N]

Prints one PASS or FAIL line per check and exits 1 if any check fails. It reads the instance's own handle
(`instance.json`), the Hub and daemon on their loopback URLs, and the recorded processes; it never starts, stops or
changes anything apart from minting one sign-in link to read the seed. See docs/ui-testing.md, "Scope and isolation"
and "Seed and sign-in", for what each check expects.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import httpx

LOOPBACK = "127.0.0.1"
LIVE_PORTS = {8100, 8120}  # docs/ui-testing.md, "Scope and isolation": never used by an instance
# docs/ui-testing.md, "Seed and sign-in": the rows every default start creates, found by name.
SEED = {
    "/api/apps": ("Synthetic App",),
    "/api/pairing": ("Synthetic notes", "Synthetic approver"),
    "/api/manage/accounts": ("member@example.test",),
}
# Variable names that carry credentials. The launcher's allowlist keeps them from the instance's processes.
SECRET_NAME = re.compile(r"TOKEN|SECRET|PASSWORD|PASSWD|API_?KEY|AUTH|CREDENTIAL|PRIVATE_KEY|TS_AUTHKEY|NTFY", re.I)
# Variables that name a data location; in the instance's processes each must point inside the instance directory.
PATH_VARS = ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "HARNESS_CONFIG_DIR")


@dataclass
class Check:
    name: str
    ok: bool
    detail: str

    def line(self) -> str:
        return f"{'PASS' if self.ok else 'FAIL'} {self.name}: {self.detail}"


def _url_port(url: str) -> tuple[str | None, int | None]:
    parts = urlsplit(url or "")
    return parts.hostname, parts.port


def check_ready(handle: dict, http: httpx.Client) -> Check:
    """Ready means the handle says so (the contract's readiness signal) and both servers answer their health URLs."""
    if handle.get("status") != "ready":
        return Check("ready", False, f"instance.json status is {handle.get('status')!r}, not 'ready'")
    for label, url in (("Hub", f"{handle.get('hub')}/healthz"), ("daemon", f"{handle.get('daemon')}/health")):
        try:
            status = http.get(url).status_code
        except httpx.HTTPError as e:
            return Check("ready", False, f"{label} {url} did not answer ({type(e).__name__})")
        if status != 200:
            return Check("ready", False, f"{label} {url} answered HTTP {status}")
    return Check("ready", True, "instance.json is ready; Hub /healthz and daemon /health answer 200")


def check_loopback(handle: dict, listeners: Iterable[tuple[str, int]], expect_port: int | None = None) -> Check:
    """Both URLs are 127.0.0.1 on non-live ports, the Hub on the expected port, and every socket the instance's
    processes listen on is bound to 127.0.0.1. `listeners` is (ip, port) for each listening socket they own."""
    ports = []
    for label in ("hub", "daemon"):
        host, port = _url_port(handle.get(label, ""))
        if host != LOOPBACK or port is None:
            return Check("loopback", False, f"{label} URL {handle.get(label)!r} is not http://{LOOPBACK}:<port>")
        if port in LIVE_PORTS:
            return Check("loopback", False, f"{label} uses live port {port}")
        ports.append(port)
    if expect_port is not None and ports[0] != expect_port:
        return Check("loopback", False, f"Hub is on port {ports[0]}, expected {expect_port}")
    bound = list(listeners)
    exposed = sorted({f"{ip}:{port}" for ip, port in bound if ip != LOOPBACK})
    if exposed:
        return Check("loopback", False, f"the instance listens beyond loopback: {', '.join(exposed)}")
    missing = [p for p in ports if p not in {port for _, port in bound}]
    if missing:
        return Check("loopback", False, f"no recorded process listens on {', '.join(map(str, missing))}")
    return Check("loopback", True, f"Hub :{ports[0]} and daemon :{ports[1]} listen on {LOOPBACK} only")


def check_seed(http: httpx.Client) -> Check:
    """The Hub is claimed and the synthetic seed rows are present. `http` is a signed-in client on the Hub."""
    claim = http.get("/api/claim")
    if claim.status_code != 200 or claim.json().get("state") != "claimed":
        return Check("seed", False, f"the Hub is not claimed (/api/claim: HTTP {claim.status_code})")
    absent = []
    for path, names in SEED.items():
        reply = http.get(path)
        text = reply.text if reply.status_code == 200 else ""
        absent += [f"{name} ({path})" for name in names if name not in text]
    if absent:
        return Check("seed", False, "missing synthetic seed: " + ", ".join(absent))
    return Check("seed", True, "claimed; " + ", ".join(n for names in SEED.values() for n in names) + " present")


def _inside(path: str, root: Path) -> bool:
    try:
        return Path(path).resolve().is_relative_to(root)
    except (OSError, ValueError):
        return False


def check_isolation(root: Path, processes: Iterable[tuple[str, dict, list[str]]]) -> Check:
    """No credential reaches the instance's processes and every data location they use is inside the instance
    directory: environment paths, the Hub's --state-dir and the daemon config's data_dir and repos_dir.
    `processes` is (name, environment, argv) for each recorded process."""
    root = root.resolve()
    problems = []
    seen = 0
    for name, env, argv in processes:
        seen += 1
        leaked = sorted(k for k, v in env.items() if v and SECRET_NAME.search(k))
        if leaked:
            problems.append(f"{name} has credential variables {', '.join(leaked)}")
        problems += [f"{name} {k}={env[k]} is outside the instance" for k in PATH_VARS
                     if env.get(k) and not _inside(env[k], root)]
        for flag in ("--state-dir", "--config-dir", "--claim-file"):
            if flag in argv[:-1] and not _inside(argv[argv.index(flag) + 1], root):
                problems.append(f"{name} {flag} is outside the instance")
    if not seen:
        problems.append("no recorded instance process could be inspected")
    try:
        config = json.loads((root / "daemon" / "config" / "harness.yaml").read_text(encoding="utf-8"))
        problems += [f"daemon {key} is outside the instance" for key in ("data_dir", "repos_dir")
                     if not _inside(config.get(key, ""), root)]
    except (OSError, ValueError):
        problems.append("the daemon config in the instance could not be read")
    if not (root / "daemon" / "data" / "fake-model-instance").is_file():
        problems.append("the daemon's fake-model-instance marker is missing")
    if problems:
        return Check("isolation", False, "; ".join(problems))
    return Check("isolation", True, f"{seen} processes: no credential variables, all data paths inside {root}")


# Live inspection ---------------------------------------------------------------------------------------------------
def recorded_processes(handle: dict) -> list:
    """The recorded children and their descendants (a venv's python.exe is a launcher for the real one), only
    while each still has its recorded start time."""
    import psutil
    found = []
    for child in handle.get("children", []):
        try:
            proc = psutil.Process(int(child["pid"]))
            if child.get("created") is not None and abs(proc.create_time() - float(child["created"])) > 0.01:
                continue
            found += [(child["name"], p) for p in [proc, *proc.children(recursive=True)]]
        except (psutil.Error, KeyError, TypeError, ValueError):
            continue
    return found


def live_listeners(procs) -> list[tuple[str, int]]:
    import psutil
    bound = []
    for _, proc in procs:
        try:
            bound += [(c.laddr.ip, c.laddr.port) for c in proc.net_connections(kind="inet")
                      if c.status == psutil.CONN_LISTEN]
        except psutil.Error:
            continue
    return bound


def live_environments(procs) -> list[tuple[str, dict, list[str]]]:
    import psutil
    found = []
    for name, proc in procs:
        try:
            found.append((name, proc.environ(), proc.cmdline()))
        except psutil.Error:
            continue
    return found


def signed_in_client(root: Path, hub: str) -> httpx.Client:
    """A Hub client signed in with a fresh one-time code, as `ui_instance.py signin-link` mints it."""
    from harness_hub.auth import create_signin_code
    http = httpx.Client(base_url=hub, timeout=10, trust_env=False, headers={"X-Hub-Request": "1", "Origin": hub})
    reply = http.post("/api/session", json={"code": create_signin_code(root / "hub")})
    if reply.status_code == 204:
        http.headers["X-Hub-Session"] = reply.headers["x-hub-session"]
    return http


def run(root: Path, expect_port: int | None = None, out: Callable[[str], None] = print) -> int:
    root = root.resolve()
    try:
        handle = json.loads((root / "instance.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        out(f"FAIL ready: no readable instance.json in {root} ({type(e).__name__})")
        return 1
    checks = []
    with httpx.Client(timeout=5, trust_env=False) as http:
        checks.append(check_ready(handle, http))
    try:
        procs = recorded_processes(handle)
        checks.append(check_loopback(handle, live_listeners(procs), expect_port))
        checks.append(check_isolation(root, live_environments(procs)))
    except ImportError:
        checks += [Check(n, False, "psutil is needed to inspect the instance") for n in ("loopback", "isolation")]
    if checks[0].ok:
        http = signed_in_client(root, handle["hub"])
        try:
            checks.append(check_seed(http))
        except (httpx.HTTPError, ValueError) as e:
            checks.append(Check("seed", False, f"the Hub API did not answer as expected ({type(e).__name__})"))
        finally:
            http.close()
    else:
        checks.append(Check("seed", False, "skipped: the instance is not ready"))
    for check in checks:
        out(check.line())
    return 0 if all(c.ok for c in checks) else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="doctor.py", description=__doc__.split("\n\n")[0])
    ap.add_argument("--dir", type=Path, required=True, help="the instance directory given to ui_instance.py start")
    ap.add_argument("--expect-port", type=int, help="the Hub port the READY line reported")
    args = ap.parse_args(argv)
    return run(args.dir, args.expect_port)


if __name__ == "__main__":
    sys.exit(main())
