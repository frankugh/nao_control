#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path

from cmdrec.commands import export_commands_jsonl, normalize_text


def load_lines(path: Path) -> list[str]:
    lines: list[str] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        cleaned = normalize_text(raw_line)
        if not cleaned:
            continue
        if cleaned.startswith("#"):
            continue
        lines.append(cleaned)
    return lines


def main() -> None:
    parser = argparse.ArgumentParser(description="Export NONE candidates to JSONL.")
    parser.add_argument(
        "--input",
        type=Path,
        default=Path("data/none_candidates.txt"),
        help="Path to input text file.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("data/none_candidates.jsonl"),
        help="Path to output JSONL.",
    )
    args = parser.parse_args()

    seen: set[str] = set()
    entries: list[dict[str, str]] = []
    for text in load_lines(args.input):
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        entries.append({"text": text, "label": "NONE"})

    export_commands_jsonl(entries, args.output)
    print(f"Exported {len(entries)} NONE candidates to {args.output}")


if __name__ == "__main__":
    main()
