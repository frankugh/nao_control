@echo off
call env_config.bat

echo [INFO] Controleer of virtualenv aanwezig is...
"%PYTHON2%" -m pip install --upgrade pip setuptools
"%PYTHON2%" -m pip install virtualenv

if not exist venv (
    echo [INFO] Maak Python2 virtualenv...
    "%PYTHON2%" -m virtualenv venv
)

echo [INFO] Activeer venv en installeer requirements...
call venv\Scripts\activate.bat
pip install -r requirements.txt
deactivate

echo [INFO] Installatie klaar.
pause
