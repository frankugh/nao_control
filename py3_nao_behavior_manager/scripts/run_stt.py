#!/usr/bin/env python3
import argparse

from dialog.pipeline import build_pipeline

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--profile", default="laptop_whisper_print")
    args = p.parse_args()

    pipe = build_pipeline(args.profile)

    print(f"[RUN] profile={args.profile}  (Ctrl+C om te stoppen)")
    try:
        while True:
            pipe.run_once()
    except KeyboardInterrupt:
        print("\n[Stop]")

if __name__ == "__main__":
    main()
