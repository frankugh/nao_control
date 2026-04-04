@echo off
setlocal
cd /d "%~dp0\..\.."

if not exist "py3_dialog_manager\venv\Scripts\python.exe" (
  echo [ERROR] py3_dialog_manager\venv\Scripts\python.exe niet gevonden. Draai eerst de install/venv setup.
  pause
  exit /b 1
)

echo [INFO] Start DM Renee op http://127.0.0.1:5302/
start "" powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Sleep -Seconds 2; Start-Process 'http://127.0.0.1:5302/'"
py3_dialog_manager\venv\Scripts\python.exe py3_dialog_manager\webapp_server.py --host 127.0.0.1 --port 5302 --preset renee

pause
endlocal
