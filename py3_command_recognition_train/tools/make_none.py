#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import random
import re
from pathlib import Path
from typing import Iterable, Any

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


def is_markdown_like(text: str) -> bool:
    cleaned = text.lstrip()
    if "```" in text:
        return True
    if cleaned.startswith(("###", "```", "#", "- ", "* ", "> ")):
        return True
    return False


def is_valid(text: str, min_len: int, max_len: int) -> bool:
    if not (min_len <= len(text) <= max_len):
        return False
    if URL_RE.search(text):
        return False
    lowered = text.casefold()
    if "assistant:" in lowered or "system:" in lowered:
        return False
    if "###" in text or is_markdown_like(text):
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


def extract_from_jsonl(path: Path) -> list[str]:
    texts: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        text = payload.get("text", "")
        if text:
            texts.append(text)
    return texts


def extract_text_from_value(value: Any) -> str:
    if isinstance(value, str) and value.strip():
        return value
    if isinstance(value, (list, tuple)):
        parts = [item for item in value if isinstance(item, str) and item.strip()]
        if parts:
            return " ".join(parts)
    return ""


def extract_texts_from_row(row: dict[str, Any]) -> list[str]:
    messages = row.get("messages")
    if isinstance(messages, list):
        texts: list[str] = []
        for message in messages:
            if not isinstance(message, dict):
                continue
            role = str(message.get("role", "")).casefold()
            if role != "user":
                continue
            text = extract_text_from_value(message.get("content")) or extract_text_from_value(message.get("text"))
            if text:
                texts.append(text)
        if texts:
            return texts

    preferred_keys = [
        "text",
        "utterance",
        "prompt",
        "response",
        "question",
        "answer",
        "title",
        "body",
        "instruction",
        "output",
        "input",
    ]
    for key in preferred_keys:
        value = row.get(key)
        text = extract_text_from_value(value)
        if text:
            return [text]
    for value in row.values():
        text = extract_text_from_value(value)
        if text:
            return [text]
    return []


def is_robot_like(text: str) -> bool:
    lowered = text.casefold()
    if lowered.rstrip().endswith("?"):
        return True
    keywords = [
        "kun je",
        "kan je",
        "wil je",
        "jij",
        "je",
        "hoe",
        "wat",
        "waarom",
        "robot",
        "nao",
        "alex",
        "rene",
    ]
    return any(keyword in lowered for keyword in keywords)


def hard_negative_share(texts: list[str]) -> float:
    if not texts:
        return 0.0
    keyword_texts = [text for text in texts if any(keyword in text.casefold() for keyword in KEYWORDS)]
    return len(keyword_texts) / len(texts)


def extract_from_hf() -> list[str]:
    try:
        from datasets import load_dataset
    except ImportError as exc:
        raise SystemExit("Missing optional dependency 'datasets'. Install with pip install -e .[extras]") from exc

    dataset_ids = [
        "BramVanroy/dutch_chat_datasets",
        "BramVanroy/quora-chat-dutch",
        "BramVanroy/stackoverflow-chat-dutch",
    ]
    last_error: Exception | None = None
    for dataset_id in dataset_ids:
        try:
            try:
                dataset = load_dataset(dataset_id, split="train")
            except Exception:
                dataset_dict = load_dataset(dataset_id)
                split_names = list(dataset_dict.keys())
                chosen_split = None
                if "train_sft" in dataset_dict:
                    chosen_split = "train_sft"
                elif any(name.startswith("train") for name in split_names):
                    chosen_split = next(name for name in split_names if name.startswith("train"))
                else:
                    chosen_split = max(split_names, key=lambda name: dataset_dict[name].num_rows)
                dataset = dataset_dict[chosen_split]
            texts: list[str] = []
            for row in dataset:
                row_texts = extract_texts_from_row(row)
                if row_texts:
                    texts.extend(row_texts)
            if texts:
                return texts
        except Exception as exc:
            last_error = exc
            continue
    if last_error:
        raise SystemExit(
            "No HF dataset could be loaded. Tried: "
            + ", ".join(dataset_ids)
            + f". Last error: {last_error}"
        )
    return []


