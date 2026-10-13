"""Exercise runner policy with synthetic contexts; never contact or alter a runner."""

import copy
import ctypes
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
HOOK = ROOT / "ops/github/review-job-started.ps1"
INSTALLER = ROOT / "ops/github/install-runner.ps1"
REPO = "dflippojr/agent-harness-hub"
POWERSHELL = shutil.which("pwsh") or (shutil.which("powershell") if os.name == "nt" else None)
pytestmark = pytest.mark.skipif(POWERSHELL is None, reason="runner scripts require PowerShell")
# The installer refuses an elevated token. Hosted Windows CI runners are elevated, so there the installer tests
# check that refusal instead of the mocked installation.
ELEVATED = os.name == "nt" and bool(ctypes.windll.shell32.IsUserAnAdmin())
needs_standard_user = pytest.mark.skipif(ELEVATED, reason="installer refuses an elevated token")


def _in_administrators_group(shell):
    script = "([Security.Principal.WindowsIdentity]::GetCurrent()).Groups.Value -contains 'S-1-5-32-544'"
    result = subprocess.run(
        [shell, "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        timeout=30,
    )
    return result.stdout.strip() == "True"


# The installer refuses Administrators-group accounts unless -OwnerAccount is passed. Tests on such a
# non-elevated account pass the switch, and the refusal test runs only there.
ADMIN_ACCOUNT = (
    os.name == "nt" and not ELEVATED and POWERSHELL is not None and _in_administrators_group(POWERSHELL)
)
needs_administrator_account = pytest.mark.skipif(
    not ADMIN_ACCOUNT, reason="needs a non-elevated Administrators account"
)


def _process_env():
    # Pass only platform setup, so fixture failures cannot print inherited tokens.
    keys = {
        "PATH",
        "SYSTEMROOT",
        "WINDIR",
        "COMSPEC",
        "PATHEXT",
        "PSMODULEPATH",
        "TEMP",
        "TMP",
        "USERPROFILE",
        "USERDOMAIN",
        "USERNAME",
        "APPDATA",
        "LOCALAPPDATA",
        "HOME",
        "LANG",
        "LC_ALL",
    }
    return {key: value for key, value in os.environ.items() if key.upper() in keys}


def _powershell(script, env):
    return subprocess.run(
        [POWERSHELL, "-NoProfile", "-NonInteractive", "-File", str(script)],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


@pytest.fixture
def job_context(tmp_path):
    env = _process_env()
    env.update(
        GITHUB_REPOSITORY=REPO,
        GITHUB_WORKFLOW_REF=f"{REPO}/.github/workflows/review.yml@refs/heads/main",
        GITHUB_REF="refs/heads/main",
        GITHUB_EVENT_NAME="pull_request_target",
        GITHUB_EVENT_PATH=str(tmp_path / "event.json"),
    )
    payload = {
        "repository": {"full_name": REPO, "default_branch": "main"},
        "pull_request": {
            "base": {"repo": {"full_name": REPO}, "ref": "main"},
            "head": {"repo": {"full_name": REPO}},
        },
    }
    return env, payload


def _run_hook(env, payload):
    Path(env["GITHUB_EVENT_PATH"]).write_text(json.dumps(payload), encoding="utf-8")
    return _powershell(HOOK, env)


@pytest.mark.parametrize("event_name", ["pull_request_target", "workflow_dispatch"])
def test_hook_accepts_base_repository_review(job_context, event_name):
    env, payload = job_context
    env["GITHUB_EVENT_NAME"] = event_name
    if event_name == "workflow_dispatch":
        del payload["pull_request"]
    result = _run_hook(env, payload)
    assert result.returncode == 0, result.stderr
    assert "accepted" in result.stdout


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("GITHUB_REPOSITORY", "synthetic/fork"),
        ("GITHUB_EVENT_NAME", "pull_request"),
        ("GITHUB_EVENT_NAME", "push"),
        ("GITHUB_EVENT_NAME", "schedule"),
        ("GITHUB_REF", "refs/pull/42/merge"),
        ("GITHUB_REF", "refs/heads/feature"),
        ("GITHUB_REF", "refs/tags/main"),
        ("GITHUB_WORKFLOW_REF", f"{REPO}/.github/workflows/sonar.yml@refs/heads/main"),
        ("GITHUB_WORKFLOW_REF", f"{REPO}/.github/workflows/review.yml@refs/heads/feature"),
        ("GITHUB_WORKFLOW_REF", f"{REPO}/.github/workflows/review.yml@refs/tags/main"),
        ("GITHUB_WORKFLOW_REF", "synthetic/fork/.github/workflows/review.yml@refs/heads/main"),
        ("GITHUB_WORKFLOW_REF", "dflippojr/agent-harness/.github/workflows/review.yml@review-v1"),
    ],
)
def test_hook_rejects_other_job_contexts(job_context, key, value):
    env, payload = job_context
    env[key] = value
    result = _run_hook(env, payload)
    assert result.returncode != 0
    assert "refused" in result.stderr


