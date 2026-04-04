@echo off
setlocal

set "SCRIPT_PATH=%~dp0install_repo.ps1"
if not exist "%SCRIPT_PATH%" (
  echo [install] ERROR: install_repo.ps1 niet gevonden: %SCRIPT_PATH%
  pause
  exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT_PATH%"
set "EXIT_CODE=%ERRORLEVEL%"

pause
exit /b %EXIT_CODE%
