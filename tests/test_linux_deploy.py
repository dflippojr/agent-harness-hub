"""The Linux deployment (ops/hub/*.sh, docs/deployment.md), run end to end without systemd.

Each test runs the real bash scripts against a scratch HOME. A fake `systemctl` on PATH keeps the unit's state in files
and "starts" it by running the unit's ExecStart (the deployed run-hub.sh) once; a fake `python` stands in for the
interpreter, its virtual environments and the Hub, which writes status.json and the /healthz answer a fake `curl`
serves. Git is real: each test has a release checkout with a local bare `origin`.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
OPS = ROOT / "ops" / "hub"
UNIT = "agent-harness-hub.service"
SHELL_SCRIPTS = sorted(OPS.glob("*.sh"))
EXECUTABLES = ("deploy-hub.sh", "check-hub.sh", "install-unit.sh", "uninstall-unit.sh", "run-hub.sh")

FAKE_SYSTEMCTL = r"""#!/usr/bin/env bash
# systemctl --user, kept in files under $FAKE_SYSTEMD. `start` runs the unit's ExecStart once, synchronously.
set -u
state=$FAKE_SYSTEMD
printf '%s\n' "$*" >>"$state/calls"
[[ ${1:-} == --user ]] && shift
command=${1:-}; shift || true
unit_file=$XDG_CONFIG_HOME/systemd/user/${1:-none}
case $command in
    daemon-reload) : ;;
    enable) [[ -f $unit_file ]] || { echo "Unit file $1 does not exist." >&2; exit 1; }; touch "$state/enabled" ;;
    disable) rm -f "$state/enabled" ;;
    is-enabled)
        if [[ -f $unit_file && -e $state/enabled ]]; then echo enabled
        elif [[ -f $unit_file ]]; then echo disabled; exit 1
        else exit 1; fi ;;
    is-active)
        current=$(cat "$state/active" 2>/dev/null || echo inactive)
        echo "$current"; [[ $current == active ]] ;;
    stop) [[ -e $state/fail-stop ]] && exit 1; echo inactive >"$state/active"; rm -f "$state"/healthz.* ;;
    start | restart)
        [[ -e $state/fail-start ]] && exit 1
        # As systemd: start on an active unit does nothing; restart always runs it again.
        [[ $command == start && $(cat "$state/active" 2>/dev/null) == active ]] && exit 0
        echo active >"$state/active"
        echo 4242 >"$state/prop.ExecMainStartTimestampMonotonic"
        environment=() exec_start=''
        while IFS= read -r line; do
            case $line in
                Environment=*) environment+=("${line#Environment=}") ;;
                EnvironmentFile=*)
                    while IFS= read -r assignment; do
                        [[ $assignment =~ ^[A-Z_]+= ]] && environment+=("$assignment")
                    done <"${line#EnvironmentFile=}" ;;
                ExecStart=*) exec_start=${line#ExecStart=} ;;
            esac
        done <"$unit_file"
        # shellcheck disable=SC2086
        env "${environment[@]}" $exec_start >>"$state/execstart.log" 2>&1 || true ;;
    show)
        shift
        property=''
        while (($#)); do [[ $1 == -p ]] && property=$2; shift; done
        cat "$state/prop.$property" 2>/dev/null || { [[ $property == Result ]] && echo success; } || true ;;
    *) echo "fake systemctl: unsupported $command" >&2; exit 2 ;;
esac
"""

FAKE_CURL = r"""#!/usr/bin/env bash
# curl -fsS --max-time 3 http://127.0.0.1:<port>/healthz: answers only while the fake unit is active.
url=${*: -1}
port=${url#http://127.0.0.1:}; port=${port%%/*}
[[ $(cat "$FAKE_SYSTEMD/active" 2>/dev/null) == active && -f $FAKE_SYSTEMD/healthz.$port ]] || exit 7
cat "$FAKE_SYSTEMD/healthz.$port"
"""

FAKE_LOGINCTL = """#!/usr/bin/env bash
echo yes
"""

# The interpreter, a virtual environment's python and the Hub. Markers in the archived commit steer it: VERSION is the
# version it reports, BROKEN makes the Hub exit at once, STALE makes it write an old status.json.
FAKE_PYTHON = r"""#!/usr/bin/env bash
set -u
venv=$(cd "$(dirname "$0")/.." && pwd)
case "$*" in
    "-m venv "*) mkdir -p "$3/bin" && cp "$0" "$3/bin/python" && exit 0 ;;
    *"-m pip install"*"--no-deps"*)
        source=${*: -1}
        cp "$source/VERSION" "$venv/version"
        [[ -f $source/BROKEN ]] && touch "$venv/broken"
        [[ -f $source/STALE ]] && touch "$venv/stale"
        exit 0 ;;
    *"-m pip install"*) exit 0 ;;
    "-I -m harness_hub --version") echo "harness_hub $(cat "$venv/version")"; exit 0 ;;
    "-I -c "*) echo True; exit 0 ;;
    "-u -m harness_hub "*)
        printf '%s\n' "$*" >>"$FAKE_SYSTEMD/hub-args"
        [[ -e $venv/broken ]] && { echo "the Hub failed to start" >&2; exit 1; }
        while (($#)); do
            case $1 in --state-dir) state_dir=$2 ;; --port) port=$2 ;; esac
            shift
        done
        written=$(date -u +%Y-%m-%dT%H:%M:%SZ)
        [[ -e $venv/stale ]] && written=2000-01-01T00:00:00Z
        mkdir -p "$state_dir"
        printf '{"version": "%s", "build": "%s", "written_at": "%s"}\n' \
            "$(cat "$venv/version")" "$HUB_BUILD_COMMIT" "$written" >"$state_dir/status.json"
        printf '{"ok":true,"version":"%s"}' "$(cat "$venv/version")" >"$FAKE_SYSTEMD/healthz.$port"
        exit 0 ;;
