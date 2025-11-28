@echo off
call "%~dp0..\env_config.bat"

echo [INFO] Ga naar Py2-controller map...
cd /d "%~dp0"

echo [INFO] Installeer/upgrade tools voor Python2...
"%PYTHON2%" -m pip install --upgrade pip setuptools
"%PYTHON2%" -m pip install --upgrade virtualenv

if not exist "venv\Scripts\python.exe" (
    echo [INFO] Maak nieuwe Python2-venv in %CD%\venv ...
    "%PYTHON2%" -m virtualenv venv
) else (
    echo [INFO] Bestaande venv gevonden, gebruik die...
)

set "PY2_VENV_PY=venv\Scripts\python.exe"

echo [INFO] Installeer Python2 dependencies...
"%PY2_VENV_PY%" -m pip install --upgrade -r requirements.txt

echo [INFO] Klaar met Py2-installatie.
pause
