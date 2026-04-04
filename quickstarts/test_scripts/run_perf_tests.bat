@echo off
setlocal EnableDelayedExpansion

set "ROOT_DIR=%~dp0\..\.."
set "DM_DIR=%ROOT_DIR%\py3_dialog_manager"
set "DM_PYTHON_EXE=%DM_DIR%\venv\Scripts\python.exe"
set "SR_DIR=%ROOT_DIR%\py3_script_runner"
set "SR_PYTHON_EXE=%SR_DIR%\venv\Scripts\python.exe"
set "DM_TEST_TARGET=py3_dialog_manager\tests"
set "SR_TEST_TARGET=py3_script_runner\tests"
set "BASELINE_FILE=py3_dialog_manager\logs\perf\baseline_run_id.txt"
set "SET_BASELINE_SCRIPT=py3_dialog_manager\scripts\perf_set_baseline.py"
set "COMPARE_SCRIPT=py3_dialog_manager\scripts\perf_compare_latest.py"

set "TEST_SUITE=all"
set "PERF_MODE=auto"
set "FINAL_EXIT=0"
set "RUN_DM=0"
set "RUN_SR=0"
set "RUN_PERF_ANALYSIS=0"
set "ONLY_COLLECT=0"

set "DM_COLLECT_EXIT=0"
set "DM_PERF_COLLECT_EXIT=0"
set "SR_COLLECT_EXIT=0"
set "SR_PERF_COLLECT_EXIT=0"
set "DM_RUN_EXIT=n/a"
set "SR_RUN_EXIT=n/a"
set "PERF_POST_EXIT=n/a"

set "DM_ALL_COUNT=0"
set "DM_PERF_COUNT=0"
set "DM_NON_PERF_COUNT=0"
set "SR_ALL_COUNT=0"
set "SR_PERF_COUNT=0"
set "SR_NON_PERF_COUNT=0"
set "TOTAL_PERF_COUNT=0"

echo %* | findstr /I /C:"--collect-only" >nul
if "%ERRORLEVEL%"=="0" set "ONLY_COLLECT=1"

if not exist "%DM_PYTHON_EXE%" (
  echo [tests] ERROR: %DM_PYTHON_EXE% niet gevonden. Draai eerst de py3_dialog_manager venv/install setup.
  pause
  exit /b 1
)

:choose_suite
set "TEST_SUITE="
set /p "TEST_SUITE=Welke tests wil je draaien? [all/dm/sr/perf] [all]: "
if "!TEST_SUITE!"=="" set "TEST_SUITE=all"
if /I "!TEST_SUITE!"=="a" set "TEST_SUITE=all"
if /I "!TEST_SUITE!"=="d" set "TEST_SUITE=dm"
if /I "!TEST_SUITE!"=="s" set "TEST_SUITE=sr"
if /I "!TEST_SUITE!"=="p" set "TEST_SUITE=perf"
if /I "!TEST_SUITE!"=="all" (
  set "RUN_DM=1"
  set "RUN_SR=1"
  goto suite_ok
)
if /I "!TEST_SUITE!"=="dm" (
  set "RUN_DM=1"
  goto suite_ok
)
if /I "!TEST_SUITE!"=="sr" (
  set "RUN_SR=1"
  goto suite_ok
)
if /I "!TEST_SUITE!"=="perf" (
  set "RUN_DM=1"
  set "RUN_PERF_ANALYSIS=1"
  goto choose_perf_mode
)
echo [tests] Ongeldige keuze. Kies all, dm, sr of perf.
goto choose_suite

