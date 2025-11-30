@echo off
setlocal
cd /d "%~dp0"

if not exist "venv\Scripts\python.exe" (
    echo [ERROR] venv\Scripts\python.exe niet gevonden. Draai eerst install_behavior_manager.bat
    pause
    exit /b 1
)

echo [INFO] Start Py3 NAO behavior manager (dev)...
venv\Scripts\python.exe py3_server.py

pause
endlocal
