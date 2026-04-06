# Retry pipeline if the scheduled 05:00 / 07:00 run likely did not finish successfully.
# Invoked by Task Scheduler at 05:25 and 07:25 (see install_scheduled_tasks.ps1).
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('After0500', 'After0700')]
    [string]$Phase
)

$ErrorActionPreference = 'Stop'
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$Marker = Join-Path $RepoRoot 'artifacts\last_sync_ok.json'
$Py = Join-Path $RepoRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $Py)) { $Py = 'python' }
$Pipeline = Join-Path $RepoRoot 'run_pipeline.py'

function Get-LastSyncTime {
    if (-not (Test-Path $Marker)) { return $null }
    return (Get-Item $Marker).LastWriteTime
}

$now = Get-Date
$last = Get-LastSyncTime

function Invoke-Pipeline {
    Set-Location $RepoRoot
    & $Py $Pipeline
    exit $LASTEXITCODE
}

if ($Phase -eq 'After0500') {
    # After 05:25: ensure we have a sync stamped today (local calendar date).
    if ($null -eq $last -or $last.Date -lt $now.Date) {
        Invoke-Pipeline
    }
    exit 0
}

# After0700: catch missed 07:00 task — no stamp today, or only a pre-07:00 run today.
$sevenAm = Get-Date -Hour 7 -Minute 0 -Second 0 -Millisecond 0
if ($now -lt $sevenAm.AddMinutes(20)) { exit 0 }

$stale = ($null -eq $last) -or ($last.Date -lt $now.Date)
$onlyEarly = (-not $stale) -and ($last.Date -eq $now.Date) -and ($last -lt $sevenAm)
if ($stale -or $onlyEarly) {
    Invoke-Pipeline
}
exit 0
