#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import random
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from cmdrec.commands import EXPECTED_LABELS, normalize_key, normalize_text, parse_commands_markdown


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    entries: list[dict[str, Any]] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line:
            continue
        entries.append(json.loads(line))
    return entries


def write_jsonl(entries: Iterable[dict[str, Any]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for entry in entries:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


def label_counts(entries: Iterable[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for entry in entries:
        label = str(entry.get("label") or "")
        counts[label] = counts.get(label, 0) + 1
    return counts


def normalize_label(raw_label: Any, known_plain_labels: set[str]) -> str:
    plain = str(raw_label or "").strip().upper()
    plain = plain.replace("\\_", "_")
    plain = plain.replace("-", "_").replace(" ", "_")
    if plain not in known_plain_labels:
        raise ValueError(f"Unknown label '{raw_label}' (normalized='{plain}')")
    if plain == "NONE":
        return plain
    # Keep compatibility with existing datasets that use markdown-escaped underscores.
    return plain.replace("_", "\\_")


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

    deduped: list[str] = []
    seen: set[str] = set()
    for text in texts:
        key = normalize_key(text)
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append(text)
    return deduped


def stratified_split(
    entries: list[dict[str, Any]], gold_ratio: float, seed: int
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rng = random.Random(seed)
    by_label: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for entry in entries:
        by_label[str(entry["label"])].append(entry)

    gold: list[dict[str, Any]] = []
    train: list[dict[str, Any]] = []
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


def count_jsonl_lines(path: Path) -> int:
    if not path.exists():
        return 0
    count = 0
    for raw in path.read_text(encoding="utf-8").splitlines():
        if raw.strip():
            count += 1
    return count


def merge_with_override(
    base_map: dict[str, dict[str, Any]],
    incoming_entries: Iterable[dict[str, Any]],
    source_default: str,
) -> tuple[int, int, int]:
    added = 0
    duplicate_same_label = 0
    overridden_conflict = 0
    for entry in incoming_entries:
        text = normalize_text(str(entry.get("text") or ""))
        if not text:
            continue
        key = normalize_key(text)
        if not key:
            continue
        label = str(entry.get("label") or "")
        source = str(entry.get("source") or source_default)
        row = {"text": text, "label": label, "source": source}
        existing = base_map.get(key)
        if existing is None:
            base_map[key] = row
            added += 1
            continue
        if str(existing.get("label") or "") == label:
            duplicate_same_label += 1
            continue
        base_map[key] = row
        overridden_conflict += 1
    return added, duplicate_same_label, overridden_conflict


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build gold_v2 + train_base_v2 from raw sources and reviewed data."
    )
    parser.add_argument("--commands", type=Path, default=Path("data/commands_raw.md"))
    parser.add_argument("--none-seed", type=Path, default=Path("data/none_seed.txt"))
    parser.add_argument("--reviewed", type=Path, default=Path("data/al/reviewed.jsonl"))
    parser.add_argument("--gold-candidates", type=Path, default=Path("data/al/gold_candidates.jsonl"))
    parser.add_argument("--none-external", type=Path, default=Path("data/none_external.jsonl"))
    parser.add_argument("--gold-out", type=Path, default=Path("data/gold_v2.jsonl"))
    parser.add_argument("--train-out", type=Path, default=Path("data/train_base_v2.jsonl"))
    parser.add_argument("--report-out", type=Path, default=Path("data/gold_v2_build_report.json"))
    parser.add_argument("--gold-ratio", type=float, default=0.20)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    known_plain_labels = set(EXPECTED_LABELS)
    known_plain_labels.update({"NONE", "LOCOMOTION_REQUEST"})

    command_entries_raw = parse_commands_markdown(args.commands)
    command_entries: list[dict[str, Any]] = []
    command_keys: set[str] = set()
    for entry in command_entries_raw:
        text = normalize_text(str(entry.get("text") or ""))
        if not text:
            continue
        key = normalize_key(text)
        if not key:
            continue
        label = normalize_label(entry.get("label"), known_plain_labels)
        if key in command_keys:
            continue
        command_keys.add(key)
        command_entries.append({"text": text, "label": label, "source": "commands_raw"})

    none_seed_texts = load_none_seed(args.none_seed)
    none_entries: list[dict[str, Any]] = []
    for text in none_seed_texts:
        key = normalize_key(text)
        if key in command_keys:
            continue
        none_entries.append({"text": text, "label": "NONE", "source": "seed"})

    raw_pool_map: dict[str, dict[str, Any]] = {}
    merge_with_override(raw_pool_map, command_entries + none_entries, "raw")

    gold_candidate_entries_raw = load_jsonl(args.gold_candidates)
    gold_candidate_entries: list[dict[str, Any]] = []
    gold_candidate_keys: set[str] = set()
    for entry in gold_candidate_entries_raw:
        text = normalize_text(str(entry.get("text") or ""))
        if not text:
            continue
        key = normalize_key(text)
        if not key:
            continue
        label = normalize_label(entry.get("label"), known_plain_labels)
        gold_candidate_entries.append(
            {
                "text": text,
                "label": label,
                "source": str(entry.get("source") or "al_gold_candidate"),
            }
        )
        gold_candidate_keys.add(key)

    gc_added, gc_dupe, gc_override = merge_with_override(
        raw_pool_map, gold_candidate_entries, "al_gold_candidate"
    )
    split_input = list(raw_pool_map.values())

    gold_v2, train_split = stratified_split(split_input, args.gold_ratio, args.seed)
    gold_keys = {normalize_key(str(entry.get("text") or "")) for entry in gold_v2}

    reviewed_entries_raw = load_jsonl(args.reviewed)
    reviewed_train_only: list[dict[str, Any]] = []
    for entry in reviewed_entries_raw:
        text = normalize_text(str(entry.get("text") or ""))
        if not text:
            continue
        key = normalize_key(text)
        if not key:
            continue
        if key in gold_candidate_keys:
            continue
        label = normalize_label(entry.get("label"), known_plain_labels)
        reviewed_train_only.append(
            {
                "text": text,
                "label": label,
                "source": str(entry.get("source") or "al_reviewed"),
            }
        )

    train_map: dict[str, dict[str, Any]] = {}
    merge_with_override(train_map, train_split, "split_train")
    train_pre_aug_count = len(train_map)

    reviewed_added = 0
    reviewed_dupe = 0
    reviewed_override = 0
    reviewed_skipped_in_gold = 0
    for entry in reviewed_train_only:
        key = normalize_key(str(entry.get("text") or ""))
        if key in gold_keys:
            reviewed_skipped_in_gold += 1
            continue
        existing = train_map.get(key)
        if existing is None:
            train_map[key] = entry
            reviewed_added += 1
            continue
        if str(existing.get("label") or "") == str(entry.get("label") or ""):
            reviewed_dupe += 1
            continue
        train_map[key] = entry
        reviewed_override += 1

    leakage_removed = 0
    for key in list(train_map.keys()):
        if key in gold_keys:
            leakage_removed += 1
            del train_map[key]

    final_train = list(train_map.values())

    write_jsonl(gold_v2, args.gold_out)
    write_jsonl(final_train, args.train_out)

    report = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "inputs": {
            "commands": str(args.commands),
            "none_seed": str(args.none_seed),
            "gold_candidates": str(args.gold_candidates),
            "reviewed": str(args.reviewed),
            "none_external": str(args.none_external),
            "gold_ratio": args.gold_ratio,
            "seed": args.seed,
        },
        "raw_pool": {
            "commands_count": len(command_entries),
            "none_seed_count": len(none_entries),
            "combined_before_gold_candidates": len(command_entries) + len(none_entries),
            "split_input_count": len(split_input),
            "split_input_label_counts": label_counts(split_input),
            "gold_candidates_input_count": len(gold_candidate_entries),
            "gold_candidates_added": gc_added,
            "gold_candidates_duplicate_same_label": gc_dupe,
            "gold_candidates_overridden_conflict": gc_override,
        },
        "split": {
            "gold_v2_count": len(gold_v2),
            "gold_v2_label_counts": label_counts(gold_v2),
            "train_split_count": len(train_split),
            "train_split_label_counts": label_counts(train_split),
        },
        "reviewed_train_only": {
            "reviewed_total": len(reviewed_entries_raw),
            "reviewed_train_only_candidates": len(reviewed_train_only),
            "train_before_reviewed_augment": train_pre_aug_count,
            "reviewed_added": reviewed_added,
            "reviewed_duplicate_same_label": reviewed_dupe,
            "reviewed_overridden_conflict": reviewed_override,
            "reviewed_skipped_because_in_gold_v2": reviewed_skipped_in_gold,
        },
        "outputs": {
            "gold_v2_out": str(args.gold_out),
            "train_base_v2_out": str(args.train_out),
            "gold_v2_count": len(gold_v2),
            "train_base_v2_count": len(final_train),
            "train_base_v2_label_counts": label_counts(final_train),
            "final_leakage_removed_from_train": leakage_removed,
        },
        "none_external_policy": {
            "used_in_build_gold_v2": False,
            "none_external_line_count": count_jsonl_lines(args.none_external),
            "used_in_retrain_when": "Only if NONE target count (none_ratio * command_count) exceeds NONE in train pool.",
            "action_needed_now": "No immediate action for split/build. Refresh none_external only if you want different external NONE sampling in retrain.",
        },
    }

    args.report_out.parent.mkdir(parents=True, exist_ok=True)
    args.report_out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"Wrote gold_v2: {len(gold_v2)} -> {args.gold_out}")
    print(f"Wrote train_base_v2: {len(final_train)} -> {args.train_out}")
    print(f"Wrote report: {args.report_out}")


if __name__ == "__main__":
    main()
