@echo off
setlocal

REM Ga naar de map waar dit .bat-bestand staat
pushd "%~dp0"

REM MCP server in apart venster
start "MCP Server" cmd /k "call venv\Scripts\activate.bat & python nao_mcp_server.py"

REM Kleine pauze zodat MCP kan opstarten
timeout /t 2 >nul

REM ngrok in apart venster
start "ngrok" cmd /k "ngrok http 8000"

REM Wacht tot ngrok zijn lokale API draait
timeout /t 5 >nul

REM ngrok basis-URL ophalen via API
for /f "usebackq tokens=*" %%i in (`
  powershell -Command "(Invoke-WebRequest -UseBasicParsing http://127.0.0.1:4040/api/tunnels | ConvertFrom-Json).tunnels | Where-Object { $_.proto -eq 'https' } | Select-Object -First 1 -ExpandProperty public_url"
`) do set NGROK_URL=%%i

REM Volledige MCP endpoint
set "MCP_ENDPOINT=%NGROK_URL%/mcp"

set "URL_FILE=url.txt"
set "OLD_URL="

if exist "%URL_FILE%" (
    set /p OLD_URL=<"%URL_FILE%"
)

echo.
if "%OLD_URL%"=="" (
    echo Geen bestaande URL gevonden, nieuwe URL wordt opgeslagen.
    >"%URL_FILE%" echo %MCP_ENDPOINT%
) else (
    if /I "%MCP_ENDPOINT%"=="%OLD_URL%" (
        echo De MCP endpoint-URL is hetzelfde gebleven:
        echo   %MCP_ENDPOINT%
		echo.
        echo De GPT connector kan ongewijzigd blijven. 
    ) else (
        echo WAARSCHUWING: MCP endpoint is VERANDERD.
        echo   Oude: %OLD_URL%
        echo   Nieuwe: %MCP_ENDPOINT%
        >"%URL_FILE%" echo %MCP_ENDPOINT%
    )
)

echo.
echo Je kunt dit venster nu sluiten
pause
popd
endlocal
