@echo off
setlocal EnableDelayedExpansion

set "ROOT_DIR=%~dp0"
set "DM_DIR=%ROOT_DIR%py3_dialog_manager"
set "DM_PY_EXE=%DM_DIR%\venv\Scripts\python.exe"
set "SR_DIR=%ROOT_DIR%py3_script_runner"
set "SR_PY_EXE=%SR_DIR%\venv\Scripts\python.exe"
set "DM_TEST_TARGET=py3_dialog_manager/tests"
set "SR_TEST_TARGET=py3_script_runner/tests"

set "ONLY_COLLECT=0"
set "FINAL_EXIT=0"

set "DM_COLLECT_EXIT=0"
set "SR_COLLECT_EXIT=0"
set "DM_PERF_COLLECT_EXIT=0"
set "SR_PERF_COLLECT_EXIT=0"
set "DM_RUN_EXIT=n/a"
set "SR_RUN_EXIT=n/a"

set "DM_ALL_COUNT=0"
set "DM_PERF_COUNT=0"
set "DM_NON_PERF_COUNT=0"
set "SR_ALL_COUNT=0"
set "SR_PERF_COUNT=0"
set "SR_NON_PERF_COUNT=0"
set "TOTAL_PERF_COUNT=0"

echo %* | findstr /I /C:"--collect-only" >nul
if "%ERRORLEVEL%"=="0" set "ONLY_COLLECT=1"

if not exist "%DM_PY_EXE%" (
  echo [all-tests] ERROR: Python venv not found: "%DM_PY_EXE%"
  echo [all-tests] Expected venv at: py3_dialog_manager\venv
  set "FINAL_EXIT=1"
)
if not exist "%SR_PY_EXE%" (
  echo [all-tests] ERROR: Python venv not found: "%SR_PY_EXE%"
  echo [all-tests] Expected venv at: py3_script_runner\venv
  set "FINAL_EXIT=1"
)
if not "%FINAL_EXIT%"=="0" goto summary

pushd "%ROOT_DIR%" >nul
if errorlevel 1 (
  echo [all-tests] ERROR: Could not enter repo root "%ROOT_DIR%"
  set "FINAL_EXIT=1"
  goto summary
)

echo [all-tests] Dialog manager target : %DM_TEST_TARGET%
echo [all-tests] Script runner target  : %SR_TEST_TARGET%
echo [all-tests] Extra args            : %*
echo [all-tests] Includes              : perf + non-perf ^(no marker filter^)
echo.

echo [all-tests] Step 1/4 - Discover dialog manager tests ^(all + perf^)
set "TMP_DM_ALL=%TEMP%\dm_collect_all_%RANDOM%_%RANDOM%.txt"
set "TMP_DM_PERF=%TEMP%\dm_collect_perf_%RANDOM%_%RANDOM%.txt"
"%DM_PY_EXE%" -m pytest --collect-only -q %DM_TEST_TARGET% %* > "%TMP_DM_ALL%" 2>&1
set "DM_COLLECT_EXIT=%ERRORLEVEL%"
for /f %%C in ('find /c "::" ^< "%TMP_DM_ALL%"') do set "DM_ALL_COUNT=%%C"

"%DM_PY_EXE%" -m pytest --collect-only -q %DM_TEST_TARGET% -m perf %* > "%TMP_DM_PERF%" 2>&1
set "DM_PERF_COLLECT_EXIT=%ERRORLEVEL%"
if "%DM_PERF_COLLECT_EXIT%"=="5" set "DM_PERF_COLLECT_EXIT=0"
for /f %%C in ('find /c "::" ^< "%TMP_DM_PERF%"') do set "DM_PERF_COUNT=%%C"

set /a "DM_NON_PERF_COUNT=%DM_ALL_COUNT%-%DM_PERF_COUNT%"
if %DM_NON_PERF_COUNT% LSS 0 set "DM_NON_PERF_COUNT=0"
echo [all-tests]   selected all tests : %DM_ALL_COUNT%
echo [all-tests]   selected perf tests: %DM_PERF_COUNT%
echo [all-tests]   selected non-perf  : %DM_NON_PERF_COUNT%
if not "%DM_COLLECT_EXIT%"=="0" (
  echo [all-tests]   ERROR: all-test collect failed ^(exit %DM_COLLECT_EXIT%^)
  set "FINAL_EXIT=1"
)
if not "%DM_PERF_COLLECT_EXIT%"=="0" (
  echo [all-tests]   ERROR: perf-test collect failed ^(exit %DM_PERF_COLLECT_EXIT%^)
  set "FINAL_EXIT=1"
)

echo.
echo [all-tests] Step 2/4 - Discover script runner tests ^(all + perf^)
set "TMP_SR_ALL=%TEMP%\sr_collect_all_%RANDOM%_%RANDOM%.txt"
set "TMP_SR_PERF=%TEMP%\sr_collect_perf_%RANDOM%_%RANDOM%.txt"
"%SR_PY_EXE%" -m pytest --collect-only -q %SR_TEST_TARGET% %* > "%TMP_SR_ALL%" 2>&1
set "SR_COLLECT_EXIT=%ERRORLEVEL%"
for /f %%C in ('find /c "::" ^< "%TMP_SR_ALL%"') do set "SR_ALL_COUNT=%%C"

