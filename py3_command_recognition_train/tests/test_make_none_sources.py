from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "tools" / "make_none.py"


def load_module():
    spec = importlib.util.spec_from_file_location("make_none", MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_make_none_merges_seed_and_candidates(tmp_path: Path) -> None:
    seed_path = tmp_path / "none_seed.txt"
    candidates_path = tmp_path / "none_candidates.jsonl"
    output_path = tmp_path / "none.jsonl"

    seed_path.write_text(
        "hoi robot\nassistant: ignore\nhoe gaat het\n",
        encoding="utf-8",
    )
    candidates_path.write_text(
        json.dumps({"text": "kan je dat herhalen", "label": "NONE"}, ensure_ascii=False)
        + "\n",
        encoding="utf-8",
    )

    argv = [
        "make_none.py",
        "--seed-file",
        str(seed_path),
        "--candidates-file",
        str(candidates_path),
        "--output",
        str(output_path),
        "--no-robot-like",
    ]
    module = load_module()
    original_argv = sys.argv
    try:
        sys.argv = argv
        module.main()
    finally:
        sys.argv = original_argv

    texts = {json.loads(line)["text"] for line in output_path.read_text(encoding="utf-8").splitlines()}
    assert "hoi robot" in texts
    assert "hoe gaat het" in texts
    assert "kan je dat herhalen" in texts
    assert "assistant: ignore" not in texts