:choose_perf_mode
set "PERF_MODE="
set /p "PERF_MODE=Hoe wil je perf draaien? [auto/tests/baseline/compare] [auto]: "
if "!PERF_MODE!"=="" set "PERF_MODE=auto"
if /I "!PERF_MODE!"=="a" set "PERF_MODE=auto"
if /I "!PERF_MODE!"=="t" set "PERF_MODE=tests"
if /I "!PERF_MODE!"=="b" set "PERF_MODE=baseline"
if /I "!PERF_MODE!"=="c" set "PERF_MODE=compare"
if /I "!PERF_MODE!"=="auto" goto suite_ok
if /I "!PERF_MODE!"=="tests" goto suite_ok
if /I "!PERF_MODE!"=="baseline" goto suite_ok
if /I "!PERF_MODE!"=="compare" goto suite_ok
echo [perf-tests] Ongeldige keuze. Kies auto, tests, baseline of compare.
goto choose_perf_mode

:suite_ok
pushd "%ROOT_DIR%" >nul
if errorlevel 1 (
  echo [tests] ERROR: kon repo root niet openen: %ROOT_DIR%
  pause
  exit /b 1
)

if "%RUN_DM%"=="1" (
  call :collect_dm %*
  if not "!DM_COLLECT_EXIT!"=="0" set "FINAL_EXIT=1"
  if not "!DM_PERF_COLLECT_EXIT!"=="0" set "FINAL_EXIT=1"
)

if "%RUN_SR%"=="1" (
  call :collect_sr %*
  if not "!SR_COLLECT_EXIT!"=="0" set "FINAL_EXIT=1"
  if not "!SR_PERF_COLLECT_EXIT!"=="0" set "FINAL_EXIT=1"
)

if "%ONLY_COLLECT%"=="1" goto summary

if "%RUN_PERF_ANALYSIS%"=="1" (
  call :run_dm_perf %*
  goto summary
)

if "%RUN_DM%"=="1" (
  echo.
  echo [tests] Run dialog manager tests zonder markerfilter...
  echo [tests] Command: "%DM_PYTHON_EXE%" -m pytest %DM_TEST_TARGET% -ra --durations=15 %*
  "%DM_PYTHON_EXE%" -m pytest %DM_TEST_TARGET% -ra --durations=15 %*
  set "DM_RUN_EXIT=!ERRORLEVEL!"
  if not "!DM_RUN_EXIT!"=="0" set "FINAL_EXIT=1"
)

if "%RUN_SR%"=="1" (
  echo.
  if not exist "%SR_PYTHON_EXE%" (
    echo [tests] ERROR: %SR_PYTHON_EXE% niet gevonden. Draai eerst de py3_script_runner venv/install setup.
    set "SR_RUN_EXIT=1"
    set "FINAL_EXIT=1"
  ) else (
    echo [tests] Run script runner tests zonder markerfilter...
    echo [tests] Command: "%SR_PYTHON_EXE%" -m pytest %SR_TEST_TARGET% -ra --durations=15 %*
    "%SR_PYTHON_EXE%" -m pytest %SR_TEST_TARGET% -ra --durations=15 %*
    set "SR_RUN_EXIT=!ERRORLEVEL!"
    if not "!SR_RUN_EXIT!"=="0" set "FINAL_EXIT=1"
  )
)

goto summary

