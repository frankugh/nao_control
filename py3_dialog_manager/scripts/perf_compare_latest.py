from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Any, Dict, List, Optional, Tuple


_METRIC_TEST_SELECTORS = {
    "api_send_emit_none": "tests/test_perf_smoke.py::test_send_api_latency_budget",
    "api_dm_events_limit_400": "tests/test_perf_smoke.py::test_dm_events_api_latency_budget",
    "api_transcribe_mock_stt": "tests/test_perf_interaction_flow.py::test_transcribe_api_latency_budget_with_mock_stt",
    "interaction_transcribe_send_cmdrec_reasoning": "tests/test_perf_interaction_flow.py::test_interaction_flow_latency_budget_with_cmdrec_and_reasoning",
    "command_stop_flow": "tests/test_perf_interaction_flow.py::test_stop_command_flow_latency_budget",
    "command_stand_up_flow": "tests/test_perf_interaction_flow.py::test_stand_up_command_flow_latency_budget",
    "dance_followup_happy_flow": "tests/test_perf_interaction_flow.py::test_dance_followup_happy_flow_latency_budget",
    "pipeline_run_once_empty_input": "tests/test_perf_pipeline_core.py::test_pipeline_run_once_empty_input_latency_budget",
    "pipeline_run_once_dialog_path": "tests/test_perf_pipeline_core.py::test_pipeline_run_once_dialog_path_latency_budget",
    "pipeline_run_once_command_stop": "tests/test_perf_pipeline_core.py::test_pipeline_run_once_command_stop_latency_budget",
    "pipeline_run_once_command_non_stop": "tests/test_perf_pipeline_core.py::test_pipeline_run_once_command_non_stop_latency_budget",
    "pipeline_run_once_command_no_executor": "tests/test_perf_pipeline_core.py::test_pipeline_run_once_command_without_executor_latency_budget",
}