def test_hook_rejects_pull_request_event_from_fork_head(job_context):
    env, payload = job_context
    env["GITHUB_EVENT_NAME"] = "pull_request"
    env["GITHUB_REF"] = "refs/pull/42/merge"
    payload["pull_request"]["head"]["repo"]["full_name"] = "synthetic/fork"
    assert _run_hook(env, payload).returncode != 0


@pytest.mark.parametrize(
    "field",
    [
        ("repository", "full_name"),
        ("repository", "default_branch"),
        ("pull_request", "base", "repo", "full_name"),
        ("pull_request", "base", "ref"),
        ("pull_request", "head", "repo", "full_name"),
    ],
)
@pytest.mark.parametrize("missing", [False, True])
def test_hook_rejects_invalid_or_missing_payload_fields(job_context, field, missing):
    env, original = job_context
    payload = copy.deepcopy(original)
    parent = payload
    for key in field[:-1]:
        parent = parent[key]
    if missing:
        del parent[field[-1]]
    else:
        parent[field[-1]] = "synthetic-invalid-value"
    assert _run_hook(env, payload).returncode != 0


@pytest.mark.parametrize(
    "key",
    ["GITHUB_REPOSITORY", "GITHUB_WORKFLOW_REF", "GITHUB_REF", "GITHUB_EVENT_NAME", "GITHUB_EVENT_PATH"],
)
def test_hook_fails_closed_without_environment(job_context, key):
    env, payload = job_context
    Path(env["GITHUB_EVENT_PATH"]).write_text(json.dumps(payload), encoding="utf-8")
    del env[key]
    assert _powershell(HOOK, env).returncode != 0


@pytest.mark.parametrize("payload", [None, "not-json", '"synthetic-secret-value"'])
def test_hook_fails_closed_without_valid_event_and_does_not_echo_it(job_context, payload):
    env, _ = job_context
    if payload is not None:
        Path(env["GITHUB_EVENT_PATH"]).write_text(payload, encoding="utf-8")
    result = _powershell(HOOK, env)
    assert result.returncode != 0
    assert "synthetic-secret-value" not in result.stdout + result.stderr
    assert "refused" in result.stderr


def _ps_literal(path):
    return "'" + str(path).replace("'", "''") + "'"


