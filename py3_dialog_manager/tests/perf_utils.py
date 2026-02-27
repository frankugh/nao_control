from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

_RUN_ID = os.getenv("DM_PERF_RUN_ID", "").strip() or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def perf_env(*, key: str, default: str) -> str:
    return os.getenv(key, default)


def perf_env_int(*, key: str, default: int) -> int:
    return int(perf_env(key=key, default=str(default)))


def perf_env_float(*, key: str, default: float) -> float:
    return float(perf_env(key=key, default=str(default)))


def percentile(values: List[float], p: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * (p / 100.0)
    lo = int(rank)
    hi = min(lo + 1, len(ordered) - 1)
    if lo == hi:
        return ordered[lo]
    weight = rank - lo
    return ordered[lo] * (1.0 - weight) + ordered[hi] * weight


def collect_latency_ms(fn: Callable[[], None], *, warmup: int, iterations: int) -> List[float]:
    for _ in range(max(0, warmup)):
        fn()
    out: List[float] = []
    for _ in range(max(1, iterations)):
        started = time.perf_counter()
        fn()
        out.append((time.perf_counter() - started) * 1000.0)
    return out


def default_perf_controls() -> Tuple[int, int]:
    warmup = perf_env_int(key="DM_PERF_WARMUP", default=10)
    iterations = perf_env_int(key="DM_PERF_ITERATIONS", default=80)
    return warmup, iterations


def _perf_metrics_enabled() -> bool:
    raw = perf_env(key="DM_PERF_LOG_ENABLED", default="1").strip().lower()
    return raw not in ("0", "false", "no", "off")


def _perf_run_id() -> str:
    return _RUN_ID


def _perf_metrics_path() -> Path:
    raw = perf_env(key="DM_PERF_METRICS_PATH", default="").strip()
    if raw:
        return Path(raw)
    return Path(__file__).resolve().parents[1] / "logs" / "perf" / "perf_metrics.jsonl"


def _summary(samples: List[float]) -> Dict[str, float]:
    if not samples:
        return {
            "count": 0.0,
            "min_ms": 0.0,
            "max_ms": 0.0,
            "mean_ms": 0.0,
            "p50_ms": 0.0,
            "p95_ms": 0.0,
            "p99_ms": 0.0,
        }
    total = float(sum(samples))
    count = float(len(samples))
    return {
        "count": count,
        "min_ms": float(min(samples)),
        "max_ms": float(max(samples)),
        "mean_ms": total / count,
        "p50_ms": float(percentile(samples, 50.0)),
        "p95_ms": float(percentile(samples, 95.0)),
        "p99_ms": float(percentile(samples, 99.0)),
    }


def record_perf_metric(
    *,
    metric: str,
    samples: List[float],
    budget_ms: Optional[float] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, float]:
    stats = _summary(samples)
    if not _perf_metrics_enabled():
        return stats

    payload: Dict[str, Any] = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "run_id": _perf_run_id(),
        "metric": str(metric),
        "count": int(stats["count"]),
        "min_ms": float(stats["min_ms"]),
        "max_ms": float(stats["max_ms"]),
        "mean_ms": float(stats["mean_ms"]),
        "p50_ms": float(stats["p50_ms"]),
        "p95_ms": float(stats["p95_ms"]),
        "p99_ms": float(stats["p99_ms"]),
    }
    if budget_ms is not None:
        payload["budget_ms"] = float(budget_ms)
    if extra:
        payload["extra"] = dict(extra)

    path = _perf_metrics_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except OSError:
        return stats
    return stats
