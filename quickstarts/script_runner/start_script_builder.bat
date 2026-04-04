@echo off
setlocal
cd /d "%~dp0\..\.."

if not exist "py3_script_runner\venv\Scripts\python.exe" (
    echo [ERROR] py3_script_runner\venv\Scripts\python.exe niet gevonden. Draai eerst de py3_script_runner venv/install setup.
    pause
    exit /b 1
)

echo [INFO] Start Script Builder...
echo [INFO] Default URL: http://127.0.0.1:8765/
echo [INFO] Extra argumenten worden doorgestuurd, bijvoorbeeld: --port 8770 --no-browser

py3_script_runner\venv\Scripts\python.exe py3_script_runner\script_runner_app.py %*

pause
endlocal
