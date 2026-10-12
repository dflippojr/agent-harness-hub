"""A disposable Hub plus Agent Harness Server in fake model mode, for UI testing (docs/ui-testing.md).

    python scripts/ui_instance.py start --dir DIR [--daemon-src PATH] [--port N] [--daemon-port N]
                                        [--unclaimed] [--no-seed] [--keep] [--log-dir PATH]
    python scripts/ui_instance.py signin-link --dir DIR
    python scripts/ui_instance.py approve --dir DIR --id REQUEST_ID --match CODE
    python scripts/ui_instance.py stop --dir DIR

Start runs a real daemon from an agent-harness checkout in fake model mode (scripted model, its own config and data
under DIR, loopback only), then a Hub with its own state directory and claim file pointed at it. Unless --unclaimed,
the "host" approves the claim with the daemon's own CLI; unless --no-seed, it adds synthetic apps, pairing requests,
marketplace entries and a member. It prints one READY JSON line (no secrets) and stays in the foreground: Ctrl+C stops
both servers and deletes DIR unless --keep. `stop` ends the instance recorded in DIR from another shell and deletes
DIR; it is safe to repeat, and after a partial start.

Children get an allowlisted environment with HOME pointed inside DIR, so no real token, provider key or tailnet
identity can reach them. Nothing here reads the real daemon's config or data, or talks to :8100 or a *.ts.net URL.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import hashlib
import json
import os
import secrets
import shutil
import signal
import socket
import stat
import subprocess
import sys
import time
from pathlib import Path

import httpx

HANDLE = "instance.json"
MARKER = "hub-ui-instance"          # in DIR: what lets `stop` delete it
DAEMON_MARKER = "fake-model-instance"  # in the daemon data_dir: what lets the daemon run in fake model mode
FAKE_ENV = "HARNESS_FAKE_MODEL"
LIVE_PORTS = {8100: "the live daemon's port", 8120: "the live Hub's default port"}
START_SECONDS = 90
# The variables a child process needs to run, and nothing else: tokens, provider keys and proxies stay out.
ENV_ALLOW = ("PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "TEMP", "TMP", "TMPDIR",
             "LANG", "LC_ALL", "TZ", "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "VIRTUAL_ENV")
SYNTHETIC_PAIRINGS = (("Synthetic notes", ["sessions"]), ("Synthetic approver", ["sessions", "approvals"]))
SYNTHETIC_MEMBER = {"login": "member@example.test", "display_name": "Synthetic member"}


class StartError(RuntimeError):
    pass


def free_port(requested: int = 0) -> int | None:
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", requested))
        except OSError:
            return None
        return s.getsockname()[1]


def cli_config(root: Path) -> Path:
    """The daemon CLI's client config: it must live under HOME/.agent-harness, and HOME is inside DIR."""
    path = root / "home" / ".agent-harness"
    path.mkdir(parents=True, exist_ok=True)
    return path / "harness-cli.json"


def child_env(root: Path, **extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k.upper() in ENV_ALLOW}
    home = root / "home"
    home.mkdir(exist_ok=True)
    env.update(HOME=str(home), USERPROFILE=str(home), APPDATA=str(home / "AppData" / "Roaming"),
               LOCALAPPDATA=str(home / "AppData" / "Local"), PYTHONUTF8="1", NO_PROXY="127.0.0.1,localhost")
    env.update(extra)
    return env


