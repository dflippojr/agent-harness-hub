# Supervisor for the deployed Hub. Started hidden at logon by the "AgentHarness-Hub" scheduled task (install-task.ps1),
# next to the daemon's "AgentHarness-Daemon". Restarts the Hub if it exits. The Hub listens on loopback only.
# The Python comes from the deployment's .venv-path pointer (deploy-hub.ps1); HUB_BUILD_COMMIT comes from deployed.json,
# so the Hub's status.json (docs/production-signals.md) names the commit it runs.
param(
    [string]$DeployDir = 'D:\Agents\agent-harness-hub',
    [int]$Port = 8120,
    [string]$DaemonUrl = 'http://127.0.0.1:8100',
    [int]$StatusSeconds = 60
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'hub-common.ps1')

$paths = Get-HubPaths $DeployDir
if (-not (Test-Path -LiteralPath $paths.Pointer)) { throw "no deployment yet: $($paths.Pointer) is missing; run deploy-hub.ps1" }
$venv = ([string](Get-Content -Raw -LiteralPath $paths.Pointer)).Trim()
if (-not [System.IO.Path]::IsPathRooted($venv)) { throw "invalid deployment virtual environment pointer: $($paths.Pointer)" }
$python = Join-Path $venv 'Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) { throw "Hub Python is missing: $python" }
$buildCommit = ''
if (Test-Path -LiteralPath $paths.Stamp) { $buildCommit = [string](Get-Content -Raw -LiteralPath $paths.Stamp | ConvertFrom-Json).commit }
if ($buildCommit -notmatch '^[0-9a-f]{40}$') { $buildCommit = '' }

New-Item -ItemType Directory -Force $paths.Logs | Out-Null
$hubLog = Join-Path $paths.Logs 'hub.log'
$supervisorLog = Join-Path $paths.Logs 'supervisor.log'

# One supervisor per logon session, even if the task is started twice.
$mutex = New-Object System.Threading.Mutex($false, 'Local\AgentHarnessHub')
if (-not $mutex.WaitOne(0)) { exit 0 }

function Log($msg) { "$(Get-Date -Format s) $msg" | Add-Content $supervisorLog }

while ($true) {
    if ((Test-Path $hubLog) -and (Get-Item $hubLog).Length -gt 20MB) { Move-Item $hubLog "$hubLog.prev" -Force }
    Log "starting Hub $buildCommit"
    # cmd handles the append redirect so both streams land in one log without PowerShell wrapping stderr.
    $hubArgs = "--daemon-url $DaemonUrl --state-dir `"$($paths.State)`" --host $HubHost --port $Port --status-interval $StatusSeconds"
    $proc = Start-Process cmd.exe -ArgumentList "/c `"set HUB_BUILD_COMMIT=$buildCommit&& `"$python`" -u -m harness_hub $hubArgs >> `"$hubLog`" 2>&1`"" `
        -WorkingDirectory $DeployDir -WindowStyle Hidden -PassThru
    $proc.WaitForExit()
    Log "Hub exited with code $($proc.ExitCode); restarting in 10s"
    Start-Sleep -Seconds 10
}
