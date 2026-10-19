<#
.SYNOPSIS
Deploys one commit of the Hub to the tower, or rolls back to the previous deployment.

.DESCRIPTION
Modelled on agent-harness ops/harness/deploy-ci.ps1 and fail-closed the same way. It builds the requested commit
(from `git archive`, so the working tree never matters) into a side-by-side virtual environment with the locked,
hash-checked requirements before any downtime, checks that environment reports the expected version, and only then
stops the Hub, repoints `.venv-path`, writes `deployed.json`, starts the Hub and health-checks `/healthz`. A failure
after the stop restores the previous pointer and stamp and restarts the previous Hub. It keeps the previous
environment for `-Rollback` and deletes older ones.

By default the commit must already be on origin/main. `-AllowUnmerged` is for a trial deployment of a pull request's
head; it is recorded in deployed.json and printed, and the next deployment from main replaces it.

It never touches the agent-harness daemon and publishes nothing: the Hub listens on loopback only (hub-common.ps1).
The scheduled task that starts it at logon is installed once with install-task.ps1; this script runs it on the first
deployment if it is missing and the caller is allowed to register tasks (no elevation is needed).
#>
param(
    [ValidatePattern('^[0-9a-f]{40}$')][string]$Commit = '',
    [string]$ReleaseRoot = (Split-Path (Split-Path $PSScriptRoot -Parent) -Parent),
    [string]$DeployDir = 'D:\Agents\agent-harness-hub',
    [string]$Python = '',
    [int]$Port = 8120,
    [string]$DaemonUrl = 'http://127.0.0.1:8100',
    [string]$PublicUrl = '',
    [switch]$AllowUnmerged,
    [switch]$Rollback,
    [switch]$DryRun
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'hub-common.ps1')
. (Join-Path $PSScriptRoot 'hub-ops.ps1')

function Run([string]$File, [string[]]$Arguments) {
    if ($DryRun) { Write-Host "[dry run] $File $($Arguments -join ' ')"; return }
    & $File @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$File failed with exit code $LASTEXITCODE" }
}

function Get-GitOutput([string]$FailureMessage, [string[]]$GitArguments) {
    $output = & git @GitArguments
    if ($LASTEXITCODE -ne 0) { throw "$FailureMessage (git exit code $LASTEXITCODE)" }
    if ($null -eq $output) { return '' }
    return ([string]::Join("`n", [string[]]$output)).Trim()
}

function Set-Pointer([string]$Path, [string]$Value) {
    if ($DryRun) { Write-Host "[dry run] write $Path -> $Value"; return }
    $temporary = "$Path.tmp"
    [System.IO.File]::WriteAllText($temporary, $Value)
    if (Test-Path -LiteralPath $Path) { Remove-Item -LiteralPath $Path -Force }
    Move-Item -LiteralPath $temporary -Destination $Path
}

function Read-Pointer([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) { return '' }
    return ([string](Get-Content -Raw -LiteralPath $Path)).Trim()
}

function Write-Stamp([string]$Path, [string]$Sha, [string]$Version, [bool]$Unmerged) {
    $stamp = [ordered]@{
        commit = $Sha; version = $Version; deployed_at = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
        unmerged = $Unmerged
    } | ConvertTo-Json
    if ($DryRun) { Write-Host "[dry run] write $Path"; return }
    [System.IO.File]::WriteAllText($Path, $stamp + "`n")
}

$paths = Get-HubPaths $DeployDir
if (-not $DryRun) {
    foreach ($directory in @($paths.Deploy, $paths.Venvs, $paths.Logs)) { New-Item -ItemType Directory -Force -Path $directory | Out-Null }
}