def filter_texts(texts: Iterable[str], min_len: int, max_len: int) -> list[str]:
    seen: set[str] = set()
    filtered: list[str] = []
    for text in texts:
        cleaned = normalize_text(text)
        if not cleaned:
            continue
        if not is_valid(cleaned, min_len, max_len):
            continue
        key = cleaned.casefold()
        if key in seen:
            continue
        seen.add(key)
        filtered.append(cleaned)
    return filtered


def dedupe_texts(texts: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for text in texts:
        cleaned = normalize_text(text)
        if not cleaned:
            continue
        key = cleaned.casefold()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(cleaned)
    return deduped


def oversample_keyword_entries(entries: list[dict[str, str]], target_ratio: float = 0.4) -> list[dict[str, str]]:
    if not entries:
        return entries
    keyword_entries = [entry for entry in entries if any(keyword in entry["text"].casefold() for keyword in KEYWORDS)]
    target_count = int(round(len(entries) * target_ratio))
    if len(keyword_entries) >= target_count or not keyword_entries:
        return entries
    extra_needed = target_count - len(keyword_entries)
    extra = [dict(random.choice(keyword_entries)) for _ in range(extra_needed)]
    return entries + extra


def write_none(entries: Iterable[dict[str, str]], out_path: Path) -> None:
    with out_path.open("w", encoding="utf-8") as handle:
        for entry in entries:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


def count_by_source(entries: Iterable[dict[str, str]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for entry in entries:
        source = entry.get("source", "unknown")
        counts[source] = counts.get(source, 0) + 1
    return counts


def dedupe_entries(entries: Iterable[dict[str, str]]) -> list[dict[str, str]]:
    seen: set[str] = set()
    deduped: list[dict[str, str]] = []
    for entry in entries:
        key = normalize_text(entry["text"]).casefold()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(entry)
    return deduped


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate NONE/OTHER dataset.")
    parser.add_argument("--from-file", type=Path, help="Deprecated. Use --seed-file instead.")
    parser.add_argument(
        "--seed-file",
        type=Path,
        default=Path("data/none_seed.txt"),
        help="Optional text or JSONL file with one utterance per line.",
    )
    parser.add_argument("--download-hf", action="store_true", help="Download dutch_chat_datasets from HuggingFace.")
    parser.add_argument(
        "--candidates-file",
        type=Path,
        default=Path("data/none_candidates.jsonl"),
        help="Optional JSONL with NONE candidates.",
    )
    parser.add_argument(
        "--candidates",
        dest="candidates_file",
        type=Path,
        help="Deprecated. Use --candidates-file instead.",
    )
    parser.add_argument("--min-len", type=int, default=3, help="Minimum length for NONE samples.")
    parser.add_argument("--max-len", type=int, default=90, help="Maximum length for NONE samples.")
    parser.add_argument("--max-none", type=int, default=5000, help="Maximum NONE samples to write (0 disables).")
    parser.add_argument("--max-seed", type=int, default=0, help="Maximum seed samples to keep (0 disables).")
    parser.add_argument(
        "--max-candidates",
        type=int,
        default=0,
        help="Maximum candidates samples to keep (0 disables).",
    )
    parser.add_argument("--robot-like", action="store_true", help="Keep only robot-like utterances.")
    parser.add_argument("--no-robot-like", action="store_true", help="Disable robot-like filtering.")
    parser.add_argument("--output", type=Path, default=Path("data/none.jsonl"))
    parser.add_argument(
        "--output-seed",
        type=Path,
        default=None,
        help="Optional output path for seed-only NONE entries (JSONL).",
    )
    parser.add_argument(
        "--output-external",
        type=Path,
        default=None,
        help="Optional output path for external NONE entries (candidates + hf) (JSONL).",
    )
    args = parser.parse_args()

    if args.robot_like and args.no_robot_like:
        raise SystemExit("Use either --robot-like or --no-robot-like, not both.")

    robot_like_all = args.robot_like
    robot_like_hf = args.robot_like or (args.download_hf and not args.no_robot_like)

    seed_texts: list[str] = []
    candidate_texts: list[str] = []
    hf_texts: list[str] = []
    source_stats: list[tuple[str, int, int, int, float]] = []
    seed_paths = [args.seed_file]
    if args.from_file:
        seed_paths.append(args.from_file)
    for seed_path in [path for path in seed_paths if path and path.exists()]:
        raw_texts = extract_from_file(seed_path)
        filtered = filter_texts(raw_texts, args.min_len, args.max_len)
        selected = [text for text in filtered if not robot_like_all or is_robot_like(text)]
        source_stats.append(
            (
                f"seed:{seed_path}",
                len(raw_texts),
                len(filtered),
                len(selected),
                hard_negative_share(selected),
            )
        )
        seed_texts.extend(selected)

    if args.candidates_file and args.candidates_file.exists():
        raw_texts = extract_from_jsonl(args.candidates_file)
        filtered = filter_texts(raw_texts, args.min_len, args.max_len)
        source_stats.append(
            (
                f"candidates:{args.candidates_file}",
                len(raw_texts),
                len(filtered),
                len(filtered),
                hard_negative_share(filtered),
            )
        )
        candidate_texts.extend(filtered)

    if args.download_hf:
        raw_texts = extract_from_hf()
        filtered = filter_texts(raw_texts, args.min_len, args.max_len)
        selected = [text for text in filtered if not robot_like_hf or is_robot_like(text)]
        source_stats.append(
            (
                "hf",
                len(raw_texts),
                len(filtered),
                len(selected),
                hard_negative_share(selected),
            )
        )
        hf_texts.extend(selected)

    if not seed_texts and not candidate_texts and not hf_texts:
        raise SystemExit("No NONE samples found. Provide seed/candidates or enable --download-hf.")

    rng = random.Random(42)
    if args.max_seed and len(seed_texts) > args.max_seed:
        seed_texts = rng.sample(seed_texts, k=args.max_seed)
    if args.max_candidates and len(candidate_texts) > args.max_candidates:
        candidate_texts = rng.sample(candidate_texts, k=args.max_candidates)

    seed_entries = [{"text": text, "label": "NONE", "source": "seed"} for text in dedupe_texts(seed_texts)]
    candidate_entries = [{"text": text, "label": "NONE", "source": "candidates"} for text in dedupe_texts(candidate_texts)]
    hf_entries = [{"text": text, "label": "NONE", "source": "hf"} for text in dedupe_texts(hf_texts)]

    combined = dedupe_entries(seed_entries + candidate_entries + hf_entries)
    balanced = oversample_keyword_entries(combined)

    before_cap = count_by_source(balanced)
    max_none = args.max_none
    if max_none and len(balanced) > max_none:
        seed_entries = [entry for entry in balanced if entry["source"] == "seed"]
        candidate_entries = [entry for entry in balanced if entry["source"] == "candidates"]
        hf_entries = [entry for entry in balanced if entry["source"] == "hf"]
        required = len(seed_entries) + len(candidate_entries)
        if max_none < required:
            max_none = required
        remaining = max_none - required
        if remaining < len(hf_entries):
            hf_entries = rng.sample(hf_entries, k=remaining)
        balanced = seed_entries + candidate_entries + hf_entries

    after_cap = count_by_source(balanced)
    write_none(balanced, args.output)
    if args.output_seed:
        seed_only = [entry for entry in balanced if entry.get("source") == "seed"]
        write_none(seed_only, args.output_seed)
    if args.output_external:
        external_only = [entry for entry in balanced if entry.get("source") != "seed"]
        write_none(external_only, args.output_external)
    print(f"Wrote {len(balanced)} NONE utterances to {args.output}")
    print(f"Source counts before cap: {before_cap}")
    print(f"Source counts after cap: {after_cap}")
    for source, extracted, after_filter, selected, share in source_stats:
        print(
            f"Source {source}: extracted={extracted} "
            f"after_filter={after_filter} selected={selected} "
            f"hard_negative_share={share:.2f}"
        )
    print(f"Final hard_negative_share={hard_negative_share([entry['text'] for entry in balanced]):.2f}")


if __name__ == "__main__":
    main()
