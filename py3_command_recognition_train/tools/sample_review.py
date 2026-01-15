#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import random
from pathlib import Path


def load_none(path: Path) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        payload = json.loads(line)
        entries.append({"text": payload["text"], "label": payload.get("label", "NONE")})
    return entries


def main() -> None:
    parser = argparse.ArgumentParser(description="Sample NONE entries for manual review.")
    parser.add_argument("--input", type=Path, default=Path("data/none.jsonl"))
    parser.add_argument("--output", type=Path, default=Path("review/none_sample.csv"))
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--n", type=int, default=200)
    args = parser.parse_args()

    entries = load_none(args.input)
    random.seed(args.seed)
    sample = random.sample(entries, k=min(args.n, len(entries)))

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["id", "text", "keep", "label", "notes"])
        for idx, entry in enumerate(sample):
            writer.writerow([idx, entry["text"], 1, "NONE", ""])

    print(f"Wrote {len(sample)} rows to {args.output}")


if __name__ == "__main__":
    main()
