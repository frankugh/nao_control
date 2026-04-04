@echo off
setlocal
cd /d "%~dp0\..\.."

set "PYTHON_EXE=py3_script_runner\venv\Scripts\python.exe"
set "SCRIPT_PATH=py3_script_runner\scripts\example_workshop_summary.json"

if not exist "%PYTHON_EXE%" (
    echo [ERROR] %PYTHON_EXE% niet gevonden. Draai eerst de py3_script_runner venv/install setup.
    pause
    exit /b 1
)

if not exist "%SCRIPT_PATH%" (
    echo [ERROR] %SCRIPT_PATH% niet gevonden.
    pause
    exit /b 1
)

echo [INFO] Start py3_script_runner met %SCRIPT_PATH%
echo [INFO] Sluit dit venster niet tijdens de run.

"%PYTHON_EXE%" -m py3_script_runner.cli --script "%SCRIPT_PATH%" %*
set "EXIT_CODE=%ERRORLEVEL%"

if not "%EXIT_CODE%"=="0" (
    echo [ERROR] Script runner stopte met exit code %EXIT_CODE%.
) else (
    echo [INFO] Klaar.
)

pause
exit /b %EXIT_CODE%
