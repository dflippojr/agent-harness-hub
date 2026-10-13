<#
.SYNOPSIS
  Registers a pinned Agent Harness Hub review runner, ephemeral by default.

.DESCRIPTION
  The registration token is used once and is not saved. By default the runner
  is ephemeral and must run as a dedicated, standard (non-administrator) Windows
  account: it accepts one job, and the owner preserves diagnostics, cleans the
  work directory, and provisions a fresh registration before the next review.
  -Persistent omits --ephemeral for the owner-account runner described in
  docs/review-runner.md. -OwnerAccount allows an account that is a member of
  Administrators, and only when the token is not elevated; an elevated token is
  refused in every case. The defaults install this repository's review runner.
  The hook is installed outside the runner directory and the checkout. This
  script refuses a non-empty install directory and an already configured runner.

.EXAMPLE
  $token = gh api -X POST repos/dflippojr/agent-harness-hub/actions/runners/registration-token --jq .token
  .\ops\github\install-runner.ps1 -Token $token
.EXAMPLE
  .\ops\github\install-runner.ps1 -Token $token -Persistent -OwnerAccount
#>
param(
    [Parameter(Mandatory)][string]$Token,
    [ValidateSet('dflippojr/agent-harness-hub')][string]$Repo = 'dflippojr/agent-harness-hub',
    [string]$InstallDir = 'D:\Agents\agent-harness-hub-review',
    [string]$HookDir = 'D:\Agents\agent-harness-hub-review-hooks',
    [string]$WorkDir = '_work',
    [string]$Name = 'agent-harness-hub-review',
    [string]$TaskName = 'AgentHarnessHub-GitHubRunner-Review',
    [ValidateSet('agent-harness-hub-review')][string]$Labels = 'agent-harness-hub-review',
    [string]$Version = '2.337.0',
    [string]$Sha256 = '1150692afa94e71f872017e254ea55b6eece1eece3fe7e3a6d4c93d0a1b85cfc',
    [switch]$Persistent,
    [switch]$OwnerAccount
)
$ErrorActionPreference = 'Stop'
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
if ($principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'refusing an elevated administrator token: run this installer from a non-elevated session'
}
# Checked on the groups, not the elevated token: a non-elevated administrator still belongs to Administrators.
$inAdministrators = [bool]($identity.Groups | Where-Object { $_.Value -eq 'S-1-5-32-544' })
if ($inAdministrators -and -not $OwnerAccount) {
    throw 'this account is in Administrators: use a dedicated standard account, or pass -OwnerAccount'
}
$InstallDir = [IO.Path]::GetFullPath($InstallDir).TrimEnd('\')
$HookDir = [IO.Path]::GetFullPath($HookDir).TrimEnd('\')
$checkout = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..')).TrimEnd('\')
foreach ($excluded in @($InstallDir, $checkout)) {
    if ($HookDir -ieq $excluded -or $HookDir.StartsWith("$excluded\", [StringComparison]::OrdinalIgnoreCase)) {
        throw 'HookDir must be outside the runner application directory and checkout'
    }
}
# The scheduled task uses a PowerShell single-quoted path literal.
if ($InstallDir.Contains("'")) { throw 'InstallDir cannot contain a single quote' }
$zip = Join-Path $env:TEMP "actions-runner-win-x64-$Version.zip"
$url = "https://github.com/actions/runner/releases/download/v$Version/actions-runner-win-x64-$Version.zip"

if (Test-Path -LiteralPath (Join-Path $InstallDir '.runner')) {
    throw "a runner is already configured in $InstallDir"
}
if ((Test-Path -LiteralPath $InstallDir) -and (Get-ChildItem -LiteralPath $InstallDir -Force | Select-Object -First 1)) {
    throw "install directory is not empty: $InstallDir; use a new, empty directory"
}
New-Item -ItemType Directory -Force $InstallDir | Out-Null
if (-not (Test-Path -LiteralPath $zip)) { Invoke-WebRequest -UseBasicParsing -Uri $url -OutFile $zip }
$actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $zip).Hash.ToLowerInvariant()
if ($actual -ne $Sha256) { throw "runner archive hash mismatch: $actual" }
Expand-Archive -LiteralPath $zip -DestinationPath $InstallDir -Force

New-Item -ItemType Directory -Force $HookDir | Out-Null
$hook = Join-Path $HookDir 'review-job-started.ps1'
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'review-job-started.ps1') -Destination $hook -Force
$runnerEnv = Join-Path $InstallDir '.env'
# UTF-8 without a BOM: the runner reads .env as key=value, not PowerShell.
[IO.File]::WriteAllLines($runnerEnv, [string[]]@("ACTIONS_RUNNER_HOOK_JOB_STARTED=$hook"),
    (New-Object Text.UTF8Encoding($false)))

$registerFlags = @()
if (-not $Persistent) { $registerFlags += '--ephemeral' }
$mode = if ($Persistent) { 'Persistent' } else { 'Ephemeral' }
Push-Location $InstallDir
try {
    & .\config.cmd --unattended --url "https://github.com/$Repo" --token $Token --name $Name `
        --labels $Labels --work $WorkDir @registerFlags
    if ($LASTEXITCODE -ne 0) { throw "runner registration failed with exit code $LASTEXITCODE" }
} finally { Pop-Location }

$launchArgs = "-NoProfile -NonInteractive -WindowStyle Hidden -Command `"& '$InstallDir\run.cmd'`""
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $launchArgs -WorkingDirectory $InstallDir
$trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -Hidden
$taskPrincipal = New-ScheduledTaskPrincipal -UserId $identity.Name -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -Principal $taskPrincipal -Description "$mode GitHub Actions review runner $Name for $Repo." -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName
Get-ScheduledTask -TaskName $TaskName | Select-Object TaskName, State
