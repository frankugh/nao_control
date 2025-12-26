@echo off
setlocal

REM ==== Paden ====
set "BASE_DIR=%~dp0"
set "PY2_DIR=%BASE_DIR%py2_nao_base_controller"
set "PY3_DIR=%BASE_DIR%py3_nao_behavior_manager"
set "BUILD_FILES_DIR=%BASE_DIR%build_files"
set "OUT_DIR=%BASE_DIR%build"
set "OUT_STACK=%OUT_DIR%"   REM build\ is onze root, subfolders worden erin gemaakt

set "PY2_VENV_PY=%PY2_DIR%\venv\Scripts\python.exe"
set "PY3_VENV_PY=%PY3_DIR%\venv\Scripts\python.exe"

echo [INFO] Build start vanuit %BASE_DIR%

REM =====================================================================
REM ========================   PY2  BUILD   =============í================
REM =====================================================================

echo [INFO] Clean oude Py2 build...
rmdir /s /q "%PY2_DIR%\build" 2>nul
rmdir /s /q "%PY2_DIR%\dist" 2>nul
del "%PY2_DIR%\nao_base_controller.spec" 2>nul

echo [INFO] Build Py2 nao_base_controller.exe...
cd /d "%PY2_DIR%"
"%PY2_VENV_PY%" -m PyInstaller --onedir --name nao_base_controller nao_api.py
if errorlevel 1 (
    echo [ERROR] Py2 build faalde.
    pause
    exit /b 1
)

REM =====================================================================
REM ========================   PY3  BUILD   =============================
REM =====================================================================

echo [INFO] Clean oude Py3 build...
rmdir /s /q "%PY3_DIR%\build" 2>nul
rmdir /s /q "%PY3_DIR%\dist" 2>nul
del "%PY3_DIR%\nao_behavior_manager.spec" 2>nul

echo [INFO] Build Py3 nao_behavior_manager.exe...
cd /d "%PY3_DIR%"
"%PY3_VENV_PY%" -m PyInstaller --onedir --name nao_behavior_manager py3_server.py
if errorlevel 1 (
    echo [ERROR] Py3 build faalde.
    pause
    exit /b 1
)

REM =====================================================================
REM ====================   RUNTIME STRUCTUUR MAKEN   ====================
REM =====================================================================

echo [INFO] Reset output-map...
cd /d "%BASE_DIR%"
rmdir /s /q "%OUT_DIR%" 2>nul
mkdir "%OUT_DIR%" 2>nul

REM ==== Py2 runtime kopiëren ====
echo [INFO] Kopieer Py2 runtime...
robocopy "%PY2_DIR%\dist\nao_base_controller" "%OUT_DIR%\nao_base_controller" /E >nul

REM ==== SDK kopiëren ====
echo [INFO] Kopieer geprune’de NAOqi-SDK...
robocopy "%BUILD_FILES_DIR%\naoqi-sdk" "%OUT_DIR%\nao_base_controller\naoqi-sdk" /MIR >nul

REM ==== Py3 runtime kopiëren ====
echo [INFO] Kopieer Py3 runtime...
robocopy "%PY3_DIR%\dist\nao_behavior_manager" "%OUT_DIR%\behavior_manager" /E >nul

REM ==== Config + start scripts kopiëren ====
echo [INFO] Kopieer config.ini en start_all.bat...
copy /Y "%BASE_DIR%config.ini" "%OUT_DIR%\config.ini" >nul
if exist "%BUILD_FILES_DIR%\start_all.bat" copy /Y "%BUILD_FILES_DIR%\start_all.bat" "%OUT_DIR%\start_all.bat" >nul

REM readme.txt kopiëren (optioneel)
if exist "%BUILD_FILES_DIR%\readme.txt" (
    echo [INFO] Kopieer readme.txt...
    copy /Y "%BUILD_FILES_DIR%\readme.txt" "%OUT_DIR%\readme.txt" >nul
)

echo.
echo [OK] Build voltooid.
echo Output staat in: %OUT_DIR%
pause
endlocal
