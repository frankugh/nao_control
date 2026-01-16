from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "tools" / "export_none_candidates.py"


def load_module():
    spec = importlib.util.spec_from_file_location("export_none_candidates", MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_export_none_candidates_dedupes(tmp_path: Path) -> None:
    input_path = tmp_path / "none_candidates.txt"
    output_path = tmp_path / "none_candidates.jsonl"
    input_path.write_text(
        "# comment\n\nhoi robot\nHoi robot\nhoi robot\n",
        encoding="utf-8",
    )

    argv = [
        "export_none_candidates.py",
        "--input",
        str(input_path),
        "--output",
        str(output_path),
    ]
    module = load_module()
    original_argv = sys.argv
    try:
        sys.argv = argv
        module.main()
    finally:
        sys.argv = original_argv

    lines = output_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["text"] == "hoi robot"
    assert payload["label"] == "NONE"
