@echo off
setlocal
cd /d "%~dp0\..\.."

if not exist "py2_nao_base_controller\venv\Scripts\python.exe" (
  echo [ERROR] py2_nao_base_controller\venv\Scripts\python.exe niet gevonden. Draai eerst de py2_nao_base_controller venv/install setup.
  pause
  exit /b 1
)

set "BASE_PORT=5101"
set /p "BASE_PORT=Op welke port wil je de base controller starten? [5101] "
if "%BASE_PORT%"=="" set "BASE_PORT=5101"

echo [INFO] Start base controller op http://127.0.0.1:%BASE_PORT%
py2_nao_base_controller\venv\Scripts\python.exe py2_nao_base_controller\nao_api.py --host 127.0.0.1 --port %BASE_PORT%

pause
endlocal
