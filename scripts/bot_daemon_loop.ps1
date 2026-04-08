# Runs main_bot.py forever: any exit or uncaught error -> sleep -> retry (never gives up).
# Intended to be started by Task Scheduler (see install_bot_daemon_task.ps1).
# Logs restarts to logs/bot_daemon.log (Python also writes logs/bot.log).
$ErrorActionPreference = 'Continue'

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$PyVenv = Join-Path $RepoRoot '.venv\Scripts\python.exe'
$Py = if (Test-Path -LiteralPath $PyVenv) { $PyVenv } else { $null }
$Bot = Join-Path $RepoRoot 'main_bot.py'
if (-not (Test-Path $Bot)) {
    throw "Missing main_bot.py at $Bot -- cannot start daemon."
}

$LogDir = Join-Path $RepoRoot 'logs'
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$DaemonLog = Join-Path $LogDir 'bot_daemon.log'

function Write-DaemonLog {
    param([string]$Message)
    try {
        $line = "{0} {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Message
        Add-Content -LiteralPath $DaemonLog -Value $line -Encoding utf8 -ErrorAction SilentlyContinue
    } catch { }
}

Set-Location -LiteralPath $RepoRoot
Write-DaemonLog "bot_daemon: loop started (repo=$RepoRoot)"

while ($true) {
    try {
        if ([string]::IsNullOrEmpty($Py) -or -not (Test-Path -LiteralPath $Py)) {
            $Py = (Get-Command python -ErrorAction Stop).Source
        }
        Write-DaemonLog 'bot_daemon: launching main_bot.py'
        try {
            & $Py $Bot
        } catch {
            Write-DaemonLog ("bot_daemon: invoke failed: " + $_.Exception.Message)
        }
        $code = $LASTEXITCODE
        Write-DaemonLog "bot_daemon: main_bot ended exit=$code ; pausing 15s then restart"
    } catch {
        Write-DaemonLog ("bot_daemon: outer error (will retry): " + $_.Exception.Message)
    }
    try {
        Start-Sleep -Seconds 15
    } catch {
        Start-Sleep -Seconds 15
    }
}
