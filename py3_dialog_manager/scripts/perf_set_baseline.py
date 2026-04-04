from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict, List


_PACKAGE_ROOT = Path(__file__).resolve().parents[1]


def _resolve_repo_path(path: str) -> Path:
    candidate = Path(path)
    if candidate.is_absolute():
        return candidate
    return (_PACKAGE_ROOT / candidate).resolve()


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


def _is_recheck_run(run_id: str) -> bool:
    return str(run_id or "").startswith("recheck_")


def main() -> int:
    parser = argparse.ArgumentParser(description="Set fixed perf baseline run_id for comparisons.")
    parser.add_argument(
        "--path",
        default="logs/perf/perf_metrics.jsonl",
        help="Path to perf metrics JSONL file (default: logs/perf/perf_metrics.jsonl).",
    )
    parser.add_argument(
        "--baseline-file",
        default="logs/perf/baseline_run_id.txt",
        help="Baseline run_id output file (default: logs/perf/baseline_run_id.txt).",
    )
    parser.add_argument(
        "--run-id",
        default="",
        help="Explicit run_id to set as baseline (default: latest run_id in metrics file).",
    )
    parser.add_argument(
        "--include-recheck",
        action="store_true",
        help="Allow recheck_* run ids as baseline candidates.",
    )
    args = parser.parse_args()

    metrics_path = _resolve_repo_path(args.path)
    entries = _load_entries(metrics_path)
    if not entries:
        print("[perf-baseline] No metrics found:", metrics_path)
        return 1

    run_ids = _run_ids_in_order(entries)
    if not run_ids:
        print("[perf-baseline] No run_id groups found in:", metrics_path)
        return 1

    chosen = str(args.run_id or "").strip()
    if not chosen:
        candidates = list(run_ids)
        if not bool(args.include_recheck):
            normal_candidates = [rid for rid in candidates if not _is_recheck_run(rid)]
            if normal_candidates:
                candidates = normal_candidates
        chosen = candidates[-1]
    if chosen not in run_ids:
        print("[perf-baseline] Requested run_id not present:", chosen)
        return 1

    baseline_path = _resolve_repo_path(args.baseline_file)
    try:
        baseline_path.parent.mkdir(parents=True, exist_ok=True)
        baseline_path.write_text(chosen + "\n", encoding="utf-8")
    except OSError as exc:
        print("[perf-baseline] Failed to write baseline file:", baseline_path, exc)
        return 1

    print("[perf-baseline] Baseline set:", chosen)
    print("[perf-baseline] File:", baseline_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