:collect_dm
echo.
echo [tests] Discover dialog manager tests ^(all + perf^)
set "TMP_DM_ALL=%TEMP%\dm_collect_all_%RANDOM%_%RANDOM%.txt"
set "TMP_DM_PERF=%TEMP%\dm_collect_perf_%RANDOM%_%RANDOM%.txt"
"%DM_PYTHON_EXE%" -m pytest --collect-only -q %DM_TEST_TARGET% %* > "%TMP_DM_ALL%" 2>&1
set "DM_COLLECT_EXIT=!ERRORLEVEL!"
for /f %%C in ('find /c "::" ^< "%TMP_DM_ALL%"') do set "DM_ALL_COUNT=%%C"
"%DM_PYTHON_EXE%" -m pytest --collect-only -q %DM_TEST_TARGET% -m perf %* > "%TMP_DM_PERF%" 2>&1
set "DM_PERF_COLLECT_EXIT=!ERRORLEVEL!"
if "!DM_PERF_COLLECT_EXIT!"=="5" set "DM_PERF_COLLECT_EXIT=0"
for /f %%C in ('find /c "::" ^< "%TMP_DM_PERF%"') do set "DM_PERF_COUNT=%%C"
set /a "DM_NON_PERF_COUNT=!DM_ALL_COUNT!-!DM_PERF_COUNT!"
if !DM_NON_PERF_COUNT! LSS 0 set "DM_NON_PERF_COUNT=0"
echo [tests]   selected all tests : !DM_ALL_COUNT!
echo [tests]   selected perf tests: !DM_PERF_COUNT!
echo [tests]   selected non-perf  : !DM_NON_PERF_COUNT!
if not "!DM_COLLECT_EXIT!"=="0" (
  echo [tests]   ERROR: DM collect failed ^(exit !DM_COLLECT_EXIT!^)
)
if not "!DM_PERF_COLLECT_EXIT!"=="0" (
  echo [tests]   ERROR: DM perf collect failed ^(exit !DM_PERF_COLLECT_EXIT!^)
)
if exist "%TMP_DM_ALL%" del /q "%TMP_DM_ALL%" >nul 2>&1
if exist "%TMP_DM_PERF%" del /q "%TMP_DM_PERF%" >nul 2>&1
exit /b 0

:collect_sr
echo.
echo [tests] Discover script runner tests ^(all + perf^)
if not exist "%SR_PYTHON_EXE%" (
  echo [tests]   ERROR: %SR_PYTHON_EXE% niet gevonden.
  set "SR_COLLECT_EXIT=1"
  set "SR_PERF_COLLECT_EXIT=1"
  exit /b 0
)
set "TMP_SR_ALL=%TEMP%\sr_collect_all_%RANDOM%_%RANDOM%.txt"
set "TMP_SR_PERF=%TEMP%\sr_collect_perf_%RANDOM%_%RANDOM%.txt"
"%SR_PYTHON_EXE%" -m pytest --collect-only -q %SR_TEST_TARGET% %* > "%TMP_SR_ALL%" 2>&1
set "SR_COLLECT_EXIT=!ERRORLEVEL!"
for /f %%C in ('find /c "::" ^< "%TMP_SR_ALL%"') do set "SR_ALL_COUNT=%%C"
"%SR_PYTHON_EXE%" -m pytest --collect-only -q %SR_TEST_TARGET% -m perf %* > "%TMP_SR_PERF%" 2>&1
set "SR_PERF_COLLECT_EXIT=!ERRORLEVEL!"
if "!SR_PERF_COLLECT_EXIT!"=="5" set "SR_PERF_COLLECT_EXIT=0"
for /f %%C in ('find /c "::" ^< "%TMP_SR_PERF%"') do set "SR_PERF_COUNT=%%C"
set /a "SR_NON_PERF_COUNT=!SR_ALL_COUNT!-!SR_PERF_COUNT!"
if !SR_NON_PERF_COUNT! LSS 0 set "SR_NON_PERF_COUNT=0"
echo [tests]   selected all tests : !SR_ALL_COUNT!
echo [tests]   selected perf tests: !SR_PERF_COUNT!
echo [tests]   selected non-perf  : !SR_NON_PERF_COUNT!
if not "!SR_COLLECT_EXIT!"=="0" (
  echo [tests]   ERROR: SR collect failed ^(exit !SR_COLLECT_EXIT!^)
)
if not "!SR_PERF_COLLECT_EXIT!"=="0" (
  echo [tests]   ERROR: SR perf collect failed ^(exit !SR_PERF_COLLECT_EXIT!^)
)
if exist "%TMP_SR_ALL%" del /q "%TMP_SR_ALL%" >nul 2>&1
if exist "%TMP_SR_PERF%" del /q "%TMP_SR_PERF%" >nul 2>&1
exit /b 0

