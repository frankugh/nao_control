@echo off
call "%~dp0env_config.bat"

echo [INFO] Ga naar Py2-controller map...
cd /d "%~dp0py2_nao_base_controller"

echo [INFO] Installeer/upgrade tools voor Python2...
"%PYTHON2%" -m pip install --upgrade pip setuptools
"%PYTHON2%" -m pip install --upgrade virtualenv

if not exist "venv\Scripts\python.exe" (
    echo [INFO] Maak nieuwe Python2-venv in %CD%\venv ...
    "%PYTHON2%" -m virtualenv venv
) else (
    echo [INFO] Bestaande venv gevonden, gebruik die...
)

echo [INFO] Installeer requirements in venv...
venv\Scripts\python.exe -m pip install -r requirements.txt

echo [INFO] Py2-installatie voltooid.
pause