esac
echo "fake python: unsupported $*" >&2
exit 2
"""


@pytest.fixture(autouse=True)
def posix_bash():
    # The scripts are for a Linux host; Windows CI runs the PowerShell path's tests instead.
    if os.name == "nt" or shutil.which("bash") is None:
        pytest.skip("the Linux deployment scripts need bash on a POSIX host")


def write_executable(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8", newline="\n")
    path.chmod(0o755)
    return path


def git(cwd: Path, *args: str) -> str:
    identity = ["-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false"]
    return subprocess.run(["git", *identity, *args], cwd=cwd, check=True, capture_output=True,
                          text=True).stdout.strip()


@dataclass
class Host:
    """A scratch Linux host: HOME, a deploy directory, fake systemd, and a release checkout with an origin."""

    root: Path
    env: dict[str, str] = field(default_factory=dict)

    def __post_init__(self):
        self.home = self.root / "home"
        self.deploy = self.home / ".local" / "share" / "agent-harness-hub"
        self.systemd = self.root / "systemd"
        self.bin = self.root / "bin"
        self.release = self.root / "release"
        for directory in (self.home, self.systemd, self.bin):
            directory.mkdir(parents=True)
        write_executable(self.bin / "systemctl", FAKE_SYSTEMCTL)
        write_executable(self.bin / "curl", FAKE_CURL)
        write_executable(self.bin / "loginctl", FAKE_LOGINCTL)
        self.python = write_executable(self.bin / "python", FAKE_PYTHON)
        self.env = {
            **{key: value for key, value in os.environ.items() if not key.startswith(("HUB_", "XDG_"))},
            "HOME": str(self.home),
            "XDG_CONFIG_HOME": str(self.home / ".config"),
            "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
            "FAKE_SYSTEMD": str(self.systemd),
            "HUB_START_TIMEOUT": "2",
            "HUB_POLL_SECONDS": "0.1",
            "GIT_CONFIG_NOSYSTEM": "1",
        }
        self.unit_file = self.home / ".config" / "systemd" / "user" / UNIT
        # Nothing a test runs may reach the real user's unit or deployment.
        assert self.unit_file.is_relative_to(self.root) and self.deploy.is_relative_to(self.root)
        origin = self.root / "origin.git"
        git(self.root, "init", "-q", "--bare", "-b", "main", str(origin))
        git(self.root, "init", "-q", "-b", "main", str(self.release))
        git(self.release, "remote", "add", "origin", str(origin))

    def commit(self, version: str, *markers: str, push: bool = True, scripts: bool = True) -> str:
        """A commit carrying this checkout's ops/hub/*.sh (each tagged with VERSION, so a test can tell which commit's
        copy was deployed) and the markers that steer the fake Hub."""
        for name in ("BROKEN", "STALE"):
            (self.release / name).unlink(missing_ok=True)
        shutil.rmtree(self.release / "ops", ignore_errors=True)
        if scripts:
            (self.release / "ops" / "hub").mkdir(parents=True)
            for path in SHELL_SCRIPTS:
                text = path.read_text(encoding="utf-8") + f"# test build {version}\n"
                write_executable(self.release / "ops" / "hub" / path.name, text)
        (self.release / "VERSION").write_text(version, encoding="utf-8")
        (self.release / "requirements.lock").write_text("", encoding="utf-8")
        (self.release / "requirements-build.lock").write_text("", encoding="utf-8")
        for name in markers:
            (self.release / name).write_text("", encoding="utf-8")
        git(self.release, "add", "-A")
        git(self.release, "commit", "-q", "-m", version)
        sha = git(self.release, "rev-parse", "HEAD")
        if push:
            git(self.release, "push", "-q", "origin", "HEAD:main")
        return sha

    def run(self, script: str, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(["bash", str(OPS / script), *args], cwd=self.root, env=self.env, capture_output=True,
                                text=True, timeout=120, check=False)
        if check:
            assert result.returncode == 0, result.stdout + result.stderr
        return result

    def deploy_at(self, sha: str, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        git(self.release, "checkout", "-q", sha)
        return self.run("deploy-hub.sh", "--release-root", str(self.release), "--deploy-dir", str(self.deploy),
                        "--python", str(self.python), *args, check=check)

    def fail_systemctl(self, command: str, times: int = 0, *, skip: int = 0, effect: bool = True):
        """Fault injection: `systemctl --user <command>` reports an error after taking effect (or, with effect=False,
        without doing anything). The first `skip` calls work; after them, every call fails (times=0) or only the next
        `times`, so recovery that runs it again can succeed."""
        real = self.bin / "systemctl-real"
        if not real.exists():
            (self.bin / "systemctl").rename(real)
        write_executable(self.bin / "systemctl", f"""#!/usr/bin/env bash
if [[ $2 == {command} ]]; then
    count=$(cat "$FAKE_SYSTEMD/faults.{command}" 2>/dev/null || echo 0)
    echo $((count + 1)) >"$FAKE_SYSTEMD/faults.{command}"
    if ((count >= {skip} && ({times} == 0 || count < {skip} + {times}))); then
        {"" if effect else "exit 1"}
        "$(dirname "$0")/systemctl-real" "$@"
        exit 1
    fi
fi
exec "$(dirname "$0")/systemctl-real" "$@"
""")

    def run_fake_systemctl(self, *args: str):
        subprocess.run([str(self.bin / "systemctl"), "--user", *args], env=self.env, check=True, timeout=60)

    def check(self) -> subprocess.CompletedProcess[str]:
        return self.run("check-hub.sh", "--deploy-dir", str(self.deploy), check=False)

    def read(self, name: str) -> str | None:
        path = self.deploy / name
        return path.read_text(encoding="utf-8").strip() if path.exists() else None

    def stamp(self, name: str = "deployed.json") -> dict | None:
        text = self.read(name)
        return json.loads(text) if text is not None else None

    def records(self) -> dict[str, object]:
        """Every record a deployment or rollback changes, for before/after comparison."""
        return {
            ".venv-path": self.read(".venv-path"), ".venv-previous": self.read(".venv-previous"),
            "deployed.json": self.read("deployed.json"), "deployed.previous.json": self.read("deployed.previous.json"),
            "ops": self.ops_marker("ops"), "ops.previous": self.ops_marker("ops.previous"),
            "venvs": sorted(path.name for path in (self.deploy / "venvs").iterdir()),
        }

    def ops_marker(self, name: str) -> str | None:
        """Which deployment's copy of the scripts is in ops/ or ops.previous/: tests tag each copy after a deploy."""
        tag = self.deploy / name / "TAG"
        if tag.exists():
            return tag.read_text(encoding="utf-8")
        return "untagged" if (self.deploy / name).exists() else None

    def ops_build(self, name: str) -> str | None:
        """The commit version tagged into the copy of the scripts in ops/ or ops.previous/ (see commit)."""
        path = self.deploy / name / "check-hub.sh"
        found = re.search(r"# test build (\S+)", path.read_text(encoding="utf-8")) if path.exists() else None
        return found.group(1) if found else None

    def tag_ops(self, tag: str):
        (self.deploy / "ops" / "TAG").write_text(tag, encoding="utf-8")

    def healthz(self) -> dict | None:
        if (self.systemd / "active").read_text(encoding="utf-8").strip() != "active":
            return None
        path = self.systemd / "healthz.8120"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None

    def set_unit(self, *, active: str, started: str = "4242", result: str = "success", after: str = ""):
        (self.systemd / "active").write_text(active, encoding="utf-8")
        (self.systemd / "prop.ExecMainStartTimestampMonotonic").write_text(started, encoding="utf-8")
        (self.systemd / "prop.Result").write_text(result, encoding="utf-8")
        (self.systemd / "prop.After").write_text(after, encoding="utf-8")


