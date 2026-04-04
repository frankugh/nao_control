@echo off
setlocal
cd /d "%~dp0\..\.."

if not exist "py3_nao_behavior_manager\venv\Scripts\python.exe" (
  echo [ERROR] py3_nao_behavior_manager\venv\Scripts\python.exe niet gevonden. Draai eerst de py3_nao_behavior_manager venv/install setup.
  pause
  exit /b 1
)

set "BEHAVIOR_PORT=5201"
set /p "BEHAVIOR_PORT=Op welke port wil je de behavior manager starten? [5201] "
if "%BEHAVIOR_PORT%"=="" set "BEHAVIOR_PORT=5201"

echo [INFO] Start behavior manager op http://127.0.0.1:%BEHAVIOR_PORT%
py3_nao_behavior_manager\venv\Scripts\python.exe py3_nao_behavior_manager\py3_server.py --host 127.0.0.1 --port %BEHAVIOR_PORT%

pause
endlocal
