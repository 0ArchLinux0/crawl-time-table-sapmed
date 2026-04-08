# SapmedTelegramBotDaemon 재시작 — PowerShell:  .\restart-bot.ps1
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
& "$PSScriptRoot\scripts\restart_bot_daemon.ps1"