:run_dm_perf
echo.
echo [perf-tests] Run dialog manager perf tests...
echo [perf-tests] Command: "%DM_PYTHON_EXE%" -m pytest %DM_TEST_TARGET% -m perf -ra --durations=15 %*
"%DM_PYTHON_EXE%" -m pytest %DM_TEST_TARGET% -m perf -ra --durations=15 %*
set "DM_RUN_EXIT=!ERRORLEVEL!"
if not "!DM_RUN_EXIT!"=="0" (
  echo [perf-tests] FAILED: perf tests stopten met exit code !DM_RUN_EXIT!.
  set "FINAL_EXIT=1"
  exit /b 0
)

if /I "!PERF_MODE!"=="tests" (
  echo [perf-tests] Alleen perf-tests gedraaid; baseline/compare overgeslagen.
  exit /b 0
)

if /I "!PERF_MODE!"=="baseline" (
  echo [perf-tests] Baseline expliciet zetten op de laatste run.
  "%DM_PYTHON_EXE%" %SET_BASELINE_SCRIPT%
) else if /I "!PERF_MODE!"=="compare" (
  echo [perf-tests] Laatste run expliciet vergelijken met baseline.
  "%DM_PYTHON_EXE%" %COMPARE_SCRIPT%
) else if exist "%BASELINE_FILE%" (
  echo [perf-tests] Baseline gevonden, vergelijk laatste run met baseline.
  "%DM_PYTHON_EXE%" %COMPARE_SCRIPT%
) else (
  echo [perf-tests] Geen baseline gevonden, stel de huidige run in als baseline.
  "%DM_PYTHON_EXE%" %SET_BASELINE_SCRIPT%
)
set "PERF_POST_EXIT=!ERRORLEVEL!"
if not "!PERF_POST_EXIT!"=="0" (
  echo [perf-tests] FAILED: baseline/compare stap stopte met exit code !PERF_POST_EXIT!.
  set "FINAL_EXIT=1"
)
exit /b 0

:summary
if exist "%ROOT_DIR%" popd >nul 2>&1
set /a "TOTAL_PERF_COUNT=!DM_PERF_COUNT!+!SR_PERF_COUNT!"
echo.
echo [tests] Summary
echo [tests]   Suite                  : !TEST_SUITE!
echo [tests]   DM all/perf/non-perf   : !DM_ALL_COUNT! / !DM_PERF_COUNT! / !DM_NON_PERF_COUNT!
echo [tests]   SR all/perf/non-perf   : !SR_ALL_COUNT! / !SR_PERF_COUNT! / !SR_NON_PERF_COUNT!
echo [tests]   DM collect/run exit    : !DM_COLLECT_EXIT! / !DM_RUN_EXIT!
echo [tests]   SR collect/run exit    : !SR_COLLECT_EXIT! / !SR_RUN_EXIT!
echo [tests]   Perf post-step exit    : !PERF_POST_EXIT!
echo [tests]   Total perf selected    : !TOTAL_PERF_COUNT!

if "%ONLY_COLLECT%"=="1" (
  echo [tests]   Perf in executed suite: NO ^(--collect-only mode^)
) else if /I "!TEST_SUITE!"=="sr" (
  if !SR_PERF_COUNT! GTR 0 (
    echo [tests]   Perf in executed suite: YES
  ) else (
    echo [tests]   Perf in executed suite: NO
  )
) else if !TOTAL_PERF_COUNT! GTR 0 (
  echo [tests]   Perf in executed suite: YES
  echo [tests]   DM perf metrics file  : "%DM_DIR%\logs\perf\perf_metrics.jsonl"
) else (
  echo [tests]   Perf in executed suite: NO
)

if "!FINAL_EXIT!"=="0" (
  echo [tests] RESULT: SUCCESS
) else (
  echo [tests] RESULT: FAILED
)
pause
exit /b !FINAL_EXIT!
