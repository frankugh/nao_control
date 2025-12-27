# py3_nao_behavior_manager/scripts/run_from_json.py
#!/usr/bin/env python3
import argparse

from dialog.pipeline_builder import build_pipeline_from_json


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--config", required=True, help="Pad naar run-config JSON, bijv configs/run_audio_laptop_whisper_stt_only.json")
    args = p.parse_args()

    pipeline = build_pipeline_from_json(args.config)

    history = []
    print(f"[RUN] config={args.config}  (Ctrl+C om te stoppen)")
    try:
        while True:
            turn = pipeline.run_once(history=history)
            history = turn.llm.messages
    except KeyboardInterrupt:
        print("\n[Stop]")


if __name__ == "__main__":
    main()
