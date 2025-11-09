@echo off
call env_config.bat

REM Kies client host: als WEB_HOST=0.0.0.0 of 127.0.0.1 → gebruik localhost
set TEST_HOST=%WEB_HOST%
if "%TEST_HOST%"=="0.0.0.0" set TEST_HOST=127.0.0.1

REM Open browser automatisch
start http://%TEST_HOST%:%WEB_PORT%/ping

pause
