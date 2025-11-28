@echo off
call "%~dp0..\env_config.bat"

echo [INFO] Ga naar Py3 behavior manager map...
cd /d "%~dp0"

if not exist "venv\Scripts\python.exe" (
    echo [INFO] Maak nieuwe Python3-venv in %CD%\venv ...
    "%PYTHON3%" -m venv venv
) else (
    echo [INFO] Bestaande Py3-venv gevonden, gebruik die...
)

set "PY3_VENV_PY=venv\Scripts\python.exe"

echo [INFO] Installeer Py3 dependencies...
REM Als requirements.txt in de root ligt: gebruik ..\requirements.txt
"%PY3_VENV_PY%" -m pip install --upgrade pip
"%PY3_VENV_PY%" -m pip install -r ..\requirements.txt

echo [INFO] Klaar met Py3-installatie.
pause
