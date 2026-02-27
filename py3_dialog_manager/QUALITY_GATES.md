# Quality Gates (Dialog Manager)

This file defines blocking gates for changes in `py3_dialog_manager`.

## Goals
- Catch regressions before merge.
- Keep UI behavior stable with explicit contract checks.
- Catch latency regressions with repeatable perf budgets.
- Standardize handover quality: self-critique and explicit risks.

## Gate Levels
1. `G1 - Backend Regression` (blocking)
2. `G2 - UI Contract` (blocking)
3. `G3 - Performance Smoke` (blocking for release/nightly, optional for every local save)

## Canonical Commands
- Default (G1 + G2):
  - `venv/Scripts/python.exe scripts/run_quality_gates.py`
- Full (G1 + G2 + G3):
  - `venv/Scripts/python.exe scripts/run_quality_gates.py --with-perf`
  - Executes all tests marked `@pytest.mark.perf`
  - Runs perf guard compare with auto recheck on WARN/FAIL

## Performance Budgets
`tests/test_perf_smoke.py` uses p95 latency budgets and supports env overrides:
- `DM_PERF_SEND_P95_MS` (default `120`)
- `DM_PERF_DM_EVENTS_P95_MS` (default `120`)
- `DM_PERF_TRANSCRIBE_P95_MS` (default `120`)
- `DM_PERF_INTERACTION_P95_MS` (default `220`)
- `DM_PERF_STOP_P95_MS` (default `220`)
- `DM_PERF_STANDUP_P95_MS` (default `220`)
- `DM_PERF_DANCE_HAPPY_P95_MS` (default `320`)
- `DM_PERF_PIPELINE_CMDREC_P95_MS` (default `8`)
- `DM_PERF_PIPELINE_EMPTY_P95_MS` (default `4`)
- `DM_PERF_PIPELINE_DIALOG_P95_MS` (default `12`)
- `DM_PERF_PIPELINE_CMD_STOP_P95_MS` (default `10`)
- `DM_PERF_PIPELINE_CMD_NON_STOP_P95_MS` (default `10`)
- `DM_PERF_PIPELINE_CMD_NO_EXECUTOR_P95_MS` (default `10`)
- `DM_PERF_WARMUP` (default `10`)
- `DM_PERF_ITERATIONS` (default `80`)
- `DM_PERF_MOCK_STT_MS` (default `4`)
- `DM_PERF_MOCK_LLM_MS` (default `6`)
- `DM_PERF_MOCK_CMDREC_MS` (default `1`)
- `DM_PERF_MOCK_EXECUTOR_MS` (default `1`)
- `DM_PERF_LOG_ENABLED` (default `1`, append metrics JSONL)
- `DM_PERF_METRICS_PATH` (default `logs/perf/perf_metrics.jsonl`)
- `DM_PERF_RUN_ID` (optional run grouping id)

Interaction flow coverage is included in `tests/test_perf_interaction_flow.py`:
- mocked STT latency via `/api/transcribe`
- mocked cmd_rec route + mocked reasoning latency via `/api/send`
- combined `transcribe -> send` p95 budget
- mocked command route latency for `STOP` and `STAND_UP`
- mocked multi-turn dance flow: `"doe een dans"` then `"happy"` executes `DANCE` with resolved behavior
- direct pipeline path latency:
  - empty input
  - dialog path
  - command STOP
  - command non-STOP
  - command without executor

Budget changes are allowed only with evidence in handover:
- before/after measurements
- reason for budget change
- risk and rollback statement

## Regression Tracking Best Practice
- Use fixed test parameters for PR checks: same warmup/iterations and no background load.
- Use p95 as primary gate metric, not average.
- Compare against a recent baseline and track relative delta (%) and absolute delta (ms).
- Treat small changes as noise band first (for example <= 5%), fail only beyond a stricter threshold.
- Keep PR perf checks light; run deeper/high-iteration runs nightly and store results as artifacts.

Compare current run against fixed baseline:
- `venv/Scripts/python.exe scripts/perf_compare_latest.py`
- Optional thresholds:
  - `venv/Scripts/python.exe scripts/perf_compare_latest.py --warn-pct 5 --warn-ms 3 --fail-pct 10 --fail-ms 8`

Auto recheck mode (recommended):
- `venv/Scripts/python.exe scripts/perf_compare_latest.py --rerun-on-warn --rerun-count 3 --rerun-warmup 30 --rerun-iterations 250`
- Decision uses median p95 of recheck runs versus fixed baseline.

Baseline setup (fixed 0-measurement):
- Set baseline to latest recorded run:
  - `venv/Scripts/python.exe scripts/perf_set_baseline.py`
- Set baseline to explicit run id:
  - `venv/Scripts/python.exe scripts/perf_set_baseline.py --run-id <run_id>`
- Baseline file used by gates:
  - `logs/perf/baseline_run_id.txt`

## Handover Requirements
Every substantial change should include:
1. What changed
2. Critical self-review
3. Known risks
4. What was not tested
5. Test evidence (exact commands + outcome)
6. Relevant smoke-test outcomes (key pass/fail and important p95/perf warnings)
7. Rollback approach

Use `HANDOVER_TEMPLATE.md` for consistency.
