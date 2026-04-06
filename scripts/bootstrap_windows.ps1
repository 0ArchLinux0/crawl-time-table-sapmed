# 한 번에: venv, 패키지, Chromium, 절전 깨우기(타이머), 작업 스케줄러 등록.
$ErrorActionPreference = 'Stop'
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Set-Location $RepoRoot

if (-not (Test-Path (Join-Path $RepoRoot '.venv'))) {
    python -m venv .venv
}
& .\.venv\Scripts\pip.exe install -U pip
& .\.venv\Scripts\pip.exe install -r requirements.txt
& .\.venv\Scripts\python.exe -m playwright install chromium

# 스케줄 작업이 절전에서 깨울 수 있게(현재 전원 프로필; 실패 시 관리자 권한으로 다시 실행)
$subSleep = '238c9fa8-0aad-41ed-83f4-97be242c8f20'
$rtcWake = 'bd3b718a-0680-4d9d-8ab2-e1d2b4ac806d'
powercfg /SETACVALUEINDEX SCHEME_CURRENT $subSleep $rtcWake 1 2>$null
powercfg /SETDCVALUEINDEX SCHEME_CURRENT $subSleep $rtcWake 1 2>$null
powercfg /SetActive SCHEME_CURRENT 2>$null

& powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'install_scheduled_tasks.ps1')
Write-Host 'bootstrap_windows: done'
