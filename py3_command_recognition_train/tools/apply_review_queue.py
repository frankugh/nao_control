#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cmdrec.commands import EXPECTED_LABELS, LOCOMOTION_LABELS, normalize_key


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    if not path.exists():
        return entries
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        entries.append(json.loads(line))
    return entries


def write_jsonl(entries: list[dict[str, Any]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for entry in entries:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


def entry_id(entry: dict[str, Any]) -> str:
    raw_id = entry.get("id")
    if raw_id:
        return str(raw_id)
    basis = f"{entry.get('text','')}|{entry.get('reason','')}|{entry.get('timestamp','')}"
    return normalize_key(basis)[:12]
def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"since_last_retrain": 0}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"since_last_retrain": 0}


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")




def normalize_label(value: str, fallback: str) -> str:
    label = (value or "").strip()
    if not label:
        return fallback
    return label.upper()


def main() -> None:
    parser = argparse.ArgumentParser(description="Apply AL review CSV to reviewed datasets.")
    parser.add_argument("--review", type=Path, default=Path("review/al_review.csv"))
    parser.add_argument("--queue", type=Path, default=Path("data/al/review_queue.jsonl"))
    parser.add_argument("--review-log", type=Path, default=Path("data/al/review_log.jsonl"))
    parser.add_argument("--reviewed-out", type=Path, default=Path("data/al/reviewed.jsonl"))
    parser.add_argument("--gold-candidates", type=Path, default=Path("data/al/gold_candidates.jsonl"))
    parser.add_argument("--auto-retrain", action="store_true", default=False)
    parser.add_argument("--auto-retrain-threshold", type=int, default=50)
    parser.add_argument("--auto-train-sample", type=int, default=25)
    parser.add_argument("--auto-train-conf-min", type=float, default=None)
    parser.add_argument("--auto-train-conf-max", type=float, default=0.75)
    parser.add_argument("--auto-retrain-state", type=Path, default=Path("data/al/retrain_state.json"))
    parser.add_argument("--keep-review-file", action="store_true", default=False)
    args = parser.parse_args()

    queue_entries = {entry_id(item): item for item in load_jsonl(args.queue)}
    review_log = load_jsonl(args.review_log)
    reviewed_ids = {entry_id(item) for item in review_log}

    reviewed_entries = load_jsonl(args.reviewed_out)
    reviewed_keys = {normalize_key(entry.get("text", "")) for entry in reviewed_entries}

    gold_entries = load_jsonl(args.gold_candidates)
    gold_keys = {normalize_key(entry.get("text", "")) for entry in gold_entries}

    new_reviewed: list[dict[str, Any]] = []
    new_gold: list[dict[str, Any]] = []
    new_log: list[dict[str, Any]] = []

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    allowed_labels = set(EXPECTED_LABELS)
    allowed_labels.update(LOCOMOTION_LABELS)
    allowed_labels.update({"NONE", "LOCOMOTION_REQUEST"})

    with args.review.open("r", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            rid = str(row.get("id") or "").strip()
            if not rid or rid in reviewed_ids:
                continue
            base = queue_entries.get(rid, {})
            text = (row.get("text") or base.get("text") or "").strip()
            if not text:
                continue
            keep_value = str(row.get("keep") or "").strip()
            reviewed_label_raw = str(row.get("reviewed_label") or "").strip()
            if not keep_value and not reviewed_label_raw:
                continue
            if keep_value not in {"", "0", "1"}:
                raise ValueError(f"Invalid keep value '{keep_value}' for id={rid}")

            suggested = (row.get("suggested_label") or base.get("label") or base.get("pred_label") or "NONE").strip()
            suggested = normalize_label(suggested, "NONE")
            if suggested not in allowed_labels:
                raise ValueError(f"Invalid suggested_label '{suggested}' for id={rid}")

            reviewed_label = normalize_label(reviewed_label_raw, suggested)
            if reviewed_label_raw and reviewed_label not in allowed_labels:
                raise ValueError(f"Invalid reviewed_label '{reviewed_label}' for id={rid}")

            keep = keep_value != "0"
            gold_candidate = keep and str(row.get("gold_candidate", "0")).strip() == "1"

            record = {
                "id": rid,
                "text": text,
                "suggested_label": suggested,
                "confidence": row.get("confidence") or base.get("confidence"),
                "reason": row.get("reason") or base.get("reason"),
                "stt_used": row.get("stt_used") or base.get("stt_used"),
                "stt_edited": row.get("stt_edited") or base.get("stt_edited"),
                "reviewed_label": reviewed_label,
                "keep": 1 if keep else 0,
                "gold_candidate": 1 if gold_candidate else 0,
                "notes": row.get("notes", ""),
                "timestamp": row.get("timestamp") or base.get("timestamp"),
                "reviewed_at": now,
            }
            new_log.append(record)

            if keep:
                key = normalize_key(text)
                if key not in reviewed_keys:
                    reviewed_entries.append({"text": text, "label": reviewed_label, "source": "al_reviewed"})
                    reviewed_keys.add(key)
                    new_reviewed.append({"text": text, "label": reviewed_label})
                if gold_candidate and key not in gold_keys:
                    gold_entries.append({"text": text, "label": reviewed_label, "source": "al_gold_candidate"})
                    gold_keys.add(key)
                    new_gold.append({"text": text, "label": reviewed_label})

    if new_log:
        review_log.extend(new_log)
        write_jsonl(review_log, args.review_log)
    if new_reviewed:
        write_jsonl(reviewed_entries, args.reviewed_out)
    if new_gold:
        write_jsonl(gold_entries, args.gold_candidates)

    if new_log:
        reviewed_ids = {entry_id(item) for item in review_log}
        pruned_queue = [
            entry for entry in load_jsonl(args.queue) if entry_id(entry) not in reviewed_ids
        ]
        write_jsonl(pruned_queue, args.queue)

    print(f"Reviewed {len(new_log)} rows")
    print(f"Added {len(new_reviewed)} to {args.reviewed_out}")
    print(f"Added {len(new_gold)} to {args.gold_candidates}")

    if not args.keep_review_file and args.review.exists():
        try:
            args.review.unlink()
        except OSError:
            pass

    state = load_state(args.auto_retrain_state)
    since_last = int(state.get("since_last_retrain", 0))
    since_last += len(new_log)
    state["since_last_retrain"] = since_last
    save_state(args.auto_retrain_state, state)

    if args.auto_retrain and since_last >= args.auto_retrain_threshold:
        cmd = [
            sys.executable,
            "tools/retrain_cmdrec.py",
            "--out",
            "auto",
            "--promote-if-better",
            "--auto-train-sample",
            str(args.auto_train_sample),
        ]
        if args.auto_train_conf_min is not None:
            cmd.extend(["--auto-train-conf-min", str(args.auto_train_conf_min)])
        if args.auto_train_conf_max is not None:
            cmd.extend(["--auto-train-conf-max", str(args.auto_train_conf_max)])
        subprocess.run(cmd, check=True)
        state["since_last_retrain"] = 0
        save_state(args.auto_retrain_state, state)


if __name__ == "__main__":
    main()
