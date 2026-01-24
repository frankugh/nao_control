#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
import json
from pathlib import Path
from typing import Any


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    if not path.exists():
        return entries
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        entries.append(json.loads(line))
    return entries


def entry_id(entry: dict[str, Any]) -> str:
    raw_id = entry.get("id")
    if raw_id:
        return str(raw_id)
    basis = f"{entry.get('text','')}|{entry.get('reason','')}|{entry.get('timestamp','')}"
    digest = hashlib.sha1(basis.encode("utf-8")).hexdigest()
    return digest[:12]


def pick_label(entry: dict[str, Any]) -> str:
    for key in ("suggested_label", "predicted_label", "pred_label", "label", "ml_label"):
        value = entry.get(key)
        if value:
            return str(value)
    return "NONE"


def pick_confidence(entry: dict[str, Any]) -> float | None:
    for key in ("confidence", "score", "prob", "ml_confidence"):
        value = entry.get(key)
        if value is None:
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def normalize_reason(value: Any) -> str:
    reason = str(value or "").strip().lower()
    if "declined" in reason:
        return "declined"
    if "auto" in reason:
        return "auto_executed"
    if "none" in reason:
        return "none"
    return reason or "unknown"


def reason_priority(reason: str) -> int:
    if reason == "declined":
        return 0
    if reason == "none":
        return 1
    return 2


def main() -> None:
    parser = argparse.ArgumentParser(description="Export AL review queue to CSV.")
    parser.add_argument("--queue", type=Path, default=Path("data/al/review_queue.jsonl"))
    parser.add_argument("--review-log", type=Path, default=Path("data/al/review_log.jsonl"))
    parser.add_argument("--output", type=Path, default=Path("review/al_review.csv"))
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    queue_entries = load_jsonl(args.queue)
    reviewed_ids = {entry_id(item) for item in load_jsonl(args.review_log)}

    rows: list[dict[str, Any]] = []
    for entry in queue_entries:
        text = str(entry.get("text") or "").strip()
        if not text:
            continue
        rid = entry_id(entry)
        if rid in reviewed_ids:
            continue
        reason = normalize_reason(entry.get("reason") or entry.get("event") or entry.get("source"))
        confidence = pick_confidence(entry)
        rows.append(
            {
                "id": rid,
                "text": text,
                "suggested_label": pick_label(entry),
                "confidence": "" if confidence is None else f"{confidence:.4f}",
                "reason": reason,
                "timestamp": str(entry.get("timestamp") or entry.get("ts") or ""),
                "_sort_conf": 1.0 if confidence is None else confidence,
            }
        )

    rows.sort(
        key=lambda row: (
            reason_priority(row["reason"]),
            row["_sort_conf"],
            row["timestamp"],
        )
    )

    if args.limit and len(rows) > args.limit:
        rows = rows[: args.limit]

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "id",
                "text",
                "suggested_label",
                "confidence",
                "reason",
                "stt_used",
                "stt_edited",
                "reviewed_label",
                "keep",
                "gold_candidate",
                "notes",
                "timestamp",
            ]
        )
        for row in rows:
            writer.writerow(
                [
                    row["id"],
                    row["text"],
                    row["suggested_label"],
                    row["confidence"],
                    row["reason"],
                    entry.get("stt_used", ""),
                    entry.get("stt_edited", ""),
                    "",
                    "",
                    0,
                    "",
                    row["timestamp"],
                ]
            )

    print(f"Wrote {len(rows)} rows to {args.output}")


if __name__ == "__main__":
    main()
