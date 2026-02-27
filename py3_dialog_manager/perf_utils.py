from __future__ import annotations

import os
import time
from typing import Callable, List, Tuple


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
