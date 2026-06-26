#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

DM_PYTHON_EXE="$REPO_ROOT/py3_dialog_manager/venv/bin/python"
SR_PYTHON_EXE="$REPO_ROOT/py3_script_runner/venv/bin/python"
DM_TEST_TARGET="py3_dialog_manager/tests"
SR_TEST_TARGET="py3_script_runner/tests"
BASELINE_FILE="py3_dialog_manager/logs/perf/baseline_run_id.txt"
SET_BASELINE_SCRIPT="py3_dialog_manager/scripts/perf_set_baseline.py"
COMPARE_SCRIPT="py3_dialog_manager/scripts/perf_compare_latest.py"

if [ ! -f "$DM_PYTHON_EXE" ]; then
    echo "[tests] ERROR: $DM_PYTHON_EXE niet gevonden. Draai eerst install_repo.sh."
    exit 1
fi

# Detecteer --collect-only modus
ONLY_COLLECT=false
for arg in "$@"; do
    [ "$arg" = "--collect-only" ] && ONLY_COLLECT=true && break
done

# Testsuite kiezen
TEST_SUITE=""
while true; do
    read -r -p "Welke tests wil je draaien? [all/dm/sr/perf] [all] " raw
    TEST_SUITE="${raw:-all}"
    case "$(echo "$TEST_SUITE" | tr '[:upper:]' '[:lower:]')" in
        a|all)  TEST_SUITE="all";  break ;;
        d|dm)   TEST_SUITE="dm";   break ;;
        s|sr)   TEST_SUITE="sr";   break ;;
        p|perf) TEST_SUITE="perf"; break ;;
        *) echo "[tests] Ongeldige keuze. Kies all, dm, sr of perf." ;;
    esac
done

PERF_MODE="auto"
if [ "$TEST_SUITE" = "perf" ]; then
    while true; do
        read -r -p "Hoe wil je perf draaien? [auto/tests/baseline/compare] [auto] " raw
        PERF_MODE="${raw:-auto}"
        case "$(echo "$PERF_MODE" | tr '[:upper:]' '[:lower:]')" in
            a|auto)    PERF_MODE="auto";    break ;;
            t|tests)   PERF_MODE="tests";   break ;;
            b|baseline)PERF_MODE="baseline";break ;;
            c|compare) PERF_MODE="compare"; break ;;
            *) echo "[perf-tests] Ongeldige keuze. Kies auto, tests, baseline of compare." ;;
        esac
    done
fi

# Ga naar repo root
cd "$REPO_ROOT"

RUN_DM=false
RUN_SR=false
RUN_PERF_ANALYSIS=false
FINAL_EXIT=0

case "$TEST_SUITE" in
    all)  RUN_DM=true; RUN_SR=true ;;
    dm)   RUN_DM=true ;;
    sr)   RUN_SR=true ;;
    perf) RUN_DM=true; RUN_PERF_ANALYSIS=true ;;
esac

count_test_items() {
    grep -c "::" "$1" 2>/dev/null || echo 0
}

# Collect stap DM
DM_ALL_COUNT=0; DM_PERF_COUNT=0; DM_NON_PERF_COUNT=0
DM_COLLECT_EXIT=0; DM_PERF_COLLECT_EXIT=0; DM_RUN_EXIT="n/a"
if [ "$RUN_DM" = "true" ]; then
    echo ""
    echo "[tests] Discover dialog manager tests (all + perf)"
    TMP_DM_ALL="$(mktemp)"
    TMP_DM_PERF="$(mktemp)"
    "$DM_PYTHON_EXE" -m pytest --collect-only -q "$DM_TEST_TARGET" "$@" > "$TMP_DM_ALL" 2>&1 || DM_COLLECT_EXIT=$?
    DM_ALL_COUNT="$(count_test_items "$TMP_DM_ALL")"
    "$DM_PYTHON_EXE" -m pytest --collect-only -q "$DM_TEST_TARGET" -m perf "$@" > "$TMP_DM_PERF" 2>&1 || DM_PERF_COLLECT_EXIT=$?
    [ "$DM_PERF_COLLECT_EXIT" = "5" ] && DM_PERF_COLLECT_EXIT=0
    DM_PERF_COUNT="$(count_test_items "$TMP_DM_PERF")"
    DM_NON_PERF_COUNT=$(( DM_ALL_COUNT - DM_PERF_COUNT ))
    [ "$DM_NON_PERF_COUNT" -lt 0 ] && DM_NON_PERF_COUNT=0
    echo "[tests]   selected all tests : $DM_ALL_COUNT"
    echo "[tests]   selected perf tests: $DM_PERF_COUNT"
    echo "[tests]   selected non-perf  : $DM_NON_PERF_COUNT"
    [ "$DM_COLLECT_EXIT" != "0" ] && echo "[tests]   ERROR: DM collect failed (exit $DM_COLLECT_EXIT)" && FINAL_EXIT=1
    [ "$DM_PERF_COLLECT_EXIT" != "0" ] && echo "[tests]   ERROR: DM perf collect failed (exit $DM_PERF_COLLECT_EXIT)" && FINAL_EXIT=1
    rm -f "$TMP_DM_ALL" "$TMP_DM_PERF"
