@echo off
REM 봇(SapmedTelegramBotDaemon) 재시작 — 더블클릭 또는 CMD에서:  .\restart-bot.cmd
REM (주의) Windows CMD는 ./ 가 아니라 .\ 슬래시 방향이 다릅니다.
setlocal
cd /d "%~dp0"
title Sapmed — bot restart
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\restart_bot_daemon.ps1"
set EXITCODE=%ERRORLEVEL%
echo.
if /I not "%~1"=="nopause" pause
exit /b %EXITCODE%
