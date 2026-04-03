@echo off
setlocal
cd /d "%~dp0\.."
call "py3_dialog_manager\start_dialog_manager.bat" --host 127.0.0.1 --port 5301 --preset alex
endlocal
