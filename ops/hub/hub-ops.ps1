# The deployed copy of the supervisor scripts (<DeployDir>\ops) and the copy it replaced (ops.previous), which travel with
# the environments in .venv-path and .venv-previous. Dot-sourced by deploy-hub.ps1; needs $paths (Get-HubPaths) and may
# read $DryRun. Kept apart from the deployment so tests/test_deployment.py can run it on a scratch directory.

function Remove-Tree([string]$Path) {
    if ($DryRun) { Write-Host "[dry run] Remove-Item -Recurse $Path"; return }
    if (Test-Path -LiteralPath $Path) { Remove-Item -LiteralPath $Path -Recurse -Force }
}

function Copy-Ops([string]$Source = $PSScriptRoot) {
    # The task runs the deployment's own copy of these scripts (install-task.ps1), so a checkout change can't alter it.
    # The copy this replaces is kept as ops.previous, so a rollback restores the supervisor that went with the old Hub.
    $target = Join-Path $paths.Deploy 'ops'
    $old = Join-Path $paths.Deploy 'ops.previous'
    if ($DryRun) { Write-Host "[dry run] ops -> ops.previous; copy $Source\*.ps1 -> $target"; return }
    Remove-Tree $old
    if (Test-Path -LiteralPath $target) { Move-Item -LiteralPath $target -Destination $old }
    New-Item -ItemType Directory -Force -Path $target | Out-Null
    Copy-Item -Path (Join-Path $Source '*.ps1') -Destination $target -Force
}

function Swap-Ops {
    # ops and ops.previous trade places (a rollback, or the undo of one).
    $current = Join-Path $paths.Deploy 'ops'
    $other = Join-Path $paths.Deploy 'ops.previous'
    if (-not (Test-Path -LiteralPath $other)) { return }
    $temporary = Join-Path $paths.Deploy 'ops.swap'
    Remove-Tree $temporary
    if (Test-Path -LiteralPath $current) { Move-Item -LiteralPath $current -Destination $temporary }
    Move-Item -LiteralPath $other -Destination $current
    if (Test-Path -LiteralPath $temporary) { Move-Item -LiteralPath $temporary -Destination $other }
}
