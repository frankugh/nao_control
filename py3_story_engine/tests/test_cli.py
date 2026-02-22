from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

import storyteller.cli as cli
from storyteller.story_models import default_story_state
from storyteller.story_storage import save_autosave


def test_validate_config_defaults_and_prompt_files_exist(tmp_path):
    cfg = cli.validate_config_dict({}, source_dir=tmp_path)

    assert cfg["storyteller"]["type"] == "ollama_local"
    assert cfg["state_updater"]["type"] == "ollama_local"
    assert cfg["runtime"]["strict_json"] is True
    assert cfg["runtime"]["required_language"] == "nl"

    storyteller_prompt = Path(cfg["storyteller"]["prompt_file"])
    updater_prompt = Path(cfg["state_updater"]["prompt_file"])

    assert storyteller_prompt.is_file()
    assert updater_prompt.is_file()
    assert cli.load_prompt_text(str(storyteller_prompt), label="storyteller")
    assert cli.load_prompt_text(str(updater_prompt), label="state_updater")


def test_validate_config_rejects_unknown_keys(tmp_path):
    with pytest.raises(ValueError):
        cli.validate_config_dict({"bad": {}}, source_dir=tmp_path)
    with pytest.raises(ValueError):
        cli.validate_config_dict({"runtime": {"bad": 1}}, source_dir=tmp_path)
    with pytest.raises(ValueError):
        cli.validate_config_dict({"storyteller": {"bad": 1}}, source_dir=tmp_path)


def test_resolve_user_input_maps_choice_index():
    text, hint = cli.resolve_user_input("1", ["Vraag om vrede", "Val aan"])
    assert text == "Vraag om vrede"
    assert hint and "Keuze 1" in hint

    text2, hint2 = cli.resolve_user_input("2", ["Vraag om vrede", "Val aan"])
    assert text2 == "Val aan"
    assert hint2 and "Keuze 2" in hint2

    text3, hint3 = cli.resolve_user_input("vrije tekst", ["a", "b"])
    assert text3 == "vrije tekst"
    assert hint3 is None


def test_repl_debug_save_load_and_logging(monkeypatch, tmp_path, capsys):
    runtime_dir = tmp_path / "runtime" / "story"
    log_file = tmp_path / "turns.jsonl"

    monkeypatch.setattr(cli, "build_story_runtime_dir", lambda _root: str(runtime_dir))
    monkeypatch.setattr(cli, "_create_llm_backend", lambda _cfg: object())

    calls = []

    def fake_run_story_turn(**kwargs):
        calls.append(str(kwargs["user_input"]))
        state = copy.deepcopy(kwargs["state"])
        state["last_scene_summary"] = f"scene::{kwargs['user_input']}"
        return {
            "state": state,
            "scene": f"SCENE::{kwargs['user_input']}",
            "choices": ["Keuze A", "Keuze B"],
            "reply": "SCENE::reply",
            "patch": {"facts": {"add": [], "remove": []}},
            "beat_decision": {"requested_advance": "stay", "applied_advance": "stay"},
            "logs": {
                "state_updater": {"attempt_inputs": [{"messages": []}], "raw_output": "{}"},
                "storyteller": {"attempt_inputs": [{"messages": []}], "raw_output": "{}"},
            },
        }

    monkeypatch.setattr(cli, "run_story_turn", fake_run_story_turn)

    inputs = iter([
        "/debug on",
        "hallo",
        "1",
        "/save alpha",
        "/load alpha",
        "/history 1",
        "/debug off",
        "/quit",
    ])

    def fake_input(_prompt: str = "") -> str:
        try:
            return next(inputs)
        except StopIteration:
            return "/quit"

    monkeypatch.setattr("builtins.input", fake_input)

    rc = cli.main(["--session-id", "test_session", "--log-file", str(log_file)])
    assert rc == 0

    out = capsys.readouterr().out
    assert "Debug is nu: ON" in out
    assert "Save opgeslagen" in out
    assert "Save geladen" in out
    assert "Debug is nu: OFF" in out

    assert calls[0] == "hallo"
    assert calls[1] == "Keuze A"

    assert log_file.is_file()
    lines = [line for line in log_file.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first["user_input_raw"] == "hallo"
    second = json.loads(lines[1])
    assert second["user_input_raw"] == "1"
    assert second["user_input_resolved"] == "Keuze A"


def test_repl_loads_existing_autosave(monkeypatch, tmp_path, capsys):
    runtime_dir = tmp_path / "runtime" / "story"
    runtime_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(cli, "build_story_runtime_dir", lambda _root: str(runtime_dir))
    monkeypatch.setattr(cli, "_create_llm_backend", lambda _cfg: object())
    monkeypatch.setattr(
        cli,
        "run_story_turn",
        lambda **kwargs: {
            "state": kwargs["state"],
            "scene": "scene",
            "choices": ["A", "B"],
            "reply": "scene",
            "patch": None,
            "beat_decision": {"requested_advance": "stay", "applied_advance": "stay"},
            "logs": {},
        },
    )

    session_id = "resume_me"
    payload = {
        "state": default_story_state(),
        "history": [{"role": "user", "content": "hallo"}, {"role": "assistant", "content": "scene"}],
        "last_scene_text": "scene",
        "last_choices": ["A", "B"],
        "turn_idx": 1,
    }
    save_autosave(str(runtime_dir), instance_id=cli.INSTANCE_ID, sid=session_id, payload=payload)

    monkeypatch.setattr("builtins.input", lambda _prompt="": "/quit")

    rc = cli.main(["--session-id", session_id, "--no-log"])
    assert rc == 0

    out = capsys.readouterr().out
    assert "Autosave geladen" in out
