# Shared by deploy-hub.ps1, run-hub.ps1 and install-task.ps1 (dot-source it). Defines where the deployed Hub lives and
# how to stop, start and health-check it. It never touches the agent-harness daemon: every process match below names
# this deployment's own state directory, and the daemon's `-m harness` command line cannot match it.
$HubTaskName = 'AgentHarness-Hub'
$HubDefaultDeployDir = 'D:\Agents\agent-harness-hub'
$HubDefaultPort = 8120
$HubDefaultDaemonUrl = 'http://127.0.0.1:8100'
# Loopback only. A tailnet address is published with `tailscale serve` (docs/deployment.md), never by listening on it.
$HubHost = '127.0.0.1'

function Get-HubPaths([string]$DeployDir) {
    [pscustomobject]@{
        Deploy   = $DeployDir
        State    = Join-Path $DeployDir 'state'
        Logs     = Join-Path $DeployDir 'logs'
        Venvs    = Join-Path $DeployDir 'venvs'
        Pointer  = Join-Path $DeployDir '.venv-path'
        Previous = Join-Path $DeployDir '.venv-previous'
        Stamp    = Join-Path $DeployDir 'deployed.json'
        StampOld = Join-Path $DeployDir 'deployed.previous.json'
    }
}

function Get-HubProcesses([string]$StateDir) {
    # The supervisor's command line names run-hub.ps1 and the deploy directory; the Hub's names --state-dir. A disposable
    # Hub from the verify skill or a test has another state directory, so it is never matched.
    $deploy = Split-Path $StateDir -Parent
    $stateMatch = [regex]::Escape($StateDir)
    $deployMatch = [regex]::Escape($deploy)
    Get-CimInstance Win32_Process | Where-Object {
        $_.CommandLine -and (
            ($_.Name -match '^(python|pythonw)\.exe$' -and $_.CommandLine -match '-m harness_hub(\s|$)' -and
                $_.CommandLine -match $stateMatch) -or
            ($_.Name -eq 'powershell.exe' -and $_.CommandLine -match 'run-hub\.ps1' -and $_.CommandLine -match $deployMatch) -or
            ($_.Name -eq 'cmd.exe' -and $_.CommandLine -match '-m harness_hub' -and $_.CommandLine -match $stateMatch))
    }
}

function Stop-Hub([string]$StateDir) {
    $taskError = $null
    try {
        $task = Get-ScheduledTask -TaskName $HubTaskName -ErrorAction Stop
        if ($task.State -eq 'Running') { Stop-ScheduledTask -TaskName $HubTaskName }
    } catch {
        # No task yet (first deployment) is not an error; anything else is reported after the processes are gone.
        if ($_.CategoryInfo.Category -ne 'ObjectNotFound') { $taskError = $_ }
    }
    foreach ($attempt in 1..30) {
        $remaining = @(Get-HubProcesses $StateDir)
        if ($remaining.Count -eq 0) { break }
        $remaining | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
        Start-Sleep -Seconds 1
    }
    if (@(Get-HubProcesses $StateDir).Count -gt 0) { throw 'could not stop all Hub processes' }
    if ($null -ne $taskError) { throw "could not stop $HubTaskName scheduled task: $($taskError.Exception.Message)" }
    Write-Host "$HubTaskName stopped."
}

function Test-HubHealth([int]$Port, [string]$Version = '') {
    try {
        $health = Invoke-RestMethod "http://127.0.0.1:$Port/healthz" -TimeoutSec 3
        return ($health.ok -eq $true) -and (-not $Version -or $health.version -eq $Version)
    } catch { return $false }
}

function Start-Hub([int]$Port, [string]$Version = '') {
    $task = Get-ScheduledTask -TaskName $HubTaskName -ErrorAction Stop
    if ($task.State -ne 'Running') {
        try { Start-ScheduledTask -TaskName $HubTaskName }
        catch {
            # A concurrent start is success. Preserve every other scheduled-task failure.
            if ((Get-ScheduledTask -TaskName $HubTaskName).State -ne 'Running') { throw }
        }
    }
    foreach ($i in 1..30) {
        Start-Sleep -Seconds 2
        if (Test-HubHealth $Port $Version) { Write-Host "Hub up after $($i * 2) s"; return }
    }
    throw "Hub did not answer /healthz within 60 s; see $((Get-HubPaths $HubDefaultDeployDir).Logs)"
}
