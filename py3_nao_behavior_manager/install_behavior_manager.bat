@echo off
setlocal
cd /d "%~dp0"

if not exist "venv" (
    echo [INFO] Maak Py3 venv...
    py -3 -m venv venv
)

echo [INFO] Installeer dependencies (Py3)...
venv\Scripts\python.exe -m pip install --upgrade pip
venv\Scripts\python.exe -m pip install -r requirements.txt

echo [OK] Py3 behavior manager klaar.
pause
endlocal