# -- rollback ---------------------------------------------------------------------------------------------------------
if ($Rollback) {
    $current = Read-Pointer $paths.Pointer
    $previous = Read-Pointer $paths.Previous
    if (-not $previous -or -not (Test-Path -LiteralPath $previous)) { throw "no previous deployment to roll back to ($($paths.Previous))" }
    $oldStamp = if (Test-Path -LiteralPath $paths.StampOld) { Get-Content -Raw -LiteralPath $paths.StampOld } else { $null }
    $currentStamp = if (Test-Path -LiteralPath $paths.Stamp) { Get-Content -Raw -LiteralPath $paths.Stamp } else { $null }
    if ($DryRun) { Write-Host '[dry run] stop the Hub, swap .venv-path and deployed.json with their previous copies, start, check'; exit 0 }
    Stop-Hub $paths.State
    $opsSwapped = $false
    try {
        Set-Pointer $paths.Pointer $previous
        Set-Pointer $paths.Previous $current
        Swap-Ops
        $opsSwapped = $true
        if ($null -ne $oldStamp) { [System.IO.File]::WriteAllText($paths.Stamp, $oldStamp) }
        if ($null -ne $currentStamp) { [System.IO.File]::WriteAllText($paths.StampOld, $currentStamp) }
        Start-Hub $Port
    } catch {
        $failure = $_.Exception.Message
        # Stop the supervisor first: it reads its environment once, so only a new start runs the restored one.
        $recoveryErrors = @()
        try { Stop-Hub $paths.State } catch { $recoveryErrors += "stop: $($_.Exception.Message)" }
        if ($opsSwapped) { Swap-Ops }
        Set-Pointer $paths.Pointer $current
        Set-Pointer $paths.Previous $previous
        if ($null -ne $currentStamp) { [System.IO.File]::WriteAllText($paths.Stamp, $currentStamp) }
        if ($null -ne $oldStamp) { [System.IO.File]::WriteAllText($paths.StampOld, $oldStamp) }
        try { Start-Hub $Port } catch { $recoveryErrors += "restart: $($_.Exception.Message)" }
        if ($recoveryErrors.Count -gt 0) { throw "rollback failed ($failure); restoring what ran before also failed: $($recoveryErrors -join '; ')" }
        throw "rollback failed; the previous state is restored: $failure"
    }
    Write-Host "Rolled back to $previous"
    exit 0
}

