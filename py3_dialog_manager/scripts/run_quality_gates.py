from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List


ROOT = Path(__file__).resolve().parents[1]
PYTHON = sys.executable


@dataclass
class GateStep:
    name: str
    command: List[str]
    required: bool
    hint: str


def _run_step(step: GateStep) -> int:
    started = time.perf_counter()
    cmd_display = " ".join(step.command)
    print(f"[gate:start] {step.name}")
    print(f"[gate:cmd]   {cmd_display}")
    code = subprocess.call(step.command, cwd=str(ROOT), env=os.environ.copy())
    elapsed = time.perf_counter() - started
    status = "PASS" if code == 0 else "FAIL"
    print(f"[gate:{status.lower()}] {step.name} ({elapsed:.2f}s)")
    if code != 0 and step.hint:
        print(f"[gate:hint]  {step.hint}")
    return code


def _build_steps(*, with_perf: bool, with_ui_contract: bool) -> List[GateStep]:
    steps: List[GateStep] = [
        GateStep(
            name="Backend Regression",
            command=[PYTHON, "-m", "pytest", "-q", "tests", "-m", "not perf and not ui_contract"],
            required=True,
            hint="Fix regressions first; this gate blocks all code merges.",
        ),
    ]
    if with_ui_contract:
        steps.append(
            GateStep(
                name="UI Contract",
                command=[PYTHON, "-m", "pytest", "-q", "tests/test_ui_logs_contract.py", "-m", "ui_contract"],
                required=True,
                hint="UI contract drift detected. Align HTML IDs/options and refresh policy.",
            )
        )
    if with_perf:
        steps.append(
            GateStep(
                name="Performance Smoke",
                command=[PYTHON, "-m", "pytest", "-q", "tests", "-m", "perf"],
                required=True,
                hint=(
                    "Performance budget failed. Inspect p95 latency and update code or "
                    "explicitly tune budget env vars only with evidence."
                ),
            )
        )
        steps.append(
            GateStep(
                name="Performance Guard",
                command=[
                    PYTHON,
                    "scripts/perf_compare_latest.py",
                    "--baseline-file",
                    "logs/perf/baseline_run_id.txt",
                    "--rerun-on-warn",
                    "--rerun-count",
                    "3",
                    "--rerun-warmup",
                    "30",
                    "--rerun-iterations",
                    "250",
                    "--python-exe",
                    PYTHON,
                    "--workdir",
                    str(ROOT),
                ],
                required=True,
                hint="Performance regression persists after automatic recheck.",
            )
        )
    return steps


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run dialog-manager quality gates with explicit blocking stages."
    )
    parser.add_argument(
        "--with-perf",
        action="store_true",
        help="Include performance smoke gate (latency budgets).",
    )
    parser.add_argument(
        "--skip-ui-contract",
        action="store_true",
        help="Skip UI contract gate (not recommended).",
    )
    args = parser.parse_args()

    steps = _build_steps(with_perf=bool(args.with_perf), with_ui_contract=not bool(args.skip_ui_contract))
    failures: List[str] = []

    print("[gate] root:", ROOT)
    for step in steps:
        code = _run_step(step)
        if code != 0:
            failures.append(step.name)
            if step.required:
                break

    if failures:
        print("[gate:summary] FAIL")
        print("[gate:failed] ", ", ".join(failures))
        return 1

    print("[gate:summary] PASS")
    if bool(args.with_perf):
        print("[gate:perf] metrics: logs/perf/perf_metrics.jsonl")
        print("[gate:perf] compare: venv/Scripts/python.exe scripts/perf_compare_latest.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
