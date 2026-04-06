# After reboot / logon: catch up if today's runs were missed (network was down, PC was off, etc.).
$ErrorActionPreference = 'Stop'
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$Marker = Join-Path $RepoRoot 'artifacts\last_sync_ok.json'
$Py = Join-Path $RepoRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $Py)) { $Py = 'python' }
$Pipeline = Join-Path $RepoRoot 'run_pipeline.py'

Start-Sleep -Seconds 90

$now = Get-Date
if ($now.Hour -lt 5) { exit 0 }

$last = $null
if (Test-Path $Marker) { $last = (Get-Item $Marker).LastWriteTime }

Set-Location $RepoRoot

# No successful sync yet today (after 05:00 local).
if ($null -eq $last -or $last.Date -lt $now.Date) {
    & $Py $Pipeline
    exit $LASTEXITCODE
}

# Past 07:20 and we never got a post-07:00 sync today.
$sevenAm = Get-Date -Hour 7 -Minute 0 -Second 0 -Millisecond 0
if ($now -ge $sevenAm.AddMinutes(20) -and $last -lt $sevenAm -and $last.Date -eq $now.Date) {
    & $Py $Pipeline
    exit $LASTEXITCODE
}

exit 0