# -- guards -----------------------------------------------------------------------------------------------------------
$release = (Resolve-Path -LiteralPath $ReleaseRoot).Path
if (-not (Test-Path -LiteralPath (Join-Path $release '.git'))) { throw "release checkout is not a git repository: $release" }
$head = Get-GitOutput 'could not resolve the release checkout HEAD' @('-C', $release, 'rev-parse', 'HEAD')
if (-not $Commit) { $Commit = $head }
if ($head -ne $Commit) { throw "release checkout is at $head, expected $Commit" }
$dirty = & git -C $release status --porcelain --untracked-files=no
if ($LASTEXITCODE -ne 0) { throw "could not inspect the release checkout (git exit code $LASTEXITCODE)" }
if ($dirty) { throw "release checkout has uncommitted changes; refusing deployment:`n$($dirty -join "`n")" }
$unmerged = $false
Run git @('-C', $release, 'fetch', '--prune', 'origin', 'main')
if (-not $DryRun) {
    & git -C $release merge-base --is-ancestor $Commit origin/main
    if ($LASTEXITCODE -ne 0) {
        if (-not $AllowUnmerged) { throw "$Commit is not on origin/main; deploy a merged commit (or pass -AllowUnmerged for a trial)" }
        $unmerged = $true
        Write-Warning "deploying $Commit, which is not on origin/main (-AllowUnmerged)"
    }
}
Write-Host '[deploy] guards passed'

# -- stage the environment before any downtime ------------------------------------------------------------------------
$currentVenv = Read-Pointer $paths.Pointer
if (-not $Python) {
    $Python = if ($currentVenv -and (Test-Path -LiteralPath (Join-Path $currentVenv 'Scripts\python.exe'))) {
        Join-Path $currentVenv 'Scripts\python.exe'
    } else { 'python' }
}
$stagedVenv = Join-Path $paths.Venvs $Commit
if (Test-Path -LiteralPath $stagedVenv) { throw "staged environment already exists: $stagedVenv (is $Commit already deployed?)" }
$source = Join-Path $paths.Deploy ".source-$Commit"
$archive = "$source.zip"
try {
    Run git @('-C', $release, 'archive', '--format=zip', "--output=$archive", $Commit)
    if (-not $DryRun) { Expand-Archive -LiteralPath $archive -DestinationPath $source }
    Run $Python @('-m', 'venv', $stagedVenv)
    $stagedPython = Join-Path $stagedVenv 'Scripts\python.exe'
    $pip = @('-m', 'pip', 'install', '--disable-pip-version-check', '--no-input')
    Run $stagedPython ($pip + @('--require-hashes', '-r', (Join-Path $source 'requirements.lock'), '-r', (Join-Path $source 'requirements-build.lock')))
    Run $stagedPython ($pip + @('--no-deps', '--no-build-isolation', $source))
    if ($DryRun) { $version = 'dry-run' } else {
        $reported = (& $stagedPython -I -m harness_hub --version)
        if ($LASTEXITCODE -ne 0 -or "$reported" -notmatch '^harness_hub (\S+)$') { throw "staged Hub did not report a version: $reported" }
        $version = $Matches[1]
        $static = & $stagedPython -I -c "from importlib import resources; s = resources.files('harness_hub') / 'static'; print((s / 'index.html').is_file() and (s / 'pages' / 'overview.mjs').is_file())"
        if ("$static" -ne 'True') { throw 'staged Hub carries no UI' }
    }
} catch {
    Remove-Tree $stagedVenv
    throw
} finally {
    Remove-Tree $source
    if (Test-Path -LiteralPath $archive) { Remove-Item -LiteralPath $archive -Force }
}
Write-Host "[deploy] Hub $version staged at $stagedVenv"

# -- swap, start, check; roll back on failure -------------------------------------------------------------------------
$previousVenv = Read-Pointer $paths.Pointer
$previousOfPrevious = Read-Pointer $paths.Previous
$hadStamp = Test-Path -LiteralPath $paths.Stamp
$stampBefore = if ($hadStamp) { Get-Content -Raw -LiteralPath $paths.Stamp } else { $null }
$oldStampBefore = if (Test-Path -LiteralPath $paths.StampOld) { Get-Content -Raw -LiteralPath $paths.StampOld } else { $null }
if ($DryRun) {
    Write-Host '[dry run] stop the Hub; repoint .venv-path; write deployed.json; copy ops; start; check /healthz'
    exit 0
}
$stopped = $false
try {
    Copy-Ops
    $task = Get-ScheduledTask -TaskName $HubTaskName -ErrorAction SilentlyContinue
    if ($null -eq $task) { Write-Host "[deploy] $HubTaskName is not registered yet; registering it"; & (Join-Path $PSScriptRoot 'install-task.ps1') -DeployDir $DeployDir -Port $Port -DaemonUrl $DaemonUrl -PublicUrl $PublicUrl | Out-Null }
    Stop-Hub $paths.State
    $stopped = $true
    if ($previousVenv) { Set-Pointer $paths.Previous $previousVenv }
    if ($hadStamp) { [System.IO.File]::WriteAllText($paths.StampOld, $stampBefore) }
    Set-Pointer $paths.Pointer $stagedVenv
    Write-Stamp $paths.Stamp $Commit $version $unmerged
    Write-Host '[deploy] environment swapped'
    Start-Hub $Port $version
    # The Hub's own heartbeat must name this commit too, not just answer.
    $status = Join-Path $paths.State 'status.json'
    $named = $false
    foreach ($i in 1..30) {
        if (Test-Path -LiteralPath $status) {
            try { if ((Get-Content -Raw -LiteralPath $status | ConvertFrom-Json).build -eq $Commit) { $named = $true; break } } catch { }
        }
        Start-Sleep -Seconds 2
    }
    if (-not $named) { throw 'status.json did not report the deployed commit within 60 s' }
    Write-Host '[deploy] Hub started and healthy'
} catch {
    $failure = $_.Exception.Message
    if (-not $stopped) { Undo-Ops; Remove-Tree $stagedVenv; throw }
    $rollbackErrors = @()
    try { Stop-Hub $paths.State } catch { $rollbackErrors += "stop: $($_.Exception.Message)" }
    Undo-Ops
    if ($previousVenv) {
        Set-Pointer $paths.Pointer $previousVenv
        Set-Pointer $paths.Previous $previousOfPrevious
    } else {
        Remove-Item -LiteralPath $paths.Pointer -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath $paths.Previous -Force -ErrorAction SilentlyContinue
    }
    if ($null -ne $stampBefore) { [System.IO.File]::WriteAllText($paths.Stamp, $stampBefore) }
    else { Remove-Item -LiteralPath $paths.Stamp -Force -ErrorAction SilentlyContinue }
    if ($null -ne $oldStampBefore) { [System.IO.File]::WriteAllText($paths.StampOld, $oldStampBefore) }
    else { Remove-Item -LiteralPath $paths.StampOld -Force -ErrorAction SilentlyContinue }
    if ($previousVenv) {
        try { Start-Hub $Port; Write-Host '[rollback] previous Hub started and healthy' }
        catch { $rollbackErrors += "restart: $($_.Exception.Message)" }
    }
    try { Remove-Tree $stagedVenv } catch { $rollbackErrors += "cleanup: $($_.Exception.Message)" }
    if ($rollbackErrors.Count -gt 0) { throw "deployment failed after the stop ($failure); rollback also failed: $($rollbackErrors -join '; ')" }
    throw "deployment failed after the stop; the previous deployment is restored: $failure"
}

# The deployment stands: ops.previous goes with the environment it replaced, and the older copy is dropped.
Complete-Ops
# Keep the environment this replaced for -Rollback; delete the one before it.
if ($previousOfPrevious -and $previousOfPrevious -ne $stagedVenv -and $previousOfPrevious -ne $previousVenv -and
        $previousOfPrevious.StartsWith($paths.Venvs, [System.StringComparison]::OrdinalIgnoreCase)) {
    try { Remove-Tree $previousOfPrevious } catch { Write-Warning "old environment cleanup failed: $($_.Exception.Message)" }
}
Write-Host "Deployed $Commit (Hub $version)"
Write-Host "  environment: $stagedVenv"
Write-Host "  previous:    $previousVenv"
