#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import random
import re
from pathlib import Path
from typing import Iterable

URL_RE = re.compile(r"https?://|www\.", re.IGNORECASE)

KEYWORDS = [
    "boks",
    "box",
    "high five",
    "zwaai",
    "dans",
    "stop",
    "zit",
    "ga zitten",
    "sta op",
    "rust",
    "loop",
    "vooruit",
    "achteruit",
    "links",
    "rechts",
    "draai",
    "loopmodus",
]


def normalize_text(text: str) -> str:
    return " ".join(text.split()).strip()


def is_valid(text: str) -> bool:
    if not (5 <= len(text) <= 120):
        return False
    if URL_RE.search(text):
        return False
    if text.isdigit():
        return False
    return True


def extract_from_file(path: Path) -> list[str]:
    lines: list[str] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        stripped = raw_line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            continue
        if stripped.startswith("{"):
            try:
                payload = json.loads(stripped)
                text = payload.get("text", "")
                if text:
                    lines.append(text)
            except json.JSONDecodeError:
                continue
            continue
        if stripped.startswith("-"):
            stripped = stripped[1:].strip()
        lines.append(stripped)
    return lines


def extract_from_hf() -> list[str]:
    try:
        from datasets import load_dataset
    except ImportError as exc:
        raise SystemExit("Missing optional dependency 'datasets'. Install with pip install -e .[extras]") from exc

    dataset = load_dataset("dutch_chat_datasets", split="train")
    texts: list[str] = []
    for row in dataset:
        text = row.get("text") or row.get("utterance") or ""
        if text:
            texts.append(text)
    return texts


def filter_texts(texts: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    filtered: list[str] = []
    for text in texts:
        cleaned = normalize_text(text)
        if not cleaned:
            continue
        if not is_valid(cleaned):
            continue
        key = cleaned.casefold()
        if key in seen:
            continue
        seen.add(key)
        filtered.append(cleaned)
    return filtered


def oversample_keywords(texts: list[str], target_ratio: float = 0.4) -> list[str]:
    if not texts:
        return texts
    keyword_texts = [text for text in texts if any(keyword in text.casefold() for keyword in KEYWORDS)]
    target_count = int(round(len(texts) * target_ratio))
    if len(keyword_texts) >= target_count or not keyword_texts:
        return texts
    extra_needed = target_count - len(keyword_texts)
    extra = [random.choice(keyword_texts) for _ in range(extra_needed)]
    return texts + extra


def write_none(texts: Iterable[str], out_path: Path) -> None:
    with out_path.open("w", encoding="utf-8") as handle:
        for text in texts:
            handle.write(json.dumps({"text": text, "label": "NONE"}, ensure_ascii=False) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate NONE/OTHER dataset.")
    parser.add_argument("--from-file", type=Path, help="Text or JSONL file with one utterance per line.")
    parser.add_argument("--download-hf", action="store_true", help="Download dutch_chat_datasets from HuggingFace.")
    parser.add_argument("--output", type=Path, default=Path("data/none.jsonl"))
    args = parser.parse_args()

    if not args.from_file and not args.download_hf:
        raise SystemExit("Provide --from-file or --download-hf")

    texts: list[str] = []
    if args.from_file:
        texts.extend(extract_from_file(args.from_file))
    if args.download_hf:
        texts.extend(extract_from_hf())

    filtered = filter_texts(texts)
    balanced = oversample_keywords(filtered)
    write_none(balanced, args.output)
    print(f"Wrote {len(balanced)} NONE utterances to {args.output}")


if __name__ == "__main__":
    main()
