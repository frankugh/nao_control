@echo off
call env_config.bat

echo [INFO] Activeer Python2 venv...
call venv\Scripts\activate.bat

echo [INFO] Start NAO Flask API (NAO=%NAO_IP%, Web=%WEB_HOST%:%WEB_PORT%)...
python nao_api.py --host %WEB_HOST% --port %WEB_PORT% --nao_ip %NAO_IP% --nao_port %NAO_PORT%

deactivate
pause