fi

# Collect stap SR
SR_ALL_COUNT=0; SR_PERF_COUNT=0; SR_NON_PERF_COUNT=0
SR_COLLECT_EXIT=0; SR_PERF_COLLECT_EXIT=0; SR_RUN_EXIT="n/a"
if [ "$RUN_SR" = "true" ]; then
    echo ""
    echo "[tests] Discover script runner tests (all + perf)"
    if [ ! -f "$SR_PYTHON_EXE" ]; then
        echo "[tests]   ERROR: $SR_PYTHON_EXE niet gevonden."
        SR_COLLECT_EXIT=1; SR_PERF_COLLECT_EXIT=1; FINAL_EXIT=1
    else
        TMP_SR_ALL="$(mktemp)"
        TMP_SR_PERF="$(mktemp)"
        "$SR_PYTHON_EXE" -m pytest --collect-only -q "$SR_TEST_TARGET" "$@" > "$TMP_SR_ALL" 2>&1 || SR_COLLECT_EXIT=$?
        SR_ALL_COUNT="$(count_test_items "$TMP_SR_ALL")"
        "$SR_PYTHON_EXE" -m pytest --collect-only -q "$SR_TEST_TARGET" -m perf "$@" > "$TMP_SR_PERF" 2>&1 || SR_PERF_COLLECT_EXIT=$?
        [ "$SR_PERF_COLLECT_EXIT" = "5" ] && SR_PERF_COLLECT_EXIT=0
        SR_PERF_COUNT="$(count_test_items "$TMP_SR_PERF")"
        SR_NON_PERF_COUNT=$(( SR_ALL_COUNT - SR_PERF_COUNT ))
        [ "$SR_NON_PERF_COUNT" -lt 0 ] && SR_NON_PERF_COUNT=0
        echo "[tests]   selected all tests : $SR_ALL_COUNT"
        echo "[tests]   selected perf tests: $SR_PERF_COUNT"
        echo "[tests]   selected non-perf  : $SR_NON_PERF_COUNT"
        [ "$SR_COLLECT_EXIT" != "0" ] && echo "[tests]   ERROR: SR collect failed (exit $SR_COLLECT_EXIT)" && FINAL_EXIT=1
        [ "$SR_PERF_COLLECT_EXIT" != "0" ] && echo "[tests]   ERROR: SR perf collect failed (exit $SR_PERF_COLLECT_EXIT)" && FINAL_EXIT=1
        rm -f "$TMP_SR_ALL" "$TMP_SR_PERF"
    fi
fi

TOTAL_PERF_COUNT=$(( DM_PERF_COUNT + SR_PERF_COUNT ))
PERF_POST_EXIT="n/a"

if [ "$ONLY_COLLECT" = "true" ]; then
    :  # Alleen collect, geen run
