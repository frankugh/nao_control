#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path

from cmdrec.commands import export_commands_jsonl, parse_commands_markdown, relabel


def main() -> None:
    parser = argparse.ArgumentParser(description="Export commands from markdown to JSONL.")
    parser.add_argument(
        "--input",
        type=Path,
        default=Path("data/commands_raw.md"),
        help="Path to commands markdown file.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("data/commands.jsonl"),
        help="Path to output JSONL.",
    )
    args = parser.parse_args()

    entries = parse_commands_markdown(args.input)
    relabeled = [relabel(entry) for entry in entries]
    export_commands_jsonl(relabeled, args.output)

    print(f"Exported {len(relabeled)} commands to {args.output}")


if __name__ == "__main__":
    main()
