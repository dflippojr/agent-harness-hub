"""The tower deployment (ops/hub, docs/deployment.md) and the signals doc stay consistent with the Hub."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from harness_hub.claim import CLAIMED, OUTCOMES, PENDING, PERSISTING, UNCLAIMED
from harness_hub.daemon import DaemonClient
from harness_hub.status import STATUS_FILE, StatusReporter

from .conftest import FakeDaemon

ROOT = Path(__file__).resolve().parents[1]
OPS = ROOT / "ops" / "hub"
SIGNALS = (ROOT / "docs" / "production-signals.md").read_text(encoding="utf-8")


def scripts() -> dict[str, str]:
    return {path.name: path.read_text(encoding="utf-8") for path in OPS.glob("*.ps1")}


def signal_block() -> str:
    match = re.search(r"```yaml production-signals\n(.*?)```", SIGNALS, re.S)
    assert match, "docs/production-signals.md has no `yaml production-signals` block"
    return match.group(1)


def signal_entries() -> list[dict[str, str]]:
    """The block's signals as {id, kind, ...}: each is a `- id:` entry of `key: value` lines (the sweep reads real YAML;
    this keeps the check dependency-free and only needs the scalar keys the entries use)."""
    entries = []
    for chunk in re.split(r"^  - ", signal_block().split("signals:\n", 1)[1], flags=re.M)[1:]:
        entry = {}
        for line in chunk.splitlines():
            key, _, value = line.strip().partition(": ")
            entry.setdefault(key.removeprefix("- "), value)
        entries.append(entry)
    return entries


def test_the_hub_listens_on_loopback_only():
    common, run = scripts()["hub-common.ps1"], scripts()["run-hub.ps1"]
    assert "$HubHost = '127.0.0.1'" in common
    assert "--host $HubHost" in run
    for name, text in scripts().items():
        assert "0.0.0.0" not in text and "funnel" not in text.lower(), name  # noqa: S104
        assert "--host" not in text or name == "run-hub.ps1", name


def test_deploying_never_touches_the_daemon():
    for name, text in scripts().items():
        code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
        assert "AgentHarness-Daemon" not in code and "restart-daemon" not in code and "run-daemon" not in code, name
        assert "-m harness(" not in code, name
    common = scripts()["hub-common.ps1"]
    # Every process the Hub deployment may stop is matched by its own state directory and `harness_hub`.
    assert common.count("$stateMatch") >= 2 and "-m harness_hub" in common
    assert "Get-HubProcesses $StateDir" in common


def test_deploy_is_fail_closed_and_publishes_nothing():
    deploy = scripts()["deploy-hub.ps1"]
    for needle in ("--require-hashes", "Run git @('-C', $release, 'archive'", "merge-base --is-ancestor",
                   "status --porcelain", "-AllowUnmerged", "[switch]$Rollback", "[switch]$DryRun"):
        assert needle in deploy, needle
    assert re.search(r"\[ValidatePattern\('\^\[0-9a-f\]\{40\}\$'\)\]\[string\]\$Commit", deploy)
    for text in scripts().values():
        assert not re.search(r"twine|docker push|pypi|ghcr|gh release", text, re.I)


def test_rollback_restores_the_supervisor_scripts_and_stops_a_failed_target():
    deploy = scripts()["deploy-hub.ps1"]
    # ops and ops.previous travel with the environments: a deployment keeps the old scripts, a rollback swaps them back
    assert "ops.previous" in scripts()["hub-ops.ps1"] and deploy.count("Swap-Ops") == 2
    # a failed deployment is undone with Undo-Ops, which puts back the ops.previous Copy-Ops displaced, and a good one
    # drops that copy only after the Hub is healthy; the copy and the install-task step are inside the try
    swap = deploy.split("# -- swap, start, check", 1)[1]
    assert swap.count("Undo-Ops") == 2 and "Swap-Ops" not in swap and "Complete-Ops" in swap
    assert swap.index("try {") < swap.index("Copy-Ops") < swap.index("install-task.ps1") < swap.index("Stop-Hub")
    assert swap.index("Start-Hub") < swap.index("Complete-Ops")
    rollback = deploy.split("# -- rollback", 1)[1].split("# -- guards", 1)[0]
    # a rollback that fails stops the supervisor before restoring, because it reads its environment only at start
    assert rollback.index("} catch {") < rollback.index("Stop-Hub $paths.State", rollback.index("} catch {"))
    assert "$opsSwapped" in rollback


POWERSHELL = shutil.which("pwsh") or shutil.which("powershell")

# deploy-hub.ps1's calls to hub-ops.ps1, replayed on a scratch directory: a deployment copies the scripts in
# (Copy-Ops); a failed one undoes that (Undo-Ops) and a good one completes it (Complete-Ops); a rollback swaps ops
# and ops.previous (Swap-Ops).
OPS_SCENARIO = r"""
param([string]$Deploy)
$ErrorActionPreference = 'Stop'
. (Join-Path $env:HUB_OPS 'hub-ops.ps1')
$paths = @{ Deploy = $Deploy }
function Version([string]$Name) {
    $dir = Join-Path $Deploy $Name
    if (Test-Path -LiteralPath $dir) { (Get-Content -Raw (Join-Path $dir 'run-hub.ps1')).Trim() } else { '-' }
}
function Source([string]$Name) {
    $dir = Join-Path $Deploy "src-$Name"
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
    Set-Content -NoNewline -Path (Join-Path $dir 'run-hub.ps1') -Value $Name
    $dir
}
function Deploy([string]$Name) { Copy-Ops (Source $Name) }
function Fail([string]$Name) { Copy-Ops (Source $Name); UNDO }
function Shown([string]$Step) {
    [pscustomobject]@{ step = $Step; ops = Version 'ops'; previous = Version 'ops.previous' }
}
$shown = @()
Deploy 'A'; FINISH; $shown += Shown 'deploy A'
Deploy 'C'; FINISH; $shown += Shown 'deploy C'
Fail 'D'; $shown += Shown 'deploy D fails'
Swap-Ops; $shown += Shown 'rollback'
Swap-Ops; $shown += Shown 'rollback again'
$shown | ConvertTo-Json -Compress
"""


def run_ops_scenario(tmp_path: Path, undo: str, finish: str) -> dict[str, tuple[str, str]]:
    script = tmp_path / "scenario.ps1"
    script.write_text(OPS_SCENARIO.replace("UNDO", undo).replace("FINISH", finish), encoding="utf-8")
    deploy = tmp_path / "deploy"
    deploy.mkdir()
    result = subprocess.run(
        [POWERSHELL, "-NoProfile", "-NonInteractive", "-File", str(script), str(deploy)],
        capture_output=True, text=True, timeout=120, env={**os.environ, "HUB_OPS": str(OPS)}, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return {row["step"]: (row["ops"], row["previous"]) for row in json.loads(result.stdout)}


@pytest.mark.skipif(POWERSHELL is None, reason="needs PowerShell")
def test_a_failed_deployment_is_never_recorded_as_the_previous_scripts(tmp_path):
    # A, then C, then D that fails and is undone: ops.previous must still be A's supervisor (the one that goes with
    # .venv-previous), so the rollback to A runs A's scripts, and rolling forward again returns to C's.
    steps = run_ops_scenario(tmp_path, undo="Undo-Ops", finish="Complete-Ops")
    assert steps["deploy A"] == ("A", "-")
    assert steps["deploy C"] == ("C", "A")
    assert steps["deploy D fails"] == ("C", "A")
    assert steps["rollback"] == ("A", "C")
    assert steps["rollback again"] == ("C", "A")


def test_docs_name_the_scripts_and_each_script_exists():
    deployment = (ROOT / "docs" / "deployment.md").read_text(encoding="utf-8")
    for name in ("deploy-hub.ps1", "run-hub.ps1", "install-task.ps1"):
        assert (OPS / name).is_file() and name in deployment
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "[deployment.md](deployment.md)" in SIGNALS and "docs/deployment.md" in readme
    assert "-Rollback" in deployment and "/healthz" in deployment and "tailscale serve" in deployment


def test_signal_ids_are_unique_and_cover_the_five_the_issue_asks_for():
    entries = signal_entries()
    ids = [entry["id"] for entry in entries]
    assert len(ids) == len(set(ids))
    # the /health endpoint, the deployed SHA, the recent error log, the claim state, the last successful daemon poll
    assert {"health", "deployed-sha", "hub-errors", "claim", "daemon-poll"} <= set(ids)
    assert {entry["kind"] for entry in entries} <= {"freshness", "health", "errors", "deployed_sha"}


def test_signals_read_the_files_and_fields_the_hub_writes(tmp_path):
    reporter = StatusReporter(tmp_path, DaemonClient("https://daemon.test", transport=FakeDaemon().transport()),
                              lambda: CLAIMED, interval=60, build="0" * 40)
    status = asyncio.run(reporter.beat())
    assert json.loads((tmp_path / STATUS_FILE).read_text(encoding="utf-8")) == status
    block = signal_block()
    status_fields = set(re.findall(r"state/status\.json, field: (\w+)", block))
    assert status_fields == {"written_at", "daemon_last_ok_at", "claim"}
    assert status_fields <= set(status)
    assert "path: D:/Agents/agent-harness-hub/deployed.json, field: commit" in block
    assert "http://127.0.0.1:8120/healthz" in block and "logs/hub.log" in block
    # the Hub's own claim states are the values the claim signal may publish
    listed = re.search(r"values: \[([^\]]+)\]", block)
    assert listed
    published = set(listed.group(1).replace(" ", "").split(","))
    assert published == {UNCLAIMED, PENDING, CLAIMED, PERSISTING, *OUTCOMES}


def test_freshness_bounds_exceed_the_heartbeat_interval():
    run = scripts()["run-hub.ps1"]
    default = re.search(r"\$StatusSeconds = (\d+)", run)
    assert default
    interval = int(default.group(1))
    bounds = {entry["id"]: entry["max_age"] for entry in signal_entries() if "max_age" in entry}
    assert set(bounds) == {"heartbeat", "daemon-poll"}
    for bound in bounds.values():
        assert bound.endswith("m") and int(bound[:-1]) * 60 >= 4 * interval
    assert "tail -c 4194304" in SIGNALS


def test_the_signals_doc_and_scripts_carry_no_secret_material():
    for text in (SIGNALS, *scripts().values()):
        assert not re.search(r"(?<![A-Za-z0-9])(?:ha|ho|hp)-[A-Za-z0-9_\-]{8,}", text)
        assert not re.search(r"(token|password|secret)\s*[=:]\s*['\"]?[A-Za-z0-9+/_\-]{16,}", text, re.I)