elif [ "$RUN_PERF_ANALYSIS" = "true" ]; then
    echo ""
    echo "[perf-tests] Run dialog manager perf tests..."
    "$DM_PYTHON_EXE" -m pytest "$DM_TEST_TARGET" -m perf -ra --durations=15 "$@" || DM_RUN_EXIT=$?
    DM_RUN_EXIT="${DM_RUN_EXIT:-0}"
    if [ "$DM_RUN_EXIT" != "0" ] && [ "$DM_RUN_EXIT" != "n/a" ]; then
        echo "[perf-tests] FAILED: perf tests stopten met exit code $DM_RUN_EXIT."
        FINAL_EXIT=1
    else
        lower_mode="$(echo "$PERF_MODE" | tr '[:upper:]' '[:lower:]')"
        if [ "$lower_mode" = "tests" ]; then
            echo "[perf-tests] Alleen perf-tests gedraaid; baseline/compare overgeslagen."
        elif [ "$lower_mode" = "baseline" ]; then
            echo "[perf-tests] Baseline expliciet zetten op de laatste run."
            "$DM_PYTHON_EXE" "$SET_BASELINE_SCRIPT" || PERF_POST_EXIT=$?
        elif [ "$lower_mode" = "compare" ]; then
            echo "[perf-tests] Laatste run expliciet vergelijken met baseline."
            "$DM_PYTHON_EXE" "$COMPARE_SCRIPT" || PERF_POST_EXIT=$?
        elif [ -f "$BASELINE_FILE" ]; then
            echo "[perf-tests] Baseline gevonden, vergelijk laatste run met baseline."
            "$DM_PYTHON_EXE" "$COMPARE_SCRIPT" || PERF_POST_EXIT=$?
        else
            echo "[perf-tests] Geen baseline gevonden, stel de huidige run in als baseline."
            "$DM_PYTHON_EXE" "$SET_BASELINE_SCRIPT" || PERF_POST_EXIT=$?
        fi
        PERF_POST_EXIT="${PERF_POST_EXIT:-0}"
        if [ "$PERF_POST_EXIT" != "0" ] && [ "$PERF_POST_EXIT" != "n/a" ]; then
            echo "[perf-tests] FAILED: baseline/compare stap stopte met exit code $PERF_POST_EXIT."
            FINAL_EXIT=1
        fi
    fi
else
    if [ "$RUN_DM" = "true" ]; then
        echo ""
        echo "[tests] Run dialog manager tests zonder markerfilter..."
        "$DM_PYTHON_EXE" -m pytest "$DM_TEST_TARGET" -ra --durations=15 "$@" || DM_RUN_EXIT=$?
        DM_RUN_EXIT="${DM_RUN_EXIT:-0}"
        [ "$DM_RUN_EXIT" != "0" ] && FINAL_EXIT=1
    fi
    if [ "$RUN_SR" = "true" ]; then
        echo ""
        if [ ! -f "$SR_PYTHON_EXE" ]; then
            echo "[tests] ERROR: $SR_PYTHON_EXE niet gevonden. Draai eerst install_repo.sh."
            SR_RUN_EXIT=1; FINAL_EXIT=1
        else
            echo "[tests] Run script runner tests zonder markerfilter..."
            "$SR_PYTHON_EXE" -m pytest "$SR_TEST_TARGET" -ra --durations=15 "$@" || SR_RUN_EXIT=$?
            SR_RUN_EXIT="${SR_RUN_EXIT:-0}"
            [ "$SR_RUN_EXIT" != "0" ] && FINAL_EXIT=1
        fi
    fi
fi

echo ""
echo "[tests] Summary"
echo "[tests]   Suite                  : $TEST_SUITE"
echo "[tests]   DM all/perf/non-perf   : $DM_ALL_COUNT / $DM_PERF_COUNT / $DM_NON_PERF_COUNT"
echo "[tests]   SR all/perf/non-perf   : $SR_ALL_COUNT / $SR_PERF_COUNT / $SR_NON_PERF_COUNT"
echo "[tests]   DM collect/run exit    : $DM_COLLECT_EXIT / $DM_RUN_EXIT"
echo "[tests]   SR collect/run exit    : $SR_COLLECT_EXIT / $SR_RUN_EXIT"
echo "[tests]   Perf post-step exit    : $PERF_POST_EXIT"
echo "[tests]   Total perf selected    : $TOTAL_PERF_COUNT"

if [ "$ONLY_COLLECT" = "true" ]; then
    echo "[tests]   Perf in executed suite: NO (--collect-only mode)"
elif [ "$TEST_SUITE" = "sr" ]; then
    [ "$SR_PERF_COUNT" -gt 0 ] \
        && echo "[tests]   Perf in executed suite: YES" \
        || echo "[tests]   Perf in executed suite: NO"
elif [ "$TOTAL_PERF_COUNT" -gt 0 ]; then
    echo "[tests]   Perf in executed suite: YES"
    echo "[tests]   DM perf metrics file  : \"$REPO_ROOT/py3_dialog_manager/logs/perf/perf_metrics.jsonl\""
else
    echo "[tests]   Perf in executed suite: NO"
fi

if [ "$FINAL_EXIT" = "0" ]; then
    echo "[tests] RESULT: SUCCESS"
else
    echo "[tests] RESULT: FAILED"
fi

exit "$FINAL_EXIT"
