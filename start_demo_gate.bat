@echo off
setlocal
cd /d "%~dp0"

set "POWERSHELL_EXE=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
set "SCRIPT_PATH=%~dp0start_demo_gate.ps1"

if not exist "%POWERSHELL_EXE%" (
    echo [ERROR] PowerShell niet gevonden: "%POWERSHELL_EXE%"
    pause
    exit /b 1
)

if not exist "%SCRIPT_PATH%" (
    echo [ERROR] start_demo_gate.ps1 niet gevonden.
    pause
    exit /b 1
)

"%POWERSHELL_EXE%" -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT_PATH%" %*
set "EXIT_CODE=%ERRORLEVEL%"

if not "%EXIT_CODE%"=="0" (
    echo [ERROR] Demo gate stopte met exit code %EXIT_CODE%.
) else (
    echo [INFO] Demo gate klaar.
)

pause
exit /b %EXIT_CODE%
