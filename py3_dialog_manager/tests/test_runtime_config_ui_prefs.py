from __future__ import annotations

import json
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


def _make_app(monkeypatch, cfg=None):
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
    app, _, _ = webapp_server.create_app(cfg=cfg or {}, config_path="<memory>")
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
    assert cfg["locomotion_frequency"] == 0.2
    assert cfg["locomotion_arms_enabled"] is True


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
    assert data2["config"]["locomotion_frequency"] == 0.2
    assert data2["config"]["locomotion_arms_enabled"] is True


def test_runtime_config_locomotion_values_are_clamped(monkeypatch):
    app = _make_app(monkeypatch)
    client = app.test_client()

    high_resp = client.post(
        "/api/runtime_config",
        json={"config": {"locomotion_frequency": 9.0, "locomotion_arms_enabled": False}},
    )
    assert high_resp.status_code == 200
    high_data = high_resp.get_json()
    assert high_data["ok"] is True
    assert high_data["config"]["locomotion_frequency"] == 1.0
    assert high_data["config"]["locomotion_arms_enabled"] is False

    low_resp = client.post(
        "/api/runtime_config",
        json={"config": {"locomotion_frequency": -1.0, "locomotion_arms_enabled": True}},
    )
    assert low_resp.status_code == 200
    low_data = low_resp.get_json()
    assert low_data["ok"] is True
    assert low_data["config"]["locomotion_frequency"] == 0.05
    assert low_data["config"]["locomotion_arms_enabled"] is True


def test_runtime_config_defaults_read_output_router_params(monkeypatch):
    cfg = {
        "output": {
            "type": "output_router",
            "params": {"target": "nao", "tts_engine": "piper", "server_tts_lead_silence_ms": 250},
        }
    }
    app = _make_app(monkeypatch, cfg=cfg)
    client = app.test_client()

    resp = client.get("/api/runtime_config")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    runtime = data["config"]
    assert runtime["output_target"] == "nao"
    assert runtime["tts_engine"] == "piper"
    assert runtime["server_tts_lead_silence_ms"] == 250


def test_runtime_config_robot_name_defaults_and_cleaning(monkeypatch):
    app = _make_app(monkeypatch)
    client = app.test_client()

    baseline = client.get("/api/runtime_config").get_json()
    assert baseline["ok"] is True
    assert baseline["config"]["robot_name"] == ""

    set_resp = client.post("/api/runtime_config", json={"config": {"robot_name": "  Alex  "}})
    assert set_resp.status_code == 200
    set_data = set_resp.get_json()
    assert set_data["ok"] is True
    assert set_data["config"]["robot_name"] == "Alex"

    clear_resp = client.post("/api/runtime_config", json={"config": {"robot_name": "   "}})
    assert clear_resp.status_code == 200
    clear_data = clear_resp.get_json()
    assert clear_data["ok"] is True
    assert clear_data["config"]["robot_name"] == ""


def test_runtime_config_rejects_urls_that_point_to_webapp_itself(monkeypatch):
    app = _make_app(monkeypatch)
    client = app.test_client()

    base_resp = client.post(
        "/api/runtime_config",
        base_url="http://127.0.0.1:5102",
        json={"config": {"nao_base_url": "http://127.0.0.1:5102"}},
    )
    assert base_resp.status_code == 400
    base_data = base_resp.get_json()
    assert base_data["ok"] is False
    assert base_data["field"] == "nao_base_url"

    behavior_resp = client.post(
        "/api/runtime_config",
        base_url="http://127.0.0.1:5102",
        json={"config": {"behavior_manager_url": "http://127.0.0.1:5102"}},
    )
    assert behavior_resp.status_code == 400
    behavior_data = behavior_resp.get_json()
    assert behavior_data["ok"] is False
    assert behavior_data["field"] == "behavior_manager_url"


def test_startup_preset_runtime_fields_survive_apply_extract_roundtrip():
    default_cfg = json.loads(
        (Path(__file__).resolve().parents[1] / "configs" / "default.json").read_text(encoding="utf-8")
    )
    preset_cfg = webapp_server._load_startup_agent_preset_config("virtuele_robot")

    merged_cfg = webapp_server._apply_runtime_overrides(default_cfg, preset_cfg)
    runtime_cfg = webapp_server._extract_runtime_config(merged_cfg)

    assert runtime_cfg["robot_name"] == "Virtuele robot"
    assert runtime_cfg["nao_ip_enabled"] is False
    assert runtime_cfg["nao_base_url"] == "http://127.0.0.1:5000"
    assert runtime_cfg["behavior_manager_url"] == "http://127.0.0.1:5001"
    assert runtime_cfg["base_enabled"] is False
    assert runtime_cfg["behavior_enabled"] is False
    assert runtime_cfg["base_autostart"] is False
    assert runtime_cfg["behavior_autostart"] is False
    assert runtime_cfg["output_target"] == "server"
    assert runtime_cfg["tts_engine"] == "azure"