@pytest.fixture
def mock_installation(tmp_path):
    """Replace every network, archive, registration, and task operation."""
    env = _process_env()
    env["TEMP"] = str(tmp_path)
    env["INSTALL_TEST_LOG"] = str(tmp_path / "config-args.txt")
    env["INSTALL_TEST_TASK"] = str(tmp_path / "task.json")
    env["INSTALL_TEST_STARTED"] = str(tmp_path / "started.txt")
    env["INSTALL_TEST_EXIT"] = "0"
    install_dir = tmp_path / "runner with spaces"
    hook_dir = tmp_path / "hooks with spaces"
    wrapper = tmp_path / "install-test.ps1"
    mocks = r"""
$ErrorActionPreference = 'Stop'
# Load command modules before defining mocks so autoload cannot replace them.
Import-Module Microsoft.PowerShell.Management, Microsoft.PowerShell.Utility
Import-Module Microsoft.PowerShell.Archive, ScheduledTasks
function Invoke-WebRequest { param($Uri, $OutFile, [switch]$UseBasicParsing)
    Set-Content -LiteralPath $OutFile -Value 'synthetic archive'
}
function Get-FileHash { param($Algorithm, $LiteralPath)
    [PSCustomObject]@{ Hash = 'synthetic-hash' }
}
function Expand-Archive { param($LiteralPath, $DestinationPath, [switch]$Force)
    Set-Content -LiteralPath (Join-Path $DestinationPath '.env') -Value @(
        'SYNTHETIC_KEY=stale', 'ACTIONS_RUNNER_HOOK_JOB_STARTED=old-hook')
    Set-Content -LiteralPath (Join-Path $DestinationPath 'config.cmd') -Value @(
        '@echo off', 'echo %* > "%INSTALL_TEST_LOG%"', 'exit /b %INSTALL_TEST_EXIT%')
}
function New-ScheduledTaskAction { param($Execute, $Argument, $WorkingDirectory)
    @{ Execute = $Execute; Argument = $Argument; WorkingDirectory = $WorkingDirectory }
}
function New-ScheduledTaskTrigger { param([switch]$AtLogOn, $User) @{ User = $User } }
function New-ScheduledTaskSettingsSet {
    param($ExecutionTimeLimit, $MultipleInstances, [switch]$AllowStartIfOnBatteries,
        [switch]$DontStopIfGoingOnBatteries, [switch]$Hidden)
    @{ Hidden = [bool]$Hidden }
}
function New-ScheduledTaskPrincipal { param($UserId, $LogonType, $RunLevel)
    @{ UserId = $UserId; LogonType = $LogonType; RunLevel = $RunLevel }
}
function Register-ScheduledTask {
    param($TaskName, $Action, $Trigger, $Settings, $Principal, $Description, [switch]$Force)
    @{ TaskName = $TaskName; Action = $Action; Principal = $Principal; Description = $Description } |
        ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $env:INSTALL_TEST_TASK
}
function Start-ScheduledTask { param($TaskName)
    Set-Content -LiteralPath $env:INSTALL_TEST_STARTED -Value $TaskName
}
function Get-ScheduledTask { param($TaskName) [PSCustomObject]@{ TaskName = $TaskName; State = 'Mock' } }
"""

    def execute(
        *, configured=False, registration_exit=0, inside_hook=False, persistent=False, owner_account=ADMIN_ACCOUNT
    ):
        if configured:
            install_dir.mkdir()
            (install_dir / ".runner").write_text("synthetic registration", encoding="utf-8")
        env["INSTALL_TEST_EXIT"] = str(registration_exit)
        chosen_hook = install_dir / "hooks" if inside_hook else hook_dir
        switches = (" -Persistent" if persistent else "") + (" -OwnerAccount" if owner_account else "")
        command = (
            f"& {_ps_literal(INSTALLER)} -Token 'synthetic-token' "
            f"-InstallDir {_ps_literal(install_dir)} -HookDir {_ps_literal(chosen_hook)} "
            f"-Version 'synthetic-version' -Sha256 'synthetic-hash'{switches}\n"
        )
        wrapper.write_text(mocks + command, encoding="utf-8")
        return _powershell(wrapper, env)

    return execute, install_dir, hook_dir, env


@pytest.mark.skipif(os.name != "nt", reason="installer uses Windows identity and config.cmd")
@needs_standard_user
@pytest.mark.parametrize("existing_empty_dir", [False, True])
def test_installer_sets_hook_ephemeral_registration_and_limited_task(mock_installation, existing_empty_dir):
    execute, install_dir, hook_dir, env = mock_installation
    if existing_empty_dir:
        install_dir.mkdir()
    result = execute()
    assert result.returncode == 0, result.stderr
    args = Path(env["INSTALL_TEST_LOG"]).read_text()
    assert "--ephemeral" in args
    assert f"--url https://github.com/{REPO}" in args
    assert "--labels agent-harness-hub-review" in args
    assert "--name agent-harness-hub-review" in args
    installed_hook = hook_dir / HOOK.name
    assert installed_hook.read_bytes() == HOOK.read_bytes()
    env_bytes = (install_dir / ".env").read_bytes()
    assert not env_bytes.startswith(b"\xef\xbb\xbf")
    # The archive's stale .env keys are dropped: the file holds only the hook line.
    assert env_bytes.decode("utf-8").splitlines() == [f"ACTIONS_RUNNER_HOOK_JOB_STARTED={installed_hook}"]
    task = json.loads(Path(env["INSTALL_TEST_TASK"]).read_text(encoding="utf-8-sig"))
    assert task["Principal"]["RunLevel"] == "Limited"
    assert task["Principal"]["LogonType"] == "Interactive"
    assert task["Principal"]["UserId"]
    assert "-WindowStyle Hidden" in task["Action"]["Argument"]
    assert Path(env["INSTALL_TEST_STARTED"]).read_text().strip() == "AgentHarnessHub-GitHubRunner-Review"


