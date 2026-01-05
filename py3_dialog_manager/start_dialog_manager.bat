@echo off
setlocal
cd /d "%~dp0"

set "PS1=%~dp0start_dialog_manager.ps1"
set "WT=%LOCALAPPDATA%\Microsoft\WindowsApps\wt.exe"

if not exist "%PS1%" (
  echo [ERROR] PS1 niet gevonden: %PS1%
  pause
  exit /b 1
)

if not exist "%WT%" (
  echo [ERROR] wt.exe niet gevonden op: %WT%
  pause
  exit /b 1
)

REM Strip trailing backslash van %~dp0 (WT -d kan daarop struikelen)
set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"

echo [INFO] Starting Windows Terminal...
echo [INFO] ROOT=%ROOT%
echo [INFO] PS1=%PS1%

REM Start WT als apart proces/venster
start "" "%WT%" -d "%ROOT%" powershell.exe -NoExit -ExecutionPolicy Bypass -File "%PS1%"

REM Laat dit venster open zodat je errors ziet als start faalt
echo [INFO] If no Terminal window opened, something failed above.
endlocal