@pytest.fixture
def host(tmp_path) -> Host:
    return Host(tmp_path)


def venv_of(host: Host, sha: str) -> str:
    return str(host.deploy / "venvs" / sha)


# -- the unit --------------------------------------------------------------------------------------------------------


def unit_directives(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]


def test_the_unit_has_no_ordering_on_the_daemon_and_keeps_host_values_in_the_env_file(host):
    a = host.commit("1.0.0")
    host.deploy_at(a, "--public-url", "https://hub.example.invalid:8120", "--port", "8120")
    unit = host.unit_file.read_text(encoding="utf-8")
    directives = unit_directives(unit)
    # No ordering or dependency on any unit: Restart=always covers a daemon that is not up yet (#210).
    assert not [line for line in directives if re.match(r"(After|Before|Requires|Wants|BindsTo|Requisite|PartOf)=",
                                                        line)]
    for needed in ("Restart=always", "WantedBy=default.target", "UMask=0077", f"EnvironmentFile={host.deploy}/hub.env",
                   f"ExecStart=/usr/bin/env bash {host.deploy}/ops/run-hub.sh"):
        assert needed in directives, needed
    # The host-specific address is in hub.env (owner-only), not in the unit.
    assert "example.invalid" not in unit
    env_file = host.deploy / "hub.env"
    assert "HUB_PUBLIC_URL=https://hub.example.invalid:8120" in env_file.read_text(encoding="utf-8")
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600
    # The Hub was started on loopback with the env file's values.
    hub_args = (host.systemd / "hub-args").read_text(encoding="utf-8")
    assert "--host 127.0.0.1 --port 8120" in hub_args and "--public-url https://hub.example.invalid:8120" in hub_args
    assert "--status-interval 60" in hub_args


def test_a_hand_written_unit_is_replaced_only_when_asked(host):
    host.unit_file.parent.mkdir(parents=True)
    host.unit_file.write_text("[Unit]\nAfter=agent-harness.service\n[Service]\nExecStart=/bin/true\n", encoding="utf-8")
    a = host.commit("1.0.0")
    refused = host.deploy_at(a, check=False)
    assert refused.returncode != 0 and "--replace-unit" in refused.stderr
    assert "After=agent-harness.service" in host.unit_file.read_text(encoding="utf-8")
    assert host.read(".venv-path") is None and host.read("deployed.json") is None  # nothing was swapped
    assert not (host.deploy / "venvs" / a).exists()
    host.deploy_at(a, "--replace-unit")
    assert "After=" not in host.unit_file.read_text(encoding="utf-8")


def test_a_failed_first_deployment_over_a_hand_written_unit_puts_it_back_and_restarts_its_hub(host):
    # The tower's move to these scripts: a running Hub from a hand-written unit, no .venv-path yet. A deployment that
    # fails after the stop must not leave the Hub down or the owner's unit overwritten.
    hand_hub = write_executable(host.root / "hand-hub.sh", "#!/usr/bin/env bash\n"
                                "printf '{\"ok\":true,\"version\":\"0.9.0\"}' >\"$FAKE_SYSTEMD/healthz.8120\"\n")
    original = f"[Unit]\nAfter=agent-harness.service\n[Service]\nExecStart=/usr/bin/env bash {hand_hub}\n"
    host.unit_file.parent.mkdir(parents=True)
    host.unit_file.write_text(original, encoding="utf-8")
    (host.systemd / "enabled").touch()
    host.run_fake_systemctl("start", UNIT)
    assert host.healthz() == {"ok": True, "version": "0.9.0"}
    broken = host.commit("1.0.0", "BROKEN")
    failed = host.deploy_at(broken, "--replace-unit", check=False)
    assert failed.returncode != 0 and "the previous deployment is restored" in failed.stderr, failed.stderr
    assert host.unit_file.read_text(encoding="utf-8") == original
    assert (host.systemd / "enabled").exists()
    assert host.healthz() == {"ok": True, "version": "0.9.0"}  # the hand-written unit's Hub runs again
    for name in (".venv-path", "deployed.json", "hub.env"):
        assert host.read(name) is None, name


def test_a_failed_upgrade_leaves_the_unit_and_hub_env_as_they_were(host):
    a, broken = host.commit("1.0.0"), host.commit("1.1.0", "BROKEN")
    host.deploy_at(a)
    unit, env = host.unit_file.read_text(encoding="utf-8"), host.read("hub.env")
    failed = host.deploy_at(broken, "--port", "8130", check=False)
    assert failed.returncode != 0
    assert host.read("hub.env") == env and "HUB_PORT=8120" in env
    assert host.unit_file.read_text(encoding="utf-8") == unit
    assert host.healthz() == {"ok": True, "version": "1.0.0"}


# -- deploy, roll back, roll forward ----------------------------------------------------------------------------------


def test_deploy_upgrade_roll_back_and_roll_forward(host):
    a, b = host.commit("1.0.0"), host.commit("1.1.0")
    first = host.deploy_at(a)
    assert "ok    /healthz" in first.stdout  # every deployment ends with the check
    host.tag_ops("A")
    assert host.read(".venv-path") == venv_of(host, a) and host.read(".venv-previous") is None
    assert host.stamp()["commit"] == a and host.stamp()["version"] == "1.0.0" and host.stamp()["unmerged"] is False
    assert set(host.stamp()) == {"commit", "version", "deployed_at", "unmerged"}
    assert "enable agent-harness-hub.service" in (host.systemd / "calls").read_text(encoding="utf-8")

    host.deploy_at(b)
    host.tag_ops("B")
    assert host.read(".venv-path") == venv_of(host, b) and host.read(".venv-previous") == venv_of(host, a)
    assert host.stamp()["commit"] == b and host.stamp("deployed.previous.json")["commit"] == a
    assert (host.ops_marker("ops"), host.ops_marker("ops.previous")) == ("B", "A")
    assert host.healthz() == {"ok": True, "version": "1.1.0"}
    assert host.check().returncode == 0

    back = host.run("deploy-hub.sh", "--rollback", "--deploy-dir", str(host.deploy))
    assert "ok    /healthz" in back.stdout  # every rollback ends with the check
    assert host.read(".venv-path") == venv_of(host, a) and host.read(".venv-previous") == venv_of(host, b)
    assert host.stamp()["commit"] == a and host.stamp("deployed.previous.json")["commit"] == b
    assert (host.ops_marker("ops"), host.ops_marker("ops.previous")) == ("A", "B")
    assert host.healthz() == {"ok": True, "version": "1.0.0"}
    status = json.loads((host.deploy / "state" / "status.json").read_text(encoding="utf-8"))
    assert status["build"] == a

    host.run("deploy-hub.sh", "--rollback", "--deploy-dir", str(host.deploy))  # forward again while B's env exists
    assert host.read(".venv-path") == venv_of(host, b) and host.stamp()["commit"] == b
    assert (host.ops_marker("ops"), host.ops_marker("ops.previous")) == ("B", "A")
    assert host.healthz() == {"ok": True, "version": "1.1.0"}


