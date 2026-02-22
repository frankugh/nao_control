from __future__ import annotations

import json
import unicodedata
from pathlib import Path
from typing import Iterable

EXPECTED_LABELS = {
    "BOX",
    "HIGH_FIVE",
    "WAVE",
    "WALK_WITH_ME",
    "WALK_FORWARD",
    "WALK_BACKWARD",
    "WALK_LEFT",
    "WALK_RIGHT",
    "TURN_LEFT",
    "TURN_RIGHT",
    "STOP",
    "SITDOWN",
    "STAND_UP",
    "REST",
    "DANCE",
}

LOCOMOTION_LABELS = {
    "WALK_FORWARD",
    "WALK_BACKWARD",
    "WALK_LEFT",
    "WALK_RIGHT",
    "TURN_LEFT",
    "TURN_RIGHT",
}


def normalize_text(text: str) -> str:
    return " ".join(text.split()).strip()


def normalize_key(text: str) -> str:
    raw = normalize_text(text).casefold()
    if not raw:
        return ""
    cleaned_chars: list[str] = []
    for ch in raw:
        cat = unicodedata.category(ch)
        if cat.startswith("P") or cat.startswith("S"):
            cleaned_chars.append(" ")
        else:
            cleaned_chars.append(ch)
    return " ".join("".join(cleaned_chars).split()).strip()


def parse_label(line: str) -> str | None:
    if not line.startswith("## "):
        return None
    header = line[3:].strip()
    if not header:
        return None
    token = header.split()[0].rstrip(":").strip()
    return token.upper() if token else None


def parse_commands_markdown(path: Path) -> list[dict[str, str]]:
    current_label: str | None = None
    seen: set[str] = set()
    entries: list[dict[str, str]] = []

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        label = parse_label(raw_line)
        if label:
            current_label = label
            continue
        if not raw_line.lstrip().startswith("-"):
            continue
        if not current_label:
            continue
        text = normalize_text(raw_line.lstrip()[1:])
        if not text:
            continue
        key = normalize_key(text)
        if key in seen:
            continue
        seen.add(key)
        entries.append({"text": text, "label": current_label})

    return entries


def relabel(entry: dict[str, str]) -> dict[str, str]:
    label = entry["label"]
    if label in LOCOMOTION_LABELS:
        label = "LOCOMOTION_REQUEST"
    return {"text": entry["text"], "label": label}


def export_commands_jsonl(entries: Iterable[dict[str, str]], out_path: Path) -> None:
    with out_path.open("w", encoding="utf-8") as handle:
        for entry in entries:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