def write_json(path: Path, value) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(value, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def write_daemon_config(root: Path, port: int) -> tuple[Path, Path]:
    """The daemon's throwaway config (JSON is YAML), as harness.fake_instance writes it but with the Hub module on."""
    config_dir, data_dir, repos = root / "daemon" / "config", root / "daemon" / "data", root / "daemon" / "repos"
    for d in (config_dir, data_dir, repos):
        d.mkdir(parents=True, exist_ok=True)
    (data_dir / DAEMON_MARKER).write_text("disposable Hub UI-testing instance (scripts/ui_instance.py)\n",
                                          encoding="utf-8")
    config = {
        "listen": {"host": "127.0.0.1", "port": port},
        "data_dir": data_dir.as_posix(),
        "repos_dir": repos.as_posix(),
        "default_model": "fake",
        "models": {"fake": {"base_url": "http://127.0.0.1:9/v1"}},
        "modules": {"homelab": False, "memory_library": False, "images": False, "image_edit": False,
                    "gpu_guard": False, "endpoint": False, "web": False, "notifications": False,
                    "backup": False, "remote_control": False, "hub": True},
        "notify": {"enabled": False},
        # Household members need an owner allowlist. This synthetic login can never arrive: the Hub trusts no
        # tailnet identity here (no tailscaled peer, no --trust-unverified-identity-headers).
        "allowed_logins": ["owner@example.test"],
        "jobs": {"enabled": True},
        "canary": {"enabled": False},
    }
    projects = {"projects": {"ui": {"description": "Hub UI testing: an empty workspace."}}}
    (config_dir / "harness.yaml").write_text(json.dumps(config, indent=1), encoding="utf-8")
    (config_dir / "projects.yaml").write_text(json.dumps(projects, indent=1), encoding="utf-8")
    return config_dir, data_dir


def wait(check, seconds: float, what: str, procs=()):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        for name, proc in procs:
            if proc.poll() is not None:
                raise StartError(f"the {name} exited with code {proc.returncode} (see its log)")
        try:
            value = check()
        except (httpx.HTTPError, OSError, ValueError):
            value = None
        if value:
            return value
        time.sleep(0.25)
    raise StartError(f"timed out waiting for {what}")


class Instance:
    def __init__(self, args, root: Path):
        self.args, self.root = args, root
        self.src = Path(args.daemon_src).resolve()
        self.log_dir = Path(args.log_dir).resolve() if args.log_dir else root / "logs"
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.procs: list[tuple[str, subprocess.Popen]] = []
        self.logs = []
        self.state: dict = {"status": "starting", "dir": str(root), "pid": os.getpid(), "children": []}

    # Processes ------------------------------------------------------------------------------------------------
    def spawn(self, name: str, argv: list[str], env: dict, cwd: Path | None = None) -> subprocess.Popen:
        log = (self.log_dir / f"{name}.log").open("w", encoding="utf-8")
        self.logs.append(log)
        flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        proc = subprocess.Popen(argv, cwd=cwd, env=env, stdout=log, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, creationflags=flags)
        self.procs.append((name, proc))
        self.state["children"].append({"name": name, "pid": proc.pid, "created": process_created(proc.pid)})
        write_json(self.root / HANDLE, self.state)
        return proc

    def close(self) -> None:
        for _, proc in reversed(self.procs):
            if proc.poll() is None:
                proc.terminate()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=15)
        for log in self.logs:
            log.close()

    # Start ----------------------------------------------------------------------------------------------------
    def start(self) -> None:
        a = self.args
        daemon_port, hub_port = free_port(a.daemon_port), free_port(a.port)
        if daemon_port is None or hub_port is None:
            raise StartError("a port is in use; this command never reuses a running server")
        self.daemon_url, self.hub_url = f"http://127.0.0.1:{daemon_port}", f"http://127.0.0.1:{hub_port}"
        self.state.update(daemon=self.daemon_url, hub=self.hub_url, logs=str(self.log_dir))
        config_dir, _ = write_daemon_config(self.root, daemon_port)
        self.daemon_env = child_env(self.root, HARNESS_CONFIG_DIR=str(config_dir), HARNESS_URL=self.daemon_url)
        fixtures = self.src / "harness" / "fake_fixtures"
        self.spawn("daemon", [sys.executable, "-m", "harness", "--config-dir", str(config_dir)],
                   dict(self.daemon_env, **{FAKE_ENV: str(fixtures)}), cwd=self.src)
        with httpx.Client(timeout=5, trust_env=False) as http:
            wait(lambda: http.get(self.daemon_url + "/health").status_code == 200, START_SECONDS,
                 "the daemon to start", self.procs)

        self.state_dir = self.root / "hub"
        claim_file = self.state_dir / "claim.json"
        argv = [sys.executable, "-m", "harness_hub", "--daemon-url", self.daemon_url, "--state-dir",
                str(self.state_dir), "--host", "127.0.0.1", "--port", str(hub_port), "--name", "UI test Hub"]
        if not a.unclaimed:
            argv += ["--claim-file", str(claim_file)]
        self.spawn("hub", argv, child_env(self.root))
        self.http = httpx.Client(base_url=self.hub_url, timeout=10, trust_env=False,
                                 headers={"X-Hub-Request": "1", "Origin": self.hub_url})
        try:
            wait(lambda: self.http.get("/healthz").status_code == 200, START_SECONDS, "the Hub to start", self.procs)
            if not a.unclaimed:
                published = wait(lambda: json.loads(claim_file.read_text(encoding="utf-8")), START_SECONDS,
                                 "the claim file", self.procs)
                self.host_cli("hub", "approve", published["id"], "--match", published["match_code"])
                self.sign_in()
                wait(lambda: self.http.get("/api/claim").json().get("state") == "claimed", START_SECONDS,
                     "the Hub to redeem its key", self.procs)
                if not a.no_seed:
                    self.seed()
        finally:
            self.http.close()
        self.state.update(status="ready", claimed=not a.unclaimed, seeded=not (a.unclaimed or a.no_seed))
        write_json(self.root / HANDLE, self.state)

    def host_cli(self, *argv: str) -> str:
        """`harness ...` on the daemon "host" with a client config inside DIR: what the owner runs to approve."""
        client = cli_config(self.root)
        result = subprocess.run([sys.executable, "-m", "harness.cli", "--config", str(client), *argv], cwd=self.src,
                                env=self.daemon_env, capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            raise StartError(f"harness {argv[0]} {argv[1] if len(argv) > 1 else ''} failed: "
                             f"{(result.stderr or result.stdout)[-500:]}")
        return result.stdout

    def sign_in(self) -> None:
        from harness_hub.auth import create_signin_code
        response = self.http.post("/api/session", json={"code": create_signin_code(self.state_dir)})
        if response.status_code != 204:
            raise StartError(f"seeding sign-in failed: HTTP {response.status_code}")
        self.http.headers["X-Hub-Session"] = response.headers["x-hub-session"]

    def seed(self) -> None:
        """Synthetic rows for every page. Names are fixed; ids are random, so journeys find rows by name."""
        self.host_cli("keys", "create", "Synthetic App", "--kind", "app")  # its token is printed only to this pipe
        self.host_cli("hub", "entries", "init")
        for name, scopes in SYNTHETIC_PAIRINGS:
            challenge = base64.urlsafe_b64encode(hashlib.sha256(secrets.token_bytes(32)).digest()).rstrip(b"=")
            asked = httpx.post(self.daemon_url + "/api/v1/pair/requests", timeout=10, trust_env=False, json={
                "name": name, "scopes": scopes, "catalog_app_id": "test.example.notes",
                "code_challenge": challenge.decode(), "code_challenge_method": "S256"})
            if asked.status_code != 201:
                raise StartError(f"pairing request {name!r}: HTTP {asked.status_code}")
        member = self.http.post("/api/manage/account_add", json=SYNTHETIC_MEMBER)
        if member.status_code != 201:
            raise StartError(f"member: HTTP {member.status_code} {member.text[:200]}")

    def watch(self) -> int:
        while True:
            for name, proc in self.procs:
                if proc.poll() is not None:
                    print(f"FAILED the {name} exited with code {proc.returncode}", file=sys.stderr, flush=True)
                    return 1
            time.sleep(0.5)


def process_created(pid: int) -> float | None:
    try:
        import psutil
        return psutil.Process(pid).create_time()
    except Exception:  # noqa: BLE001 - only a guard against pid reuse; missing psutil leaves it unset
        return None


def owned(root: Path) -> bool:
    return (root / MARKER).is_file() or (root.is_dir() and not any(root.iterdir()))


def remove(root: Path) -> bool:
    def writable(func, path, _exc):
        os.chmod(path, stat.S_IWRITE)
        func(path)
    for _ in range(20):  # SQLite handles and git objects can linger a moment on Windows
        try:
            shutil.rmtree(root, onexc=writable)
            return True
        except FileNotFoundError:
            return True
        except OSError:
            time.sleep(0.5)
    return not root.exists()


def kill_recorded(handle: dict) -> bool:
    """Kill the recorded launcher and children, but only the same processes (start times match), and their trees."""
    targets = [t for t in [{"pid": handle.get("pid"), "created": handle.get("pid_created")},
                           *handle.get("children", [])] if isinstance(t, dict) and t.get("pid")]
    if not targets:
        return True
    import psutil  # a daemon requirement, so present wherever an instance ran
    procs = []
    for t in targets:
        try:
            proc = psutil.Process(int(t["pid"]))
            if t.get("created") is None or abs(proc.create_time() - float(t["created"])) > 0.01:
                continue
            procs += proc.children(recursive=True) + [proc]
        except (psutil.NoSuchProcess, KeyError, TypeError, ValueError):
            continue
    for p in procs:
        with contextlib.suppress(psutil.NoSuchProcess):
            p.kill()
    _, alive = psutil.wait_procs(procs, timeout=15)
    alive = [p for p in alive if not exited(p)]
    for p in alive:
        print(f"process {p.pid} is still running", file=sys.stderr)
    return not alive


def exited(proc) -> bool:
    """Gone, or a zombie: it has exited and only waits for its parent (the test runner, say) to reap it."""
    import psutil
    try:
        return proc.status() == psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return True


def stop(root: Path) -> int:
    root = root.resolve()
    if not root.exists():
        print(f"nothing to stop: {root} does not exist")
        return 0
    if not owned(root):
        print(f"refusing: {root} is not a Hub UI-testing instance directory", file=sys.stderr)
        return 2
    try:
        handle = json.loads((root / HANDLE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        handle = {}
    if isinstance(handle, dict) and not kill_recorded(handle):
        print(f"could not stop every process; {root} is left in place", file=sys.stderr)
        return 1
    if not remove(root):
        print(f"could not delete {root}; is a process still using it?", file=sys.stderr)
        return 1
    print(f"deleted {root}")
    return 0


def read_handle(root: Path) -> dict:
    handle = json.loads((root.resolve() / HANDLE).read_text(encoding="utf-8"))
    if handle.get("status") != "ready":
        raise SystemExit(f"{root} is not a ready instance")
    return handle


def signin_link(root: Path) -> int:
    from harness_hub.auth import create_signin_code
    handle = read_handle(root)
    print(f"{handle['hub']}/#signin={create_signin_code(root.resolve() / 'hub')}")
    return 0


def approve(args) -> int:
    root = args.dir.resolve()
    handle = read_handle(root)
    env = child_env(root, HARNESS_CONFIG_DIR=str(root / "daemon" / "config"), HARNESS_URL=handle["daemon"])
    client = cli_config(root)
    result = subprocess.run([sys.executable, "-m", "harness.cli", "--config", str(client), "hub", "approve", args.id,
                             "--match", args.match], cwd=Path(args.daemon_src).resolve(), env=env, timeout=60)
    return result.returncode


def _interrupt(_sig, _frame):
    raise KeyboardInterrupt


def start(args, ap) -> int:
    src = Path(args.daemon_src or "").resolve()
    if not args.daemon_src or not (src / "harness" / "fake_model.py").is_file():
        ap.error("--daemon-src (or HUB_UI_DAEMON_SRC) must name an agent-harness checkout with fake model mode")
    for flag, port in (("--port", args.port), ("--daemon-port", args.daemon_port)):
        if port in LIVE_PORTS:
            ap.error(f"{flag} {port} is {LIVE_PORTS[port]}; a UI-testing instance never uses it")
        if port and free_port(port) is None:
            ap.error(f"{flag} {port} is in use; this command never reuses a running server")
    if args.dir.exists() and any(args.dir.iterdir()):
        ap.error(f"{args.dir} is not empty (stop the instance there first: python scripts/ui_instance.py stop "
                 f"--dir {args.dir})")
    args.dir.mkdir(parents=True, exist_ok=True)
    root = args.dir.resolve()
    (root / MARKER).write_text("disposable Hub UI-testing instance (scripts/ui_instance.py)\n", encoding="utf-8")
    for name in ("SIGBREAK", "SIGTERM"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), _interrupt)
    instance = Instance(args, root)
    instance.state["pid_created"] = process_created(os.getpid())
    code = 0
    try:
        instance.start()
        ready = {k: instance.state[k] for k in ("hub", "daemon", "dir", "pid", "claimed", "seeded")}
        print("Hub UI-testing instance is up (fake model daemon, synthetic data, loopback only).\n"
              f"  Hub:          {instance.hub_url}   (sign in: python scripts/ui_instance.py signin-link "
              f"--dir \"{root}\")\n"
              f"  Daemon:       {instance.daemon_url}   (fake model mode)\n"
              f"  Logs:         {instance.log_dir}\n"
              f"  Instance dir: {root}" + ("   (kept on exit)" if args.keep else "   (deleted on exit)") + "\n"
              "READY " + json.dumps(ready) + "\nCtrl+C to stop.", flush=True)
        code = instance.watch()
    except KeyboardInterrupt:
        pass
    except StartError as e:
        print(f"FAILED {e}", file=sys.stderr, flush=True)
        code = 1
    finally:
        instance.close()
        if not args.keep and not remove(root):
            print(f"could not delete {root}; remove it by hand", file=sys.stderr)
    return code


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    ap = argparse.ArgumentParser(prog="python scripts/ui_instance.py", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="command", required=True)
    default_src = os.environ.get("HUB_UI_DAEMON_SRC") or os.environ.get("HUB_E2E_DAEMON_SRC") or ""
    s = sub.add_parser("start", help="start a fresh instance in the foreground")
    s.add_argument("--dir", type=Path, required=True, help="instance directory; must be empty or absent")
    s.add_argument("--daemon-src", default=default_src, help="agent-harness checkout (default: $HUB_UI_DAEMON_SRC)")
    s.add_argument("--port", type=int, default=0, help="Hub port (default: a free one); refused if in use")
    s.add_argument("--daemon-port", type=int, default=0, help="daemon port (default: a free one); refused if in use")
    s.add_argument("--unclaimed", action="store_true", help="leave the claim to the UI (claim journey); no seed")
    s.add_argument("--no-seed", action="store_true", help="claim, but add no synthetic rows")
    s.add_argument("--keep", action="store_true", help="keep the instance directory on exit")
    s.add_argument("--log-dir", help="where daemon.log and hub.log go (default: DIR/logs)")
    for name, text in (("stop", "stop the instance in DIR and delete DIR"),
                       ("signin-link", "print a one-time sign-in link")):
        sub.add_parser(name, help=text).add_argument("--dir", type=Path, required=True)
    a = sub.add_parser("approve", help="approve a claim on the daemon host, as the owner would")
    a.add_argument("--dir", type=Path, required=True)
    a.add_argument("--id", required=True)
    a.add_argument("--match", required=True)
    a.add_argument("--daemon-src", default=default_src)
    args = ap.parse_args(argv)
    if args.command == "start":
        return start(args, s)
    if args.command == "stop":
        return stop(args.dir)
    if args.command == "signin-link":
        return signin_link(args.dir)
    return approve(args)


if __name__ == "__main__":
    sys.exit(main())