def test_a_third_deployment_deletes_the_oldest_environment(host):
    a, b, c = host.commit("1.0.0"), host.commit("1.1.0"), host.commit("1.2.0")
    for sha in (a, b, c):
        host.deploy_at(sha)
    assert host.records()["venvs"] == sorted([b, c])
    assert host.read(".venv-previous") == venv_of(host, b)


def test_a_deployment_whose_hub_does_not_start_is_undone_and_never_becomes_previous(host):
    a, b, broken = host.commit("1.0.0"), host.commit("1.1.0"), host.commit("1.2.0", "BROKEN")
    host.deploy_at(a)
    host.tag_ops("A")
    host.deploy_at(b)
    host.tag_ops("B")
    before = host.records()
    failed = host.deploy_at(broken, check=False)
    assert failed.returncode != 0 and "the previous deployment is restored" in failed.stderr
    assert host.records() == before  # pointers, records, ops and environments, the failed one removed
    assert host.healthz() == {"ok": True, "version": "1.1.0"}  # the previous Hub runs again
    # A rollback still goes to A, the deployment before B, not to the failed one.
    host.run("deploy-hub.sh", "--rollback", "--deploy-dir", str(host.deploy))
    assert host.stamp()["commit"] == a and host.healthz() == {"ok": True, "version": "1.0.0"}
    assert (host.ops_marker("ops"), host.ops_marker("ops.previous")) == ("A", "B")


def test_a_failed_check_after_the_deployment_rolls_back_automatically(host):
    # The Hub starts and answers /healthz with the new version, but its status.json is stale: only the check fails.
    a, stale = host.commit("1.0.0"), host.commit("1.1.0", "STALE")
    host.deploy_at(a)
    host.tag_ops("A")
    before = host.records()
    failed = host.deploy_at(stale, check=False)
    assert failed.returncode != 0
    assert "FAIL" in failed.stdout and "status.json is stale" in failed.stdout
    assert "ok    /healthz" in failed.stdout and "with version 1.1.0" in failed.stdout
    assert host.records() == before
    assert host.healthz() == {"ok": True, "version": "1.0.0"}


def test_a_failed_first_deployment_leaves_no_records(host):
    broken = host.commit("1.0.0", "BROKEN")
    failed = host.deploy_at(broken, check=False)
    assert failed.returncode != 0
    for name in (".venv-path", ".venv-previous", "deployed.json", "deployed.previous.json"):
        assert host.read(name) is None, name
    # Nothing of the attempt stays: no environment and no copy of the scripts; the Hub is stopped.
    assert host.records()["venvs"] == [] and host.ops_marker("ops") is None
    assert host.ops_marker("ops.previous") is None
    assert (host.systemd / "active").read_text(encoding="utf-8").strip() == "inactive"
    assert not (host.deploy / ".snapshot").exists()


def test_a_failed_rollback_restores_what_ran_before(host):
    a, b = host.commit("1.0.0"), host.commit("1.1.0")
    host.deploy_at(a)
    host.tag_ops("A")
    host.deploy_at(b)
    host.tag_ops("B")
    before = host.records()
    (Path(venv_of(host, a)) / "broken").touch()  # A's environment no longer starts
    failed = host.run("deploy-hub.sh", "--rollback", "--deploy-dir", str(host.deploy), check=False)
    assert failed.returncode != 0 and "the previous state is restored" in failed.stderr
    assert host.records() == before
    assert host.healthz() == {"ok": True, "version": "1.1.0"}


def test_rollback_without_a_previous_deployment_changes_nothing(host):
    a = host.commit("1.0.0")
    host.deploy_at(a)
    before = host.records()
    refused = host.run("deploy-hub.sh", "--rollback", "--deploy-dir", str(host.deploy), check=False)
    assert refused.returncode != 0 and "no previous deployment" in refused.stderr
    assert host.records() == before


def test_stale_status_cannot_pass_with_zero_padded_interval(host):
    a = host.commit("1.0.0")
    stale = host.commit("1.1.0", "STALE")
    host.deploy_at(a)
    before = host.records()

    result = host.deploy_at(
        stale, "--status-seconds", "08", check=False
    )

    assert result.returncode != 0, result.stdout + result.stderr
    assert host.records() == before


