from __future__ import annotations

from types import SimpleNamespace

from dialog.interfaces import LLMResult

import webapp_server


class StubLLMBackend:
    def generate(self, messages):
        return LLMResult(reply="ok", messages=list(messages))


class StubSTTBackend:
    def transcribe(self, audio):  # pragma: no cover
        raise AssertionError("STT not used in these tests")


class _Proc:
    def __init__(self, stdout: str) -> None:
        self.stdout = stdout
        self.stderr = ""


def _make_app(monkeypatch):
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
    return app


def test_ollama_cloud_endpoint_includes_gemini_preview_model(monkeypatch):
    app = _make_app(monkeypatch)
    client = app.test_client()

    monkeypatch.setattr(webapp_server.shutil, "which", lambda _cmd: "ollama")
    ollama_list_stdout = "\n".join(
        [
            "NAME ID SIZE MODIFIED",
            "gemma:2b abc 1GB now",
            "gpt-oss:120b-cloud def 2GB now",
            "gemini-3-flash-preview ghi 3GB now",
        ]
    )

    def _fake_run(cmd, stdout=None, stderr=None, text=None, check=None):
        assert cmd == ["ollama", "list"]
        return _Proc(ollama_list_stdout)

    monkeypatch.setattr(webapp_server.subprocess, "run", _fake_run)

    cloud_resp = client.get("/api/ollama_models_cloud")
    assert cloud_resp.status_code == 200
    cloud = cloud_resp.get_json()
    assert cloud["ok"] is True
    assert "gpt-oss:120b-cloud" in cloud["models"]
    assert "gemini-3-flash-preview" in cloud["models"]

    local_resp = client.get("/api/ollama_models_local")
    assert local_resp.status_code == 200
    local = local_resp.get_json()
    assert local["ok"] is True
    assert "gemma:2b" in local["models"]
    assert "gemini-3-flash-preview" not in local["models"]


def test_ollama_model_capabilities_endpoint_uses_gpt_oss_level_heuristic(monkeypatch):
    app = _make_app(monkeypatch)
    client = app.test_client()

    def _fake_show(_host, _api_key, _model):
        raise RuntimeError("show failed")

    monkeypatch.setattr(webapp_server, "_ollama_show_model_details", _fake_show)

    resp = client.get("/api/ollama_model_capabilities?llm_type=ollama_cloud&model=gpt-oss:120b-cloud")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["model"] == "gpt-oss:120b-cloud"
    assert data["supports_thinking"] is True
    assert data["thinking_mode_type"] == "levels"


def test_ollama_model_capabilities_endpoint_maps_show_thinking_to_boolean(monkeypatch):
    app = _make_app(monkeypatch)
    client = app.test_client()

    monkeypatch.setattr(
        webapp_server,
        "_ollama_show_model_details",
        lambda _host, _api_key, _model: {"capabilities": ["thinking", "vision"]},
    )

    resp = client.get("/api/ollama_model_capabilities?llm_type=ollama_cloud&model=gemini-3-flash-preview")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["model"] == "gemini-3-flash-preview"
    assert data["supports_thinking"] is True
    assert data["thinking_mode_type"] == "boolean"
    assert "thinking" in data["capabilities"]
