# Register SapmedTelegramBotDaemon: logon + every-5min wake (MultipleInstances IgnoreNew).
# Inner bot_daemon_loop.ps1 restarts Python forever; periodic trigger revives dead wrapper.
#
# Remove:  Unregister-ScheduledTask -TaskName 'SapmedTelegramBotDaemon' -Confirm:$false
#
$ErrorActionPreference = 'Stop'

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$LoopScript = Join-Path $RepoRoot 'scripts\bot_daemon_loop.ps1'
$Pwsh = Join-Path $env:WINDIR 'System32\WindowsPowerShell\v1.0\powershell.exe'

if (-not (Test-Path $LoopScript)) { throw "Missing $LoopScript" }

$TaskName = 'SapmedTelegramBotDaemon'
$Arg = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$LoopScript`""

$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

function New-DaemonSettings {
    param([switch]$IncludeTaskRestarts)
    if ($IncludeTaskRestarts) {
        return New-ScheduledTaskSettingsSet `
            -AllowStartIfOnBatteries `
            -DontStopIfGoingOnBatteries `
            -StartWhenAvailable `
            -MultipleInstances IgnoreNew `
            -ExecutionTimeLimit ([TimeSpan]::Zero) `
            -RestartCount 999 `
            -RestartInterval (New-TimeSpan -Seconds 30)
    }
    return New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -StartWhenAvailable `
        -MultipleInstances IgnoreNew `
        -ExecutionTimeLimit ([TimeSpan]::Zero)
}

$tLogon = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME -RandomDelay (New-TimeSpan -Seconds 45)

$repAt = (Get-Date).AddMinutes(1)
$tRepeat = New-ScheduledTaskTrigger -Once -At $repAt `
    -RepetitionInterval (New-TimeSpan -Minutes 5) `
    -RepetitionDuration (New-TimeSpan -Days 9999)

Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue

$action = New-ScheduledTaskAction -Execute $Pwsh -Argument $Arg -WorkingDirectory $RepoRoot

$registered = $false
foreach ($extra in @($true, $false)) {
    try {
        $settings = New-DaemonSettings -IncludeTaskRestarts:$extra
        Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger @($tLogon, $tRepeat) -Principal $principal -Settings $settings -Force | Out-Null
        $registered = $true
        if (-not $extra) {
            Write-Host "Registered without Task Scheduler 'restart on failure'; loop + 5-min trigger still apply." -ForegroundColor Yellow
        }
        break
    } catch {
        if ($extra) { continue }
        Write-Host ""
        Write-Host "Register-ScheduledTask failed: $($_.Exception.Message)" -ForegroundColor Red
        Write-Host "Run in a normal desktop PowerShell as the user that will host the bot." -ForegroundColor Yellow
        exit 1
    }
}
if (-not $registered) { throw 'Register-ScheduledTask did not complete.' }

Write-Host ""
Write-Host "Registered: $TaskName"
Write-Host "  Triggers: At logon (+45s jitter) + every 5 min (starts only if not already running)"
Write-Host "  Log: $(Join-Path $RepoRoot 'logs\bot_daemon.log')"
Write-Host ""
Write-Host "Start now:  Start-ScheduledTask -TaskName '$TaskName'"
Write-Host "Restart:    .\restart-bot.cmd   (or .\scripts\restart_bot_daemon.ps1)"
Write-Host "Status:     Get-ScheduledTask -TaskName '$TaskName' | Get-ScheduledTaskInfo"
