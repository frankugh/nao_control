@echo off
setlocal
cd /d "%~dp0"

echo [INFO] Start Py2 NAO base controller in nieuw venster...
start "NAO Py2 Base Controller" cmd /k "%~dp0start_base_controller.bat"

echo [INFO] Start Py3 behavior manager in nieuw venster...
start "NAO Py3 Behavior Manager" cmd /k "%~dp0start_behavior_manager.bat"

echo [INFO] Beide service-launchers zijn gestart.
pause
endlocal
