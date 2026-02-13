from __future__ import annotations

import uuid
from pathlib import Path
from types import SimpleNamespace

from dialog.interfaces import LLMResult

import webapp_server


class StubLLMBackend:
    def generate(self, messages):
        return LLMResult(reply="ok", messages=list(messages))


class StubSTTBackend:
    def transcribe(self, audio):  # pragma: no cover
        raise AssertionError("STT not used in these tests")


def _make_app(monkeypatch, tmp_path: Path):
    prompts_dir = tmp_path / "master_prompts"
    prompts_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("DM_MASTER_PROMPTS_DIR", str(prompts_dir))
    base_pipeline = SimpleNamespace(
        llm=StubLLMBackend(),
        output=None,
        status_to_console=True,
        system_prompt="SYSTEM",
        log_messages_path=None,
        log_meta={},
        max_history_turns=None,
    )
    monkeypatch.setattr(webapp_server, "build_pipeline_from_config", lambda *_a, **_k: base_pipeline)
    monkeypatch.setattr(webapp_server, "make_stt_backend_from_config", lambda *_a, **_k: StubSTTBackend())
    app, _, _ = webapp_server.create_app(cfg={}, config_path="<memory>")
    return app, prompts_dir


def test_master_prompt_save_and_load_roundtrip(monkeypatch, tmp_path):
    app, prompts_dir = _make_app(monkeypatch, tmp_path)
    client = app.test_client()

    filename = f"test_prompt_{uuid.uuid4().hex}.txt"
    expected_name = f"master_prompts/{filename}"
    content = "Jij bent een testprompt.\nAntwoord kort."

    save_resp = client.post("/api/master_prompt_save", json={"save_as": filename, "content": content})
    assert save_resp.status_code == 200
    save_data = save_resp.get_json()
    assert save_data["ok"] is True
    assert save_data["name"] == expected_name
    assert expected_name in (save_data.get("prompts") or [])

    file_content = (prompts_dir / filename).read_text(encoding="utf-8")
    assert file_content == content

    load_resp = client.get("/api/master_prompt_content", query_string={"name": expected_name})
    assert load_resp.status_code == 200
    load_data = load_resp.get_json()
    assert load_data["ok"] is True
    assert load_data["name"] == expected_name
    assert load_data["content"] == content


def test_master_prompt_save_updates_existing_file(monkeypatch, tmp_path):
    app, _ = _make_app(monkeypatch, tmp_path)
    client = app.test_client()

    filename = f"test_prompt_{uuid.uuid4().hex}.txt"
    expected_name = f"master_prompts/{filename}"

    create_resp = client.post("/api/master_prompt_save", json={"save_as": filename, "content": "v1"})
    assert create_resp.status_code == 200
    assert create_resp.get_json()["ok"] is True

    update_resp = client.post(
        "/api/master_prompt_save",
        json={"name": expected_name, "content": "v2"},
    )
    assert update_resp.status_code == 200
    update_data = update_resp.get_json()
    assert update_data["ok"] is True
    assert update_data["name"] == expected_name

    load_resp = client.get("/api/master_prompt_content", query_string={"name": expected_name})
    assert load_resp.status_code == 200
    assert load_resp.get_json()["content"] == "v2"


def test_master_prompt_api_validation_errors(monkeypatch, tmp_path):
    app, _ = _make_app(monkeypatch, tmp_path)
    client = app.test_client()

    missing_name = f"master_prompts/not_found_{uuid.uuid4().hex}.txt"
    missing_resp = client.get("/api/master_prompt_content", query_string={"name": missing_name})
    assert missing_resp.status_code == 404
    assert missing_resp.get_json()["ok"] is False

    missing_target_resp = client.post("/api/master_prompt_save", json={"content": "x"})
    assert missing_target_resp.status_code == 400
    assert missing_target_resp.get_json()["ok"] is False

    missing_content_resp = client.post("/api/master_prompt_save", json={"save_as": "valid_name"})
    assert missing_content_resp.status_code == 400
    assert missing_content_resp.get_json()["ok"] is False


def test_runtime_override_master_prompt_text_takes_precedence():
    cfg_src = {
        "llm": {
            "type": "ollama_local",
            "params": {
                "model": "model-a",
                "system_prompt_file": "master_prompts/default.txt",
            },
        },
    }
    runtime_cfg = {
        "llm_type": "ollama_local",
        "llm_model": "model-b",
        "master_prompt_file": "master_prompts/short.txt",
        "master_prompt_text": "TEMP PROMPT",
    }

    merged = webapp_server._apply_runtime_overrides(cfg_src, runtime_cfg)
    params = merged["llm"]["params"]
    assert params["model"] == "model-b"
    assert params["system_prompt"] == "TEMP PROMPT"
    assert "system_prompt_file" not in params


def test_runtime_override_master_prompt_text_can_be_cleared():
    cfg_src = {
        "llm": {
            "type": "ollama_local",
            "params": {
                "model": "model-a",
                "system_prompt": "OLD TEMP",
                "system_prompt_file": "master_prompts/default.txt",
            },
        },
    }
    runtime_cfg = {
        "llm_type": "ollama_local",
        "llm_model": "model-a",
        "master_prompt_file": "master_prompts/short.txt",
        "master_prompt_text": "",
    }

    merged = webapp_server._apply_runtime_overrides(cfg_src, runtime_cfg)
    params = merged["llm"]["params"]
    assert "system_prompt" not in params
    assert params["system_prompt_file"] == "master_prompts/short.txt"