def test_upgrade_from_deployed_scripts(host):
    a, b = host.commit("1.0.0"), host.commit("1.1.0")
    host.deploy_at(a)
    git(host.release, "checkout", "-q", b)

    result = host.run(
        str(host.deploy / "ops" / "deploy-hub.sh"),
        "--release-root", str(host.release),
        "--deploy-dir", str(host.deploy),
        "--python", str(host.python),
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_a_deployment_installs_the_commits_own_scripts_whichever_copy_runs_it(host):
    # Run from the deployed copy of A's scripts, deploying B installs B's scripts, and A's become ops.previous.
    a, b = host.commit("1.0.0"), host.commit("1.1.0")
    host.deploy_at(a)
    assert host.ops_build("ops") == "1.0.0"
    git(host.release, "checkout", "-q", b)
    host.run(str(host.deploy / "ops" / "deploy-hub.sh"), "--release-root", str(host.release),
             "--deploy-dir", str(host.deploy))
    assert (host.ops_build("ops"), host.ops_build("ops.previous")) == ("1.1.0", "1.0.0")
    assert host.stamp()["commit"] == b and host.healthz() == {"ok": True, "version": "1.1.0"}
    assert not list(host.deploy.glob(".ops-*")) and not (host.deploy / "ops.new").exists()
    # and the deployed copy rolls back too
    host.run(str(host.deploy / "ops" / "deploy-hub.sh"), "--rollback", "--deploy-dir", str(host.deploy))
    assert (host.ops_build("ops"), host.ops_build("ops.previous")) == ("1.0.0", "1.1.0")
    assert host.healthz() == {"ok": True, "version": "1.0.0"}


def test_a_commit_without_the_linux_scripts_is_refused_before_any_downtime(host):
    a, bare = host.commit("1.0.0"), host.commit("1.1.0", scripts=False)
    host.deploy_at(a)
    before = host.records()
    refused = host.deploy_at(bare, check=False)
    assert refused.returncode != 0 and "carries no Linux deployment scripts" in refused.stderr
    assert host.records() == before and host.healthz() == {"ok": True, "version": "1.0.0"}


def test_zero_padded_numbers_are_read_in_base_10(host):
    # Bash reads 08 as a bad octal number; every number the scripts do arithmetic on is normalized first.
    host.env.update(HUB_START_TIMEOUT="02", HUB_STATUS_MAX_AGE="0300")
    host.deploy_at(host.commit("1.0.0"), "--port", "08120", "--status-seconds", "060")
    env = host.read("hub.env")
    assert "HUB_PORT=8120" in env and "HUB_STATUS_SECONDS=60" in env
    assert "--port 8120 --status-interval 60" in (host.systemd / "hub-args").read_text(encoding="utf-8")
    assert host.check().returncode == 0


def test_a_check_that_cannot_evaluate_fails_the_deployment_and_restores(host):
    # Fail closed: a value the freshness check cannot use fails the check (and so the deployment), never skips it.
    a, b = host.commit("1.0.0"), host.commit("1.1.0")
    host.deploy_at(a)
    before = host.records()
    host.env["HUB_STATUS_MAX_AGE"] = "1e3"
    failed = host.deploy_at(b, check=False)
    assert failed.returncode != 0, failed.stdout + failed.stderr
    assert "FAIL  HUB_STATUS_MAX_AGE must be a whole number" in failed.stdout
    assert host.records() == before and host.healthz() == {"ok": True, "version": "1.0.0"}


def test_invalid_numbers_are_refused(host):
    a = host.commit("1.0.0")
    for args, env, message in ((["--port", "70000"], {}, "invalid port"),
                               (["--status-seconds", "8s"], {}, "invalid status interval"),
                               ([], {"HUB_START_TIMEOUT": "09x"}, "HUB_START_TIMEOUT"),
                               ([], {"HUB_POLL_SECONDS": "0"}, "HUB_POLL_SECONDS")):
        saved = dict(host.env)
        host.env.update(env)
        refused = host.deploy_at(a, *args, check=False)
        host.env = saved
        assert refused.returncode != 0 and message in refused.stderr, (args, env, refused.stderr)
    assert host.read(".venv-path") is None


def test_failed_stop_restores_running_hub(host):
    a, b = host.commit("1.0.0"), host.commit("1.1.0")
    host.deploy_at(a)
    before = host.records()

    ctl = host.bin / "systemctl"
    ctl.rename(host.bin / "systemctl-real")
    write_executable(ctl, '''#!/usr/bin/env bash
"$(dirname "$0")/systemctl-real" "$@"
status=$?
if [[ $2 == stop ]]; then exit 1; fi
exit "$status"
''')

    result = host.deploy_at(b, check=False)
    assert result.returncode != 0
    assert host.records() == before
    assert host.healthz() == {
        "ok": True, "version": "1.0.0"
    }, result.stderr


def test_a_start_that_reports_an_error_after_starting_is_rolled_back(host):
    a, b = host.commit("1.0.0"), host.commit("1.1.0")
    host.deploy_at(a)
    before = host.records()
    host.fail_systemctl("start", times=1)  # the new Hub starts, then systemctl says it failed
    failed = host.deploy_at(b, check=False)
    assert failed.returncode != 0
    assert "the previous deployment is restored and answers /healthz" in failed.stderr, failed.stderr
    assert host.records() == before and host.healthz() == {"ok": True, "version": "1.0.0"}


def test_a_failed_daemon_reload_while_installing_the_unit_changes_nothing(host):
    a, b = host.commit("1.0.0"), host.commit("1.1.0")
    host.deploy_at(a)
    before, unit, env = host.records(), host.unit_file.read_text(encoding="utf-8"), host.read("hub.env")
    host.fail_systemctl("daemon-reload", times=1)  # --port changes the unit, so install-unit.sh reloads
    failed = host.deploy_at(b, "--port", "8130", check=False)
    assert failed.returncode != 0
    assert "failed before the Hub was stopped; nothing else was changed" in failed.stderr, failed.stderr
    assert host.records() == before and host.read("hub.env") == env
    assert host.unit_file.read_text(encoding="utf-8") == unit
    assert host.healthz() == {"ok": True, "version": "1.0.0"}  # never stopped


def test_a_failed_stop_over_a_hand_written_unit_restarts_its_hub(host):
    hand_hub = write_executable(host.root / "hand-hub.sh", "#!/usr/bin/env bash\n"
                                "printf '{\"ok\":true,\"version\":\"0.9.0\"}' >\"$FAKE_SYSTEMD/healthz.8120\"\n")
    original = f"[Service]\nExecStart=/usr/bin/env bash {hand_hub}\n"
    host.unit_file.parent.mkdir(parents=True)
    host.unit_file.write_text(original, encoding="utf-8")
    (host.systemd / "enabled").touch()
    host.run_fake_systemctl("start", UNIT)
    host.fail_systemctl("stop")
    failed = host.deploy_at(host.commit("1.0.0"), "--replace-unit", check=False)
    assert failed.returncode != 0 and "restored and answers /healthz" in failed.stderr, failed.stderr
    assert host.unit_file.read_text(encoding="utf-8") == original
    assert host.healthz() == {"ok": True, "version": "0.9.0"}


def test_a_rollback_whose_stop_reports_an_error_brings_the_current_hub_back(host):
    a, b = host.commit("1.0.0"), host.commit("1.1.0")
    host.deploy_at(a)
    host.deploy_at(b)
    before = host.records()
    host.fail_systemctl("stop")
    failed = host.run("deploy-hub.sh", "--rollback", "--deploy-dir", str(host.deploy), check=False)
    assert failed.returncode != 0
    assert "rollback not done" in failed.stderr and "answers /healthz again" in failed.stderr, failed.stderr
    assert host.records() == before and host.healthz() == {"ok": True, "version": "1.1.0"}


def test_a_rollback_whose_start_reports_an_error_is_undone(host):
    a, b = host.commit("1.0.0"), host.commit("1.1.0")
    host.deploy_at(a)
    host.tag_ops("A")
    host.deploy_at(b)
    host.tag_ops("B")
    before = host.records()
    host.fail_systemctl("start", times=1)
    failed = host.run("deploy-hub.sh", "--rollback", "--deploy-dir", str(host.deploy), check=False)
    assert failed.returncode != 0
    assert "the previous state is restored and its Hub answers /healthz" in failed.stderr, failed.stderr
    assert host.records() == before and host.healthz() == {"ok": True, "version": "1.1.0"}


def test_uninstall_says_what_a_failed_stop_leaves(deployed):
    deployed.fail_systemctl("stop")
    failed = deployed.run("uninstall-unit.sh", "--deploy-dir", str(deployed.deploy), check=False)
    assert failed.returncode != 0 and "may be stopped or running" in failed.stderr
    assert deployed.unit_file.exists()


def test_recovery_replaces_a_failed_hub_that_its_stop_left_running(host):
    # The failed build has the previous version, so /healthz alone cannot tell them apart. Its check fails (stale
    # status.json); the recovery stop then reports an error without stopping it. Recovery must still end with the
    # previous commit running, not report the failed one as restored.
    a, c = host.commit("1.0.0"), host.commit("1.0.0", "STALE")
    host.deploy_at(a)
    before = host.records()
    host.fail_systemctl("stop", skip=1, effect=False)  # the swap's stop works; the recovery's does nothing
    failed = host.deploy_at(c, check=False)
    assert failed.returncode != 0
    assert "the previous deployment is restored and answers /healthz" in failed.stderr, failed.stderr
    assert host.records() == before
    status = json.loads((host.deploy / "state" / "status.json").read_text(encoding="utf-8"))
    assert status["build"] == a  # the previous commit runs, not the failed one


def test_recovery_that_cannot_replace_the_failed_hub_says_so_and_keeps_its_environment(host):
    a, c = host.commit("1.0.0"), host.commit("1.0.0", "STALE")
    host.deploy_at(a)
    host.fail_systemctl("stop", skip=1, effect=False)
    restart = host.bin / "systemctl-real"  # restart, too, now does nothing and fails
    text = restart.read_text(encoding="utf-8")
    restart.write_text(text.replace("    start | restart)", "    restart) exit 1 ;;\n    start)"), encoding="utf-8")
    failed = host.deploy_at(c, check=False)
    assert failed.returncode != 0
    assert "restoring also failed" in failed.stderr and "not confirmed running" in failed.stderr, failed.stderr
    assert f"kept {host.deploy}/venvs/{c}, which may still be running" in failed.stderr
    assert (host.deploy / "venvs" / c).is_dir()


def test_a_failed_restore_of_the_previous_scripts_is_reported(host):
    # A, B, then C fails; putting ops.previous back from ops.previous.bak fails. The deployment must say the scripts
    # were not restored, not report a clean restore.
    a, b, broken = host.commit("1.0.0"), host.commit("1.1.0"), host.commit("1.2.0", "BROKEN")
    host.deploy_at(a)
    host.deploy_at(b)
    write_executable(host.bin / "mv", """#!/usr/bin/env bash
for arg in "$@"; do
    [[ $arg == */ops.previous.bak ]] && [[ $arg != "${*: -1}" ]] && { echo "mv: injected failure" >&2; exit 1; }
done
exec /bin/mv "$@"
""")
    failed = host.deploy_at(broken, check=False)
    assert failed.returncode != 0
    assert "restoring also failed" in failed.stderr and "restoring ops/ and ops.previous/ failed" in failed.stderr, \
        failed.stderr
    assert (host.deploy / "ops.previous.bak").is_dir()  # the older copy is kept, not lost


def test_a_failure_before_the_stop_restores_the_unit_even_when_the_scripts_cannot_be(host):
    # install-unit.sh rewrites the unit and hub.env, then daemon-reload fails; putting ops.previous back fails too. The
    # unit and hub.env must still be restored, and the message must say the scripts were not.
    a, b, c = host.commit("1.0.0"), host.commit("1.1.0"), host.commit("1.2.0")
    host.deploy_at(a)
    host.deploy_at(b)
    unit, env = host.unit_file.read_text(encoding="utf-8"), host.read("hub.env")
    host.fail_systemctl("daemon-reload", times=1)
    write_executable(host.bin / "mv", """#!/usr/bin/env bash
for arg in "$@"; do
    [[ $arg == */ops.previous.bak ]] && [[ $arg != "${*: -1}" ]] && { echo "mv: injected failure" >&2; exit 1; }
done
exec /bin/mv "$@"
""")
    failed = host.deploy_at(c, "--port", "8130", check=False)
    assert failed.returncode != 0
    assert "failed before the Hub was stopped (it still runs); restoring failed" in failed.stderr, failed.stderr
    assert "restoring ops/ and ops.previous/ failed" in failed.stderr
    assert host.unit_file.read_text(encoding="utf-8") == unit and host.read("hub.env") == env
    assert host.healthz() == {"ok": True, "version": "1.1.0"}
    assert not (host.deploy / "venvs" / c).exists()


def test_a_deployment_that_cannot_delete_the_older_scripts_still_stands(host):
    a, b, c = host.commit("1.0.0"), host.commit("1.1.0"), host.commit("1.2.0")
    host.deploy_at(a)
    host.deploy_at(b)
    # only the cleanup after the check passed (deployed.json names 1.2.0), not copy_ops's own clearing of the copy
    write_executable(host.bin / "rm", f"""#!/usr/bin/env bash
if [[ ${{*: -1}} == */ops.previous.bak ]] && grep -q '"1.2.0"' "{host.deploy}/deployed.json" 2>/dev/null; then
    echo "rm: injected failure" >&2; exit 1
fi
exec /bin/rm "$@"
""")
    result = host.deploy_at(c)
    assert "could not delete" in result.stderr and f"Deployed {c}" in result.stdout
    assert host.stamp()["commit"] == c and host.healthz() == {"ok": True, "version": "1.2.0"}


# -- guards -----------------------------------------------------------------------------------------------------------


def test_deploy_refuses_a_dirty_checkout_a_wrong_head_and_an_unmerged_commit(host):
    a = host.commit("1.0.0")
    unmerged = host.commit("1.1.0", push=False)
    git(host.release, "checkout", "-q", a)
    (host.release / "VERSION").write_text("dirty", encoding="utf-8")
    dirty = host.run("deploy-hub.sh", "--release-root", str(host.release), "--deploy-dir", str(host.deploy),
                     "--python", str(host.python), check=False)
    assert dirty.returncode != 0 and "uncommitted changes" in dirty.stderr
    git(host.release, "checkout", "-q", "--", "VERSION")
    wrong = host.run("deploy-hub.sh", "--release-root", str(host.release), "--deploy-dir", str(host.deploy),
                     "--python", str(host.python), "--commit", unmerged, check=False)
    assert wrong.returncode != 0 and "expected" in wrong.stderr
    refused = host.deploy_at(unmerged, check=False)
    assert refused.returncode != 0 and "not on origin/main" in refused.stderr
    assert not (host.deploy / ".venv-path").exists()
    trial = host.deploy_at(unmerged, "--allow-unmerged")
    assert "not on origin/main" in trial.stderr and host.stamp()["unmerged"] is True


def test_a_dry_run_changes_nothing(host):
    a = host.commit("1.0.0")
    result = host.deploy_at(a, "--dry-run")
    assert "[dry run]" in result.stdout
    assert not host.deploy.exists() and not host.unit_file.exists()
    assert not (host.systemd / "calls").exists()


# -- the check --------------------------------------------------------------------------------------------------------


@pytest.fixture
def deployed(host) -> Host:
    host.deploy_at(host.commit("1.0.0"))
    assert host.check().returncode == 0
    return host


def test_the_check_passes_on_a_healthy_deployment(deployed):
    result = deployed.check()
    assert result.returncode == 0, result.stdout
    assert "FAIL" not in result.stdout
    assert "is enabled" in result.stdout and "is active" in result.stdout and "status.json was written" in result.stdout


def test_the_check_reports_a_start_job_dropped_at_boot(deployed):
    # The 2026-10-29 failure: enabled, inactive, never started since the user manager came up, no failure recorded.
    deployed.set_unit(active="inactive", started="0")
    result = deployed.check()
    assert result.returncode == 1
    assert f"FAIL  {UNIT} is enabled but has not started since the user manager came up" in result.stdout
    assert "start job was dropped" in result.stdout and "ordering cycle" in result.stdout


def test_the_check_reports_an_enabled_unit_that_was_stopped(deployed):
    deployed.set_unit(active="inactive", started="98765")
    result = deployed.check()
    assert result.returncode == 1
    assert f"FAIL  {UNIT} is enabled but inactive: it was stopped" in result.stdout
    assert "/healthz on 127.0.0.1:8120 does not answer" in result.stdout


def test_the_check_reports_a_unit_restarting_after_exits(deployed):
    deployed.set_unit(active="activating")
    result = deployed.check()
    assert result.returncode == 1 and f"FAIL  {UNIT} is restarting after the Hub exited" in result.stdout


def test_the_check_reports_a_failed_unit(deployed):
    deployed.set_unit(active="failed", result="exit-code")
    result = deployed.check()
    assert result.returncode == 1 and f"FAIL  {UNIT} failed (Result=exit-code)" in result.stdout


def test_the_check_reports_an_ordering_on_another_service(deployed):
    deployed.set_unit(active="active", after="basic.target agent-harness-daemon.service")
    (deployed.systemd / "healthz.8120").write_text('{"ok":true,"version":"1.0.0"}', encoding="utf-8")
    result = deployed.check()
    assert result.returncode == 1
    assert "is ordered on or requires agent-harness-daemon.service" in result.stdout
    assert "is active" in result.stdout


def test_the_check_reports_a_disabled_unit_and_a_wrong_build(deployed):
    (deployed.systemd / "enabled").unlink()
    status = deployed.deploy / "state" / "status.json"
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    status.write_text(json.dumps({"build": "f" * 40, "written_at": now}), encoding="utf-8")
    result = deployed.check()
    assert result.returncode == 1
    assert f"FAIL  {UNIT} is disabled, so it does not start at boot" in result.stdout
    assert f"names build {'f' * 40}, not the deployed" in result.stdout


def test_the_deployed_check_finds_its_own_deployment_without_arguments(host):
    host.deploy = host.root / "custom-deploy"  # not the default under HOME
    host.deploy_at(host.commit("1.0.0"))
    result = subprocess.run(["bash", str(host.deploy / "ops" / "check-hub.sh")], cwd=host.root, env=host.env,
                            capture_output=True, text=True, timeout=60, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"{host.deploy}/state/status.json" not in result.stdout and "status.json was written" in result.stdout


# -- stop and uninstall -----------------------------------------------------------------------------------------------


def test_uninstall_stops_and_removes_the_unit_and_keeps_the_deployment(deployed):
    deployed.run("uninstall-unit.sh", "--deploy-dir", str(deployed.deploy))
    assert not deployed.unit_file.exists() and not (deployed.systemd / "enabled").exists()
    assert deployed.healthz() is None
    assert (deployed.deploy / "deployed.json").exists() and (deployed.deploy / "state").is_dir()


# -- the ops copies, replayed on their own ----------------------------------------------------------------------------

OPS_SCENARIO = r"""
set -euo pipefail
source "$HUB_OPS/hub-ops.sh"
DEPLOY=$1
version() { if [[ -d $DEPLOY/$1 ]]; then cat "$DEPLOY/$1/run-hub.sh"; else printf -- '-'; fi; }
source_of() { mkdir -p "$DEPLOY/src-$1"; printf '%s' "$1" >"$DEPLOY/src-$1/run-hub.sh"; printf '%s' "$DEPLOY/src-$1"; }
shown() { printf '%s=%s,%s\n' "$1" "$(version ops)" "$(version ops.previous)"; }
copy_ops "$(source_of A)"; complete_ops; shown 'deploy A'
copy_ops "$(source_of C)"; complete_ops; shown 'deploy C'
copy_ops "$(source_of D)"; undo_ops; shown 'deploy D fails'
swap_ops; shown 'rollback'
swap_ops; shown 'rollback again'
"""


def test_a_failed_deployment_is_never_recorded_as_the_previous_scripts(tmp_path):
    script = tmp_path / "scenario.sh"
    script.write_text(OPS_SCENARIO, encoding="utf-8")
    deploy = tmp_path / "deploy"
    deploy.mkdir()
    result = subprocess.run(["bash", str(script), str(deploy)], capture_output=True, text=True, timeout=60,
                            env={**os.environ, "HUB_OPS": str(OPS)}, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    steps = dict(line.split("=", 1) for line in result.stdout.splitlines())
    assert steps == {"deploy A": "A,-", "deploy C": "C,A", "deploy D fails": "C,A", "rollback": "A,C",
                     "rollback again": "C,A"}


SWAP_FAULT = r"""
set -euo pipefail
source "$HUB_OPS/hub-ops.sh"
DEPLOY=$1
for name in ops ops.previous; do mkdir -p "$DEPLOY/$name"; printf '%s' "$name" >"$DEPLOY/$name/run-hub.sh"; done
# the second move of the swap (ops.previous -> ops), or the third (ops.swap -> ops.previous), fails
mv() {
    if [[ ${*: -1} == "$DEPLOY/$FAIL_INTO" && ! -e $DEPLOY/fault-done ]]; then : >"$DEPLOY/fault-done"; return 1; fi
    command mv "$@"
}
if swap_ops; then echo swapped; else echo refused; fi
printf '%s,%s,%s\n' "$(cat "$DEPLOY/ops/run-hub.sh")" "$(cat "$DEPLOY/ops.previous/run-hub.sh")" \
    "$([[ -e $DEPLOY/ops.swap ]] && echo left || echo clean)"
"""


@pytest.mark.parametrize("fail_into", ["ops", "ops.previous"])
def test_an_ops_swap_that_fails_part_way_changes_nothing(tmp_path, fail_into):
    script = tmp_path / "swap.sh"
    script.write_text(SWAP_FAULT, encoding="utf-8")
    deploy = tmp_path / "deploy"
    deploy.mkdir()
    result = subprocess.run(["bash", str(script), str(deploy)], capture_output=True, text=True, timeout=60,
                            env={**os.environ, "HUB_OPS": str(OPS), "FAIL_INTO": fail_into}, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.splitlines() == ["refused", "ops,ops.previous,clean"]


UNDO_FAULT = r"""
set -euo pipefail
source "$HUB_OPS/hub-ops.sh"
DEPLOY=$1
for name in ops ops.previous; do mkdir -p "$DEPLOY/$name"; printf '%s' "$name" >"$DEPLOY/$name/run-hub.sh"; done
mkdir -p "$DEPLOY/src"; printf new >"$DEPLOY/src/run-hub.sh"
copy_ops "$DEPLOY/src"
mv() {
    if [[ $1 == -- ]]; then shift; fi
    if [[ $1 == "$DEPLOY/ops.previous.bak" && ! -e $DEPLOY/fault-done ]]; then : >"$DEPLOY/fault-done"; return 1; fi
    command mv -- "$@"
}
if undo_ops; then echo undone; else echo "failed undo=$OPS_UNDO"; fi
if undo_ops; then echo undone; else echo failed; fi
printf '%s,%s\n' "$(cat "$DEPLOY/ops/run-hub.sh")" "$(cat "$DEPLOY/ops.previous/run-hub.sh")"
"""


def test_undo_ops_reports_a_failed_step_and_retries_only_what_is_left(tmp_path):
    script = tmp_path / "undo.sh"
    script.write_text(UNDO_FAULT, encoding="utf-8")
    deploy = tmp_path / "deploy"
    deploy.mkdir()
    result = subprocess.run(["bash", str(script), str(deploy)], capture_output=True, text=True, timeout=60,
                            env={**os.environ, "HUB_OPS": str(OPS)}, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.splitlines() == ["failed undo=1", "undone", "ops,ops.previous"]


# -- the scripts themselves -------------------------------------------------------------------------------------------


def code_lines(text: str) -> str:
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


def test_the_scripts_are_strict_bash_and_executable():
    names = {path.name for path in SHELL_SCRIPTS}
    assert set(EXECUTABLES) | {"hub-common.sh", "hub-ops.sh"} <= names
    tracked = subprocess.run(["git", "ls-files", "-s", "ops/hub"], cwd=ROOT, capture_output=True, text=True,
                             check=True).stdout
    modes = {line.split("\t")[1].rsplit("/", 1)[1]: line.split()[0] for line in tracked.splitlines()}
    for name in EXECUTABLES:
        text = (OPS / name).read_text(encoding="utf-8")
        assert text.startswith("#!/usr/bin/env bash\n") and "\nset -euo pipefail\n" in text, name
        if name in modes:  # tracked: the executable bit is committed
            assert modes[name] == "100755", name


def test_the_scripts_are_shellcheck_clean():
    shellcheck = shutil.which("shellcheck")
    if shellcheck is None:
        pytest.skip("shellcheck is not installed (GitHub's ubuntu-latest image has it)")
    result = subprocess.run([shellcheck, "-x", "-P", str(OPS), *map(str, SHELL_SCRIPTS)], cwd=OPS,
                            capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr


def test_the_hub_listens_on_loopback_only_and_the_daemon_is_never_touched():
    common = (OPS / "hub-common.sh").read_text(encoding="utf-8")
    assert "\nHUB_HOST=127.0.0.1\n" in common
    assert '--host "$HUB_HOST"' in (OPS / "run-hub.sh").read_text(encoding="utf-8")
    for path in SHELL_SCRIPTS:
        text = path.read_text(encoding="utf-8")
        code = code_lines(text)
        assert "0.0.0.0" not in text and "funnel" not in text.lower(), path.name  # noqa: S104
        assert "--host" not in code or path.name == "run-hub.sh", path.name
        # the only unit the scripts name is the Hub's own; no daemon process or unit is matched
        assert not re.search(r"agent-harness(-daemon)?\.service|harness-daemon|-m harness(?!_hub)", code), path.name


def test_no_host_name_or_secret_is_committed():
    deployment = (ROOT / "docs" / "deployment.md").read_text(encoding="utf-8")
    for text in (deployment, *(path.read_text(encoding="utf-8") for path in SHELL_SCRIPTS)):
        # a tailnet name: only placeholders and example names
        for name in re.findall(r"[A-Za-z0-9-]+\.[A-Za-z0-9-]+\.ts\.net", text):
            assert name.split(".")[0] in {"hub", "example", "tower", "host"} or name.startswith("<"), name
        assert not re.search(r"(?<![A-Za-z0-9])(?:ha|ho|hp)-[A-Za-z0-9_\-]{8,}", text)
        assert not re.search(r"(token|password|secret)\s*[=:]\s*['\"]?[A-Za-z0-9+/_\-]{16,}", text, re.I)
        assert not re.search(r"https://(?!<)[A-Za-z0-9-]+\.(?!example)[A-Za-z0-9-]+\.ts\.net", text)


def test_the_deployment_doc_leads_with_linux_and_keeps_windows():
    deployment = (ROOT / "docs" / "deployment.md").read_text(encoding="utf-8")
    for path in SHELL_SCRIPTS:
        assert path.name in deployment, path.name
    linux, windows = deployment.index("## Linux"), deployment.index("## Windows")
    assert linux < windows
    for needle in ("--rollback", "check-hub.sh", "systemctl --user", "loginctl enable-linger", "tailscale serve",
                   "hub.env", "sign-in link"):
        assert needle in deployment[linux:windows], needle
    assert re.search(r"peer check[^.]*Windows only", deployment[linux:windows], re.S)
    # the Windows path keeps its scripts and switches
    for needle in ("deploy-hub.ps1", "install-task.ps1", "-Rollback", "Unregister-ScheduledTask"):
        assert needle in deployment[windows:], needle
