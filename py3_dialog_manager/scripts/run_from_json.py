# py3_nao_behavior_manager/scripts/run_from_json.py
#!/usr/bin/env python3
import argparse
from pathlib import Path

from dialog.pipeline_builder import build_pipeline_from_json


_PACKAGE_ROOT = Path(__file__).resolve().parents[1]


def _resolve_config_path(path: str) -> str:
    candidate = Path(path)
    if candidate.is_absolute():
        return str(candidate)
    return str((_PACKAGE_ROOT / candidate).resolve())


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument(
        "--config",
        required=True,
        help="Pad naar run-config JSON, bijv configs/run_console_ollama_local_console.json",
    )
    args = p.parse_args()

    config_path = _resolve_config_path(args.config)
    pipeline = build_pipeline_from_json(config_path)

    history = []
    print(f"[RUN] config={config_path}  (Ctrl+C om te stoppen)")
    try:
        while True:
            turn = pipeline.run_once(history=history)
            history = turn.llm.messages
    except KeyboardInterrupt:
        print("\n[Stop]")


if __name__ == "__main__":
    main()
