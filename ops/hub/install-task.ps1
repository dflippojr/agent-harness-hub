# Registers a per-user logon task that runs run-hub.ps1 hidden, next to the daemon's AgentHarness-Daemon task. No
# elevation needed. The tower auto-logs-in on boot, so this fires on every restart.
param(
    [string]$DeployDir = 'D:\Agents\agent-harness-hub',
    [int]$Port = 8120,
    [string]$DaemonUrl = 'http://127.0.0.1:8100',
    # The https address `tailscale serve` publishes the Hub at (docs/deployment.md); empty keeps it loopback-only.
    [string]$PublicUrl = ''
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'hub-common.ps1')
# The task runs the supervisor from the deployment's own copy, so a later checkout change can't alter a running Hub.
$script = Join-Path $DeployDir 'ops\run-hub.ps1'
if (-not (Test-Path -LiteralPath $script)) { throw "run-hub.ps1 is not deployed yet: $script (run deploy-hub.ps1)" }

$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument (
    "-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$script`" " +
    "-DeployDir `"$DeployDir`" -Port $Port -DaemonUrl $DaemonUrl" + $(if ($PublicUrl) { " -PublicUrl $PublicUrl" } else { '' }))
$trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -Hidden

Register-ScheduledTask -TaskName $HubTaskName -Action $action -Trigger $trigger -Settings $settings `
    -Description "Agent Harness Hub (127.0.0.1:$Port, loopback only) for the daemon at $DaemonUrl." -Force | Out-Null
Get-ScheduledTask -TaskName $HubTaskName | Select-Object TaskName, State
