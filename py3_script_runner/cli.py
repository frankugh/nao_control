from __future__ import annotations

import argparse
from pathlib import Path
import sys

if __package__ in (None, ""):
    _REPO_ROOT = Path(__file__).resolve().parent.parent
    if str(_REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(_REPO_ROOT))
    from py3_script_runner.runner import ScriptRunner
    from py3_script_runner.schema import ScriptSchemaError, load_script
else:
    from .runner import ScriptRunner
    from .schema import ScriptSchemaError, load_script


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run workshop scripts against one or more DM instances.")
    parser.add_argument(
        "--script",
        required=True,
        help="Path to script JSON file.",
    )
    return parser


def _resolve_script_path(raw_path: str) -> Path:
    candidate = Path(raw_path)
    if candidate.exists():
        return candidate

    base_dir = Path(__file__).resolve().parent
    alt_local = base_dir / raw_path
    if alt_local.exists():
        return alt_local

    alt_repo = base_dir.parent / raw_path
    if alt_repo.exists():
        return alt_repo

    return candidate


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    script_path = _resolve_script_path(args.script)

    try:
        script = load_script(script_path)
    except ScriptSchemaError as exc:
        print(f"Schema error: {exc}", file=sys.stderr)
        return 2

    runner = ScriptRunner(script)
    try:
        runner.preflight()
    except Exception as exc:
        print(f"Preflight failed: {exc}", file=sys.stderr)
        return 3

    try:
        result = runner.run()
    except Exception as exc:
        print(f"Run failed: {exc}", file=sys.stderr)
        return 4
    if result.aborted:
        print(f"Run aborted after {result.completed_steps}/{result.total_steps} steps. Log: {result.log_path}")
        return 1
    print(f"Run complete: {result.completed_steps}/{result.total_steps} steps. Log: {result.log_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
