#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import random
from collections import defaultdict
from pathlib import Path
from typing import Iterable

from cmdrec.commands import normalize_key, normalize_text, parse_commands_markdown, relabel


def load_none_seed(path: Path) -> list[str]:
    texts: list[str] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        stripped = raw_line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            continue
        if stripped.startswith("-"):
            stripped = stripped[1:].strip()
        cleaned = normalize_text(stripped)
        if cleaned:
            texts.append(cleaned)
    # dedupe while preserving order
    seen: set[str] = set()
    deduped: list[str] = []
    for text in texts:
        key = normalize_key(text)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(text)
    return deduped


def write_jsonl(entries: Iterable[dict[str, str]], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as handle:
        for entry in entries:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


def stratified_split(
    entries: list[dict[str, str]], gold_ratio: float, seed: int
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    rng = random.Random(seed)
    by_label: dict[str, list[dict[str, str]]] = defaultdict(list)
    for entry in entries:
        by_label[entry["label"]].append(entry)

    gold: list[dict[str, str]] = []
    train: list[dict[str, str]] = []

    for label in sorted(by_label.keys()):
        items = list(by_label[label])
        rng.shuffle(items)
        n = len(items)
        if n == 0:
            continue
        if gold_ratio <= 0:
            gold_count = 0
        elif n == 1:
            gold_count = 1
        else:
            gold_count = max(1, int(round(n * gold_ratio)))
            if gold_count >= n:
                gold_count = n - 1
        gold.extend(items[:gold_count])
        train.extend(items[gold_count:])

    return gold, train


def main() -> None:
    parser = argparse.ArgumentParser(description="Create fixed gold/train split for cmdrec.")
    parser.add_argument(
        "--commands",
        type=Path,
        default=Path("data/commands_raw.md"),
        help="Path to commands markdown file.",
    )
    parser.add_argument(
        "--none-seed",
        type=Path,
        default=Path("data/none_seed.txt"),
        help="Path to NONE seed text file.",
    )
    parser.add_argument(
        "--gold-out",
        type=Path,
        default=Path("data/gold_v1.jsonl"),
        help="Output path for gold validation set.",
    )
    parser.add_argument(
        "--train-out",
        type=Path,
        default=Path("data/train_base.jsonl"),
        help="Output path for train base set.",
    )
    parser.add_argument("--seed", type=int, default=42, help="Random seed for split.")
    parser.add_argument("--gold-ratio", type=float, default=0.2, help="Gold split ratio.")
    args = parser.parse_args()

    command_entries = [relabel(entry) for entry in parse_commands_markdown(args.commands)]
    command_keys = {normalize_key(entry["text"]) for entry in command_entries}

    none_texts = load_none_seed(args.none_seed)
    none_entries = []
    for text in none_texts:
        if normalize_key(text) in command_keys:
            continue
        none_entries.append({"text": text, "label": "NONE"})

    combined = command_entries + none_entries
    gold, train = stratified_split(combined, args.gold_ratio, args.seed)

    write_jsonl(gold, args.gold_out)
    write_jsonl(train, args.train_out)

    def count_labels(items: list[dict[str, str]]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for entry in items:
            counts[entry["label"]] = counts.get(entry["label"], 0) + 1
        return counts

    print(f"Wrote {len(gold)} gold entries to {args.gold_out}")
    print(f"Wrote {len(train)} train entries to {args.train_out}")
    print(f"Gold label counts: {count_labels(gold)}")
    print(f"Train label counts: {count_labels(train)}")


if __name__ == "__main__":
    main()
