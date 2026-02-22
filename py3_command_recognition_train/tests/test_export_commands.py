from __future__ import annotations

from pathlib import Path

from cmdrec.commands import normalize_key, parse_commands_markdown, relabel


def test_parse_and_relabel(tmp_path: Path) -> None:
    content = """
## BOX
- boks
- Boks

## WALK_FORWARD
- loop vooruit

## DANCE: Algemeen
- dans
"""
    path = tmp_path / "commands.md"
    path.write_text(content, encoding="utf-8")

    entries = parse_commands_markdown(path)
    relabeled = [relabel(entry) for entry in entries]

    assert len(relabeled) == 3
    labels = [entry["label"] for entry in relabeled]
    assert labels == ["BOX", "LOCOMOTION_REQUEST", "DANCE"]


def test_normalize_key_ignores_punctuation_and_symbol_only_differences() -> None:
    assert normalize_key("vertel eens iets") == normalize_key("vertel eens iets.")
    assert normalize_key("Hallo, Alex!") == normalize_key("Hallo Alex")
    assert normalize_key("box?") == normalize_key("box")
