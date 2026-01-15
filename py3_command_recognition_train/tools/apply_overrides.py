#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path


def load_none(path: Path) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        payload = json.loads(line)
        entries.append({"text": payload["text"], "label": payload.get("label", "NONE")})
    return entries


def write_jsonl(entries: list[dict[str, str]], path: Path) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for entry in entries:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Apply review overrides to NONE dataset.")
    parser.add_argument("--review", type=Path, default=Path("review/none_sample.csv"))
    parser.add_argument("--input", type=Path, default=Path("data/none.jsonl"))
    parser.add_argument("--clean", type=Path, default=Path("data/none_clean.jsonl"))
    parser.add_argument("--overrides", type=Path, default=Path("data/none_overrides.jsonl"))
    args = parser.parse_args()

    none_entries = load_none(args.input)

    overrides: dict[int, dict[str, str]] = {}
    with args.review.open("r", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            idx = int(row["id"])
            overrides[idx] = {
                "keep": row.get("keep", "1"),
                "label": row.get("label", "NONE"),
                "notes": row.get("notes", ""),
            }

    cleaned: list[dict[str, str]] = []
    override_entries: list[dict[str, str]] = []

    for idx, entry in enumerate(none_entries):
        override = overrides.get(idx)
        if not override:
            cleaned.append(entry)
            continue
        keep = override["keep"]
        label = override["label"].strip() or "NONE"
        if keep == "0":
            override_entries.append({"text": entry["text"], "label": label, "keep": 0})
            continue
        if label != "NONE":
            override_entries.append({"text": entry["text"], "label": label, "keep": 1})
            cleaned.append({"text": entry["text"], "label": label})
        else:
            cleaned.append(entry)

    write_jsonl(cleaned, args.clean)

    with args.overrides.open("w", encoding="utf-8") as handle:
        for entry in override_entries:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")

    print(f"Wrote {len(cleaned)} cleaned entries to {args.clean}")


if __name__ == "__main__":
    main()
