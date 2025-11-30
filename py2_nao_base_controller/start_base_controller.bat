@echo off
setlocal
cd /d "%~dp0"

if not exist "venv\Scripts\python.exe" (
    echo [ERROR] venv\Scripts\python.exe niet gevonden. Draai eerst install_base_controller.bat
    pause
    exit /b 1
)

echo [INFO] Start Py2 NAO base controller (dev)...
venv\Scripts\python.exe nao_api.py

pause
endlocal
