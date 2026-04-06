# Register Windows Scheduled Tasks: daily 05:00 / 07:00 pipeline + watchdog + logon catch-up.
# Run once in PowerShell (admin not required for "current user" tasks).
# Update $RepoRoot if you move the repo.
$ErrorActionPreference = 'Stop'

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$Py = Join-Path $RepoRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $Py)) {
    Write-Host "No .venv found; tasks will use 'python' on PATH. Create a venv in the repo root for reliability."
    $Py = (Get-Command python -ErrorAction Stop).Source
}

$Pipeline = Join-Path $RepoRoot 'run_pipeline.py'
$Watchdog = Join-Path $RepoRoot 'scripts\watchdog.ps1'
$WatchdogBoot = Join-Path $RepoRoot 'scripts\watchdog_boot.ps1'
$Pwsh = Join-Path $env:WINDIR 'System32\WindowsPowerShell\v1.0\powershell.exe'

if (-not (Test-Path $Pipeline)) { throw "Missing run_pipeline.py at $Pipeline" }

$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable

function Register-OurTask {
    param(
        [string]$Name,
        [string]$Execute,
        [string]$Argument,
        [string]$WorkingDirectory,
        [CimInstance[]]$Triggers
    )
    Unregister-ScheduledTask -TaskName $Name -Confirm:$false -ErrorAction SilentlyContinue
    $action = New-ScheduledTaskAction -Execute $Execute -Argument $Argument -WorkingDirectory $WorkingDirectory
    Register-ScheduledTask -TaskName $Name -Action $action -Trigger $Triggers -Principal $principal -Settings $settings -Force | Out-Null
    Write-Host "Registered: $Name"
}

# --- Main pipeline: 05:00 and 07:00 JST (uses machine local time; set Windows TZ to Tokyo if needed) ---
$t0500 = New-ScheduledTaskTrigger -Daily -At '05:00'
$t0700 = New-ScheduledTaskTrigger -Daily -At '07:00'

Register-OurTask -Name 'SapmedPipeline0500' -Execute $Py -Argument "`"$Pipeline`"" -WorkingDirectory $RepoRoot -Triggers @($t0500)
Register-OurTask -Name 'SapmedPipeline0700' -Execute $Py -Argument "`"$Pipeline`"" -WorkingDirectory $RepoRoot -Triggers @($t0700)

# --- Watchdog: retry if primary slot did not produce last_sync_ok today ---
$t0525 = New-ScheduledTaskTrigger -Daily -At '05:25'
$t0725 = New-ScheduledTaskTrigger -Daily -At '07:25'
$argWd1 = "-NoProfile -ExecutionPolicy Bypass -File `"$Watchdog`" -Phase After0500"
$argWd2 = "-NoProfile -ExecutionPolicy Bypass -File `"$Watchdog`" -Phase After0700"

Register-OurTask -Name 'SapmedWatchdog0525' -Execute $Pwsh -Argument $argWd1 -WorkingDirectory $RepoRoot -Triggers @($t0525)
Register-OurTask -Name 'SapmedWatchdog0725' -Execute $Pwsh -Argument $argWd2 -WorkingDirectory $RepoRoot -Triggers @($t0725)

# --- Logon: catch-up after reboot (delayed inside script) ---
$tLogon = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
Register-OurTask -Name 'SapmedBootCatchup' -Execute $Pwsh -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$WatchdogBoot`"" -WorkingDirectory $RepoRoot -Triggers @($tLogon)

Write-Host ""
Write-Host "Done. Tasks (local user, Tokyo time = Windows display time):"
Write-Host "  SapmedPipeline0500 / SapmedPipeline0700  -> python run_pipeline.py"
Write-Host "  SapmedWatchdog0525 / SapmedWatchdog0725 -> retry if sync stamp missing"
Write-Host "  SapmedBootCatchup -> logon catch-up (~90s delay)"
Write-Host "Open Task Scheduler (taskschd.msc) to verify or adjust triggers."