"%SR_PY_EXE%" -m pytest --collect-only -q %SR_TEST_TARGET% -m perf %* > "%TMP_SR_PERF%" 2>&1
set "SR_PERF_COLLECT_EXIT=%ERRORLEVEL%"
if "%SR_PERF_COLLECT_EXIT%"=="5" set "SR_PERF_COLLECT_EXIT=0"
for /f %%C in ('find /c "::" ^< "%TMP_SR_PERF%"') do set "SR_PERF_COUNT=%%C"

set /a "SR_NON_PERF_COUNT=%SR_ALL_COUNT%-%SR_PERF_COUNT%"
if %SR_NON_PERF_COUNT% LSS 0 set "SR_NON_PERF_COUNT=0"
echo [all-tests]   selected all tests : %SR_ALL_COUNT%
echo [all-tests]   selected perf tests: %SR_PERF_COUNT%
echo [all-tests]   selected non-perf  : %SR_NON_PERF_COUNT%
if not "%SR_COLLECT_EXIT%"=="0" (
  echo [all-tests]   ERROR: all-test collect failed ^(exit %SR_COLLECT_EXIT%^)
  set "FINAL_EXIT=1"
)
if not "%SR_PERF_COLLECT_EXIT%"=="0" (
  echo [all-tests]   ERROR: perf-test collect failed ^(exit %SR_PERF_COLLECT_EXIT%^)
  set "FINAL_EXIT=1"
)

if "%ONLY_COLLECT%"=="1" goto cleanup

echo.
echo [all-tests] Step 3/4 - Run dialog manager tests ^(all markers^)
if not "%DM_COLLECT_EXIT%"=="0" (
  echo [all-tests]   SKIP: collect failed.
  set "DM_RUN_EXIT=1"
  set "FINAL_EXIT=1"
) else (
  echo [all-tests]   Command: "%DM_PY_EXE%" -m pytest %DM_TEST_TARGET% -ra --durations=15 %*
  "%DM_PY_EXE%" -m pytest %DM_TEST_TARGET% -ra --durations=15 %*
  set "DM_RUN_EXIT=!ERRORLEVEL!"
  if not "!DM_RUN_EXIT!"=="0" set "FINAL_EXIT=1"
)

echo.
echo [all-tests] Step 4/4 - Run script runner tests ^(all markers^)
if not "%SR_COLLECT_EXIT%"=="0" (
  echo [all-tests]   SKIP: collect failed.
  set "SR_RUN_EXIT=1"
  set "FINAL_EXIT=1"
) else (
  echo [all-tests]   Command: "%SR_PY_EXE%" -m pytest %SR_TEST_TARGET% -ra --durations=15 %*
  "%SR_PY_EXE%" -m pytest %SR_TEST_TARGET% -ra --durations=15 %*
  set "SR_RUN_EXIT=!ERRORLEVEL!"
  if not "!SR_RUN_EXIT!"=="0" set "FINAL_EXIT=1"
)

:cleanup
if exist "%TMP_DM_ALL%" del /q "%TMP_DM_ALL%" >nul 2>&1
if exist "%TMP_DM_PERF%" del /q "%TMP_DM_PERF%" >nul 2>&1
if exist "%TMP_SR_ALL%" del /q "%TMP_SR_ALL%" >nul 2>&1
if exist "%TMP_SR_PERF%" del /q "%TMP_SR_PERF%" >nul 2>&1
if exist "%ROOT_DIR%" popd >nul 2>&1

:summary
set /a "TOTAL_PERF_COUNT=%DM_PERF_COUNT%+%SR_PERF_COUNT%"
echo.
echo [all-tests] Summary
echo [all-tests]   DM all/perf/non-perf : %DM_ALL_COUNT% / %DM_PERF_COUNT% / %DM_NON_PERF_COUNT%
echo [all-tests]   SR all/perf/non-perf : %SR_ALL_COUNT% / %SR_PERF_COUNT% / %SR_NON_PERF_COUNT%
echo [all-tests]   DM collect/run exit  : %DM_COLLECT_EXIT% / %DM_RUN_EXIT%
echo [all-tests]   SR collect/run exit  : %SR_COLLECT_EXIT% / %SR_RUN_EXIT%
echo [all-tests]   Total perf selected  : %TOTAL_PERF_COUNT%

if "%ONLY_COLLECT%"=="1" (
  echo [all-tests]   Perf in executed suite: NO ^(--collect-only mode^)
) else (
  if %TOTAL_PERF_COUNT% GTR 0 (
    echo [all-tests]   Perf in executed suite: YES
    echo [all-tests]   DM perf metrics file  : "%DM_DIR%\logs\perf\perf_metrics.jsonl"
  ) else (
    echo [all-tests]   Perf in executed suite: NO ^(no perf tests matched current args^)
  )
)

if "%FINAL_EXIT%"=="0" (
  echo [all-tests] RESULT: SUCCESS
) else (
  echo [all-tests] RESULT: FAILED
)

pause
exit /b %FINAL_EXIT%
