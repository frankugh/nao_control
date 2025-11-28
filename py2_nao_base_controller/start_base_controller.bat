@echo off
call "%~dp0..\env_config.bat"

echo [INFO] Ga naar Py2-controller map...
cd /d "%~dp0"

set "PY2_VENV_PY=venv\Scripts\python.exe"

if not exist "%PY2_VENV_PY%" (
    echo [ERROR] %PY2_VENV_PY% niet gevonden. Draai eerst install_base_controller.bat
    pause
    exit /b 1
)

echo [INFO] Python versie (moet 2.7.x zijn):
"%PY2_VENV_PY%" --version

echo [INFO] Start NAO Py2 Flask API...
"%PY2_VENV_PY%" nao_api.py ^
  --host %PY2_WEB_HOST% ^
  --port %PY2_WEB_PORT% ^
  --nao_ip %NAO_IP% ^
  --nao_port %NAO_PORT%

pause
