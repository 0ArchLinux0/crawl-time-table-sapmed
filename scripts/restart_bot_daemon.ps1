# Restart SapmedTelegramBotDaemon (stop scheduled task, then start again).
# Stops the task shell; child python may take a few seconds to exit.
# Run from repo root:
#   .\restart-bot.cmd
# Or (any cwd):
#   powershell -NoProfile -ExecutionPolicy Bypass -File <repo>\scripts\restart_bot_daemon.ps1
$ErrorActionPreference = 'Stop'

$TaskName = 'SapmedTelegramBotDaemon'
$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if (-not $task) {
    Write-Host "Task not found: $TaskName — run scripts\install_bot_daemon_task.ps1 first." -ForegroundColor Red
    exit 1
}

Write-Host "Stopping $TaskName ..."
Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
Start-Sleep -Seconds 4

Write-Host "Starting $TaskName ..."
Start-ScheduledTask -TaskName $TaskName
Start-Sleep -Seconds 2

$task = Get-ScheduledTask -TaskName $TaskName
$info = $task | Get-ScheduledTaskInfo
Write-Host ""
Write-Host "State: $($task.State)  LastRun: $($info.LastRunTime)  LastResult: $($info.LastTaskResult)"
Write-Host "Daemon log: $(Join-Path (Resolve-Path (Join-Path $PSScriptRoot '..')).Path 'logs\bot_daemon.log')"
Write-Host "Done."
