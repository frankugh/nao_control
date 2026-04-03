@echo off
setlocal
cd /d "%~dp0"

if not exist "venv\Scripts\activate.bat" (
  echo [ERROR] venv\Scripts\activate.bat niet gevonden. Draai eerst de install/venv setup.
  pause
  exit /b 1
)

echo [INFO] Start NAO studio...

call "venv\Scripts\activate.bat"
python webapp_server.py %*

pause
endlocal