@pytest.mark.skipif(os.name != "nt", reason="installer uses Windows identity and config.cmd")
@needs_standard_user
def test_installer_persistent_registration_omits_ephemeral(mock_installation):
    execute, install_dir, hook_dir, env = mock_installation
    result = execute(persistent=True)
    assert result.returncode == 0, result.stderr
    args = Path(env["INSTALL_TEST_LOG"]).read_text()
    assert "--ephemeral" not in args
    assert f"--url https://github.com/{REPO}" in args
    assert (hook_dir / HOOK.name).read_bytes() == HOOK.read_bytes()
    assert (install_dir / ".env").read_text(encoding="utf-8").splitlines() == [
        f"ACTIONS_RUNNER_HOOK_JOB_STARTED={hook_dir / HOOK.name}"
    ]
    task = json.loads(Path(env["INSTALL_TEST_TASK"]).read_text(encoding="utf-8-sig"))
    assert task["Description"].startswith("Persistent GitHub Actions review runner")
    assert task["Principal"]["RunLevel"] == "Limited"
    assert task["Principal"]["LogonType"] == "Interactive"
    assert Path(env["INSTALL_TEST_STARTED"]).read_text().strip() == "AgentHarnessHub-GitHubRunner-Review"


@pytest.mark.skipif(os.name != "nt", reason="installer uses Windows identity and config.cmd")
@needs_standard_user
def test_installer_owner_account_switch_keeps_ephemeral_default(mock_installation):
    execute, _, _, env = mock_installation
    result = execute(owner_account=True)
    assert result.returncode == 0, result.stderr
    assert "--ephemeral" in Path(env["INSTALL_TEST_LOG"]).read_text()
    task = json.loads(Path(env["INSTALL_TEST_TASK"]).read_text(encoding="utf-8-sig"))
    assert task["Description"].startswith("Ephemeral GitHub Actions review runner")


@pytest.mark.skipif(os.name != "nt", reason="installer uses Windows identity and config.cmd")
@needs_administrator_account
def test_installer_refuses_administrator_account_without_owner_switch(mock_installation):
    execute, install_dir, hook_dir, env = mock_installation
    result = execute(owner_account=False)
    assert result.returncode != 0
    assert "-OwnerAccount" in result.stderr
    assert not install_dir.exists()
    assert not hook_dir.exists()
    assert not Path(env["INSTALL_TEST_LOG"]).exists()
    assert not Path(env["INSTALL_TEST_TASK"]).exists()


@pytest.mark.skipif(os.name != "nt", reason="installer uses Windows identity and config.cmd")
@needs_standard_user
@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"configured": True}, "already configured"),
        ({"registration_exit": 9}, "registration failed"),
        ({"inside_hook": True}, "HookDir must be outside"),
    ],
)
def test_installer_refuses_unsafe_setup_without_starting_a_task(mock_installation, options, message):
    execute, _, _, env = mock_installation
    result = execute(**options)
    assert result.returncode != 0
    assert message in result.stderr
    assert not Path(env["INSTALL_TEST_TASK"]).exists()
    assert not Path(env["INSTALL_TEST_STARTED"]).exists()


@pytest.mark.skipif(os.name != "nt", reason="installer uses Windows identity and config.cmd")
@needs_standard_user
def test_installer_refuses_non_empty_install_dir_without_touching_it(mock_installation):
    execute, install_dir, hook_dir, env = mock_installation
    leftover = install_dir / "_work" / "stale.txt"
    leftover.parent.mkdir(parents=True)
    leftover.write_text("left by a previous job", encoding="utf-8")
    result = execute()
    assert result.returncode != 0
    assert "not empty" in result.stderr
    assert leftover.read_text(encoding="utf-8") == "left by a previous job"
    assert not (install_dir / ".env").exists()
    assert not hook_dir.exists()
    assert not Path(env["INSTALL_TEST_LOG"]).exists()
    assert not Path(env["INSTALL_TEST_TASK"]).exists()
    assert not Path(env["INSTALL_TEST_STARTED"]).exists()


@pytest.mark.skipif(not ELEVATED, reason="needs an elevated Windows token")
def test_installer_refuses_elevated_token_without_installing(mock_installation):
    execute, install_dir, hook_dir, env = mock_installation
    result = execute()
    assert result.returncode != 0
    assert "elevated" in result.stderr
    assert not install_dir.exists()
    assert not hook_dir.exists()
    assert not Path(env["INSTALL_TEST_LOG"]).exists()
    assert not Path(env["INSTALL_TEST_TASK"]).exists()
