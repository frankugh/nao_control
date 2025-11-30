@echo off
setlocal

set "BASE_DIR=%~dp0"
set "PY2_DIR=%BASE_DIR%nao_base_controller"
set "PY3_DIR=%BASE_DIR%behavior_manager"

set "PY2_EXE=%PY2_DIR%\nao_base_controller.exe"
set "PY3_EXE=%PY3_DIR%\nao_behavior_manager.exe"

set "PY2_PORT=5000"
set "PY3_PORT=5001"

echo ======================================
echo   Start NAO Controllers (Py2 + Py3)
echo ======================================
echo.

REM ----------------------------------------
REM 1. Bestaat Py2-executable?
REM ----------------------------------------
if not exist "%PY2_EXE%" (
    echo [ERROR] Py2 executable ontbreekt:
    echo   %PY2_EXE%
    echo Build was incomplete.
    pause
    exit /b 1
)

REM ----------------------------------------
REM 2. Bestaat Py3-executable?
REM ----------------------------------------
if not exist "%PY3_EXE%" (
    echo [ERROR] Py3 executable ontbreekt:
    echo   %PY3_EXE%
    echo Build was incomplete.
    pause
    exit /b 1
)

REM ----------------------------------------
REM 3. Healthcheck: poorten beschikbaar?
REM ----------------------------------------
echo [INFO] Controleer beschikbare poorten...

REM Check Py2 port
netstat -ano | findstr ":%PY2_PORT% " >nul
if %errorlevel%==0 (
    echo [ERROR] Poort %PY2_PORT% is al in gebruik. Py2 kan niet starten.
    pause
    exit /b 1
)

REM Check Py3 port
netstat -ano | findstr ":%PY3_PORT% " >nul
if %errorlevel%==0 (
    echo [ERROR] Poort %PY3_PORT% is al in gebruik. Py3 kan niet starten.
    pause
    exit /b 1
)

echo [INFO] Alle poorten zijn vrij.
echo.

REM ----------------------------------------
REM 4. Start Py2
REM ----------------------------------------
echo [INFO] Start Py2 Base Controller...
start "NAO Py2" /D "%PY2_DIR%" nao_base_controller.exe

REM ----------------------------------------
REM 5. Start Py3
REM ----------------------------------------
echo [INFO] Start Py3 Behavior Manager...
start "NAO Py3" /D "%PY3_DIR%" nao_behavior_manager.exe

echo.
echo [OK] Beide servers gestart.
echo Dit venster wordt nu gesloten.
timeout /t 2 >nul

endlocal
exit /b 0
