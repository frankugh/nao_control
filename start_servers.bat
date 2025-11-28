@echo off
call "%~dp0env_config.bat"

echo [INFO] Start Py2 NAO base controller in nieuw venster...
start "NAO Py2 Base Controller" cmd /k "%~dp0py2_nao_base_controller\start_base_controller.bat"

echo [INFO] Start Py3 behavior manager in nieuw venster...
start "NAO Py3 Behavior Manager" cmd /k "%~dp0py3_nao_behavior_manager\start_behavior_manager.bat"

echo [INFO] Beide servers zijn gestart (als alles goed ging).
pause
