@echo off
call "%~dp0..\env_config.bat"

echo [INFO] Ga naar Py3 behavior manager map...
cd /d "%~dp0"

set "PY3_VENV_PY=venv\Scripts\python.exe"

if not exist "%PY3_VENV_PY%" (
    echo [ERROR] %PY3_VENV_PY% niet gevonden. Draai eerst install_behavior_manager.bat
    pause
    exit /b 1
)

echo [INFO] Python versie (moet 3.x zijn):
"%PY3_VENV_PY%" --version

REM LET OP: hier ging het mis → gebruik 127.0.0.1 i.p.v. PY2_WEB_HOST
set "PY2_NAO_API_URL=http://127.0.0.1:%PY2_WEB_PORT%"

set "PY3_WEB_HOST=%PY3_WEB_HOST%"
set "PY3_WEB_PORT=%PY3_WEB_PORT%"

echo [INFO] Start Py3 NAO Behavior Manager API...
"%PY3_VENV_PY%" py3_server.py

pause
