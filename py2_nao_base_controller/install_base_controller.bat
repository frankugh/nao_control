@echo off
setlocal
cd /d "%~dp0"

if not exist "venv" (
    echo [INFO] Maak Py2 venv...
    "C:\Python27\python.exe" -m virtualenv venv
)

echo [INFO] Installeer dependencies (Py2)...
venv\Scripts\python.exe -m pip install --upgrade pip
venv\Scripts\python.exe -m pip install -r requirements.txt

echo [OK] Py2 base controller klaar.
pause
endlocal
