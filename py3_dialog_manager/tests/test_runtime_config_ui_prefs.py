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


def test_runtime_config_defaults_include_ui_preferences(monkeypatch):
    app = _make_app(monkeypatch)
    client = app.test_client()

    resp = client.get("/api/runtime_config")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    cfg = data["config"]
    assert cfg["listen_mode"] == "ptt"
    assert cfg["ui_active_tab"] == "prompt"


def test_runtime_config_ui_preferences_persist_and_are_cleaned(monkeypatch):
    app = _make_app(monkeypatch)
    client = app.test_client()

    resp = client.post(
        "/api/runtime_config",
        json={"listen_mode": "continuous", "ui_active_tab": "commands"},
    )
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["config"]["listen_mode"] == "continuous"
    assert data["config"]["ui_active_tab"] == "commands"

    effective = client.get("/api/runtime_effective").get_json()
    assert effective["ok"] is True
    assert effective["runtime_config"]["listen_mode"] == "continuous"
    assert effective["runtime_config"]["ui_active_tab"] == "commands"

    resp2 = client.post(
        "/api/runtime_config",
        json={"config": {"listen_mode": "invalid", "ui_active_tab": "invalid"}},
    )
    assert resp2.status_code == 200
    data2 = resp2.get_json()
    assert data2["ok"] is True
    assert data2["config"]["listen_mode"] == "ptt"
    assert data2["config"]["ui_active_tab"] == "prompt"