def _load_entries(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    out: List[Dict[str, Any]] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            out.append(item)
    return out


def _run_ids_in_order(entries: List[Dict[str, Any]]) -> List[str]:
    out: List[str] = []
    seen = set()
    for item in entries:
        run_id = str(item.get("run_id") or "").strip()
        if not run_id or run_id in seen:
            continue
        seen.add(run_id)
        out.append(run_id)
    return out


def _metrics_for_run(entries: List[Dict[str, Any]], run_id: str) -> Dict[str, Dict[str, Any]]:
    result: Dict[str, Dict[str, Any]] = {}
    for item in entries:
        if str(item.get("run_id") or "") != run_id:
            continue
        metric = str(item.get("metric") or "").strip()
        if not metric:
            continue
        result[metric] = item
    return result


def _is_recheck_run(run_id: str) -> bool:
    return str(run_id or "").startswith("recheck_")


def _fmt_ms(value: float) -> str:
    return f"{value:.2f}ms"


def _fmt_pct(value: float) -> str:
    return f"{value:+.2f}%"


def _classify_status(
    *,
    delta_pct: float,
    delta_ms: float,
    warn_pct: float,
    warn_ms: float,
    fail_pct: float,
    fail_ms: float,
) -> str:
    if delta_pct >= fail_pct and delta_ms >= fail_ms:
        return "FAIL"
    if delta_pct >= warn_pct and delta_ms >= warn_ms:
        return "WARN"
    return "OK"


def _delta(base_p95: float, curr_p95: float) -> Tuple[float, float]:
    if base_p95 <= 0.0:
        return 0.0, curr_p95 - base_p95
    delta_pct = ((curr_p95 - base_p95) / base_p95) * 100.0
    return delta_pct, (curr_p95 - base_p95)


def _read_baseline_file(path: Path) -> Optional[str]:
    if not path.exists():
        return None
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return raw or None


def _find_metric_value(entries: List[Dict[str, Any]], *, run_id: str, metric: str, key: str) -> Optional[float]:
    for item in entries:
        if str(item.get("run_id") or "") != run_id:
            continue
        if str(item.get("metric") or "") != metric:
            continue
        try:
            return float(item.get(key) or 0.0)
        except Exception:
            return None
    return None


def _safe_run_id(metric: str, idx: int) -> str:
    base = "".join(ch if ch.isalnum() or ch in ("_", "-") else "_" for ch in metric)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return f"recheck_{base}_{idx}_{stamp}"


def _rerun_metric_rechecks(
    *,
    metric: str,
    selector: str,
    rerun_count: int,
    rerun_warmup: int,
    rerun_iterations: int,
    python_exe: str,
    workdir: Path,
    metrics_path: Path,
) -> Optional[List[float]]:
    values: List[float] = []
    for idx in range(1, max(1, int(rerun_count)) + 1):
        run_id = _safe_run_id(metric, idx)
        env = os.environ.copy()
        env["DM_PERF_RUN_ID"] = run_id
        env["DM_PERF_WARMUP"] = str(int(rerun_warmup))
        env["DM_PERF_ITERATIONS"] = str(int(rerun_iterations))
        env["DM_PERF_LOG_ENABLED"] = "1"
        cmd = [python_exe, "-m", "pytest", "-q", selector, "-m", "perf"]
        print(
            "[perf-compare] recheck:"
            f" metric={metric} run={idx}/{rerun_count} warmup={rerun_warmup} iterations={rerun_iterations}"
        )
        code = subprocess.call(cmd, cwd=str(workdir), env=env)
        if code != 0:
            print("[perf-compare] recheck failed:", metric, f"(exit={code})")
            return None
        entries = _load_entries(metrics_path)
        p95 = _find_metric_value(entries, run_id=run_id, metric=metric, key="p95_ms")
        if p95 is None:
            print("[perf-compare] missing recheck metric row:", metric, "run_id=", run_id)
            return None
        values.append(float(p95))
    return values


def main() -> int:
    parser = argparse.ArgumentParser(description="Compare current perf run against a fixed baseline.")
    parser.add_argument(
        "--path",
        default="logs/perf/perf_metrics.jsonl",
        help="Path to perf metrics JSONL file (default: logs/perf/perf_metrics.jsonl).",
    )
    parser.add_argument(
        "--baseline-file",
        default="logs/perf/baseline_run_id.txt",
        help="Path containing baseline run_id (default: logs/perf/baseline_run_id.txt).",
    )
    parser.add_argument(
        "--baseline-run-id",
        default="",
        help="Explicit baseline run_id (overrides baseline-file).",
    )
    parser.add_argument(
        "--current-run-id",
        default="",
        help="Explicit current run_id to compare (default: latest run_id in metrics).",
    )
    parser.add_argument(
        "--include-recheck-current",
        action="store_true",
        help="Allow recheck_* runs as automatic current run candidate.",
    )
    parser.add_argument(
        "--warn-pct",
        type=float,
        default=5.0,
        help="Warn threshold for regression percentage (default: 5).",
    )
    parser.add_argument(
        "--fail-pct",
        type=float,
        default=10.0,
        help="Fail threshold for regression percentage (default: 10).",
    )
    parser.add_argument(
        "--warn-ms",
        type=float,
        default=3.0,
        help="Warn threshold for absolute regression in milliseconds (default: 3).",
    )
    parser.add_argument(
        "--fail-ms",
        type=float,
        default=8.0,
        help="Fail threshold for absolute regression in milliseconds (default: 8).",
    )
    parser.add_argument(
        "--allow-missing-baseline",
        action="store_true",
        help="Return success when baseline is not configured yet.",
    )
    parser.add_argument(
        "--rerun-on-warn",
        action="store_true",
        help="Re-run warned/failed metrics and decide using median p95 of rechecks.",
    )
    parser.add_argument(
        "--rerun-count",
        type=int,
        default=3,
        help="Number of recheck runs per warned metric (default: 3).",
    )
    parser.add_argument(
        "--rerun-warmup",
        type=int,
        default=30,
        help="Warmup iterations for each recheck run (default: 30).",
    )
    parser.add_argument(
        "--rerun-iterations",
        type=int,
        default=250,
        help="Measured iterations for each recheck run (default: 250).",
    )
    parser.add_argument(
        "--python-exe",
        default=sys.executable,
        help="Python executable used for recheck pytest calls (default: current interpreter).",
    )
    parser.add_argument(
        "--workdir",
        default=".",
        help="Working directory for recheck pytest calls (default: current directory).",
    )
    args = parser.parse_args()

    path = Path(args.path)
    entries = _load_entries(path)
    if not entries:
        print("[perf-compare] No metrics found:", path)
        if args.allow_missing_baseline:
            print("[perf-compare] baseline missing allowed -> SKIP")
            return 0
        return 1

    run_ids = _run_ids_in_order(entries)
    if not run_ids:
        print("[perf-compare] No run_id groups found.")
        if args.allow_missing_baseline:
            print("[perf-compare] baseline missing allowed -> SKIP")
            return 0
        return 1

    baseline_run = str(args.baseline_run_id or "").strip()
    if not baseline_run:
        baseline_run = _read_baseline_file(Path(args.baseline_file)) or ""
    if not baseline_run:
        print("[perf-compare] No baseline configured.")
        print("[perf-compare] set via --baseline-run-id or baseline file:", args.baseline_file)
        if args.allow_missing_baseline:
            print("[perf-compare] baseline missing allowed -> SKIP")
            return 0
        return 1
    if baseline_run not in run_ids:
        print("[perf-compare] Baseline run_id not found in metrics:", baseline_run)
        return 1

    current_run = str(args.current_run_id or "").strip()
    if not current_run:
        baseline_index = run_ids.index(baseline_run)
        candidates = [rid for rid in run_ids[baseline_index + 1 :] if rid != baseline_run]
        if not bool(args.include_recheck_current):
            candidates = [rid for rid in candidates if not _is_recheck_run(rid)]
        if not candidates:
            print("[perf-compare] No current run newer than baseline.")
            if args.allow_missing_baseline:
                print("[perf-compare] baseline-only state allowed -> SKIP")
                return 0
            return 1
        current_run = candidates[-1]
    if current_run not in run_ids:
        print("[perf-compare] Current run_id not found in metrics:", current_run)
        return 1
    if current_run == baseline_run:
        print("[perf-compare] Baseline and current run are identical:", current_run)
        return 1

    baseline_metrics = _metrics_for_run(entries, baseline_run)
    current_metrics = _metrics_for_run(entries, current_run)
    shared = sorted(set(baseline_metrics.keys()) & set(current_metrics.keys()))

    if not shared:
        print("[perf-compare] No shared metrics between runs.")
        print("[perf-compare] baseline_run:", baseline_run)
        print("[perf-compare] current_run:", current_run)
        return 1

    print("[perf-compare] baseline_run:", baseline_run)
    print("[perf-compare] current_run:", current_run)
    print("[perf-compare] metrics:", len(shared))
    print(
        "[perf-compare] thresholds:"
        f" warn_pct={float(args.warn_pct):.2f}%"
        f" warn_ms={float(args.warn_ms):.2f}ms"
        f" fail_pct={float(args.fail_pct):.2f}%"
        f" fail_ms={float(args.fail_ms):.2f}ms"
    )

    detail: Dict[str, Dict[str, Any]] = {}
    for metric in shared:
        base_p95 = float(baseline_metrics[metric].get("p95_ms") or 0.0)
        curr_p95 = float(current_metrics[metric].get("p95_ms") or 0.0)
        delta_pct, delta_ms = _delta(base_p95, curr_p95)
        status = _classify_status(
            delta_pct=delta_pct,
            delta_ms=delta_ms,
            warn_pct=float(args.warn_pct),
            warn_ms=float(args.warn_ms),
            fail_pct=float(args.fail_pct),
            fail_ms=float(args.fail_ms),
        )
        detail[metric] = {
            "base_p95": base_p95,
            "curr_p95": curr_p95,
            "delta_pct": delta_pct,
            "delta_ms": delta_ms,
            "status": status,
            "final_status": status,
        }
        print(
            "[perf-compare] "
            f"{metric}: baseline={_fmt_ms(base_p95)} curr={_fmt_ms(curr_p95)} "
            f"delta={_fmt_ms(delta_ms)} ({_fmt_pct(delta_pct)}) status={status}"
        )

    if args.rerun_on_warn:
        flagged = [metric for metric in shared if detail[metric]["status"] in ("WARN", "FAIL")]
        if flagged:
            print(
                "[perf-compare] recheck enabled:"
                f" flagged_metrics={len(flagged)} reruns={int(args.rerun_count)}"
            )
        for metric in flagged:
            selector = _METRIC_TEST_SELECTORS.get(metric)
            if not selector:
                print("[perf-compare] no recheck selector for metric:", metric)
                detail[metric]["final_status"] = "FAIL"
                continue
            rerun_values = _rerun_metric_rechecks(
                metric=metric,
                selector=selector,
                rerun_count=int(args.rerun_count),
                rerun_warmup=int(args.rerun_warmup),
                rerun_iterations=int(args.rerun_iterations),
                python_exe=str(args.python_exe),
                workdir=Path(args.workdir),
                metrics_path=path,
            )
            if not rerun_values:
                detail[metric]["final_status"] = "FAIL"
                continue
            median_p95 = float(median(rerun_values))
            delta_pct_recheck, delta_ms_recheck = _delta(float(detail[metric]["base_p95"]), median_p95)
            status_recheck = _classify_status(
                delta_pct=delta_pct_recheck,
                delta_ms=delta_ms_recheck,
                warn_pct=float(args.warn_pct),
                warn_ms=float(args.warn_ms),
                fail_pct=float(args.fail_pct),
                fail_ms=float(args.fail_ms),
            )
            detail[metric]["recheck_p95_values"] = rerun_values
            detail[metric]["recheck_median_p95"] = median_p95
            detail[metric]["recheck_delta_pct"] = delta_pct_recheck
            detail[metric]["recheck_delta_ms"] = delta_ms_recheck
            detail[metric]["final_status"] = status_recheck
            print(
                "[perf-compare] recheck-result "
                f"{metric}: median_p95={_fmt_ms(median_p95)} "
                f"delta_vs_baseline={_fmt_ms(delta_ms_recheck)} ({_fmt_pct(delta_pct_recheck)}) "
                f"status={status_recheck}"
            )

    final_fail = any(detail[m]["final_status"] == "FAIL" for m in shared)
    final_warn = any(detail[m]["final_status"] == "WARN" for m in shared)
    print(
        "[perf-compare] final:"
        f" fail={sum(1 for m in shared if detail[m]['final_status'] == 'FAIL')}"
        f" warn={sum(1 for m in shared if detail[m]['final_status'] == 'WARN')}"
        f" ok={sum(1 for m in shared if detail[m]['final_status'] == 'OK')}"
    )

    if final_fail:
        return 1
    if final_warn:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
