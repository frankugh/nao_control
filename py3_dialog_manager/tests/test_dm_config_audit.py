from __future__ import annotations

from types import SimpleNamespace

from dialog.interfaces import LLMResult

import webapp_server


class StubLLMBackend:
    def generate(self, messages):
        return LLMResult(reply="ok", messages=list(messages))


class StubSTTBackend:
    def transcribe(self, _audio):  # pragma: no cover
        raise AssertionError("STT not used in this test")


def _make_app(monkeypatch):
    base_pipeline = SimpleNamespace(
        llm=StubLLMBackend(),
        output=SimpleNamespace(emit=lambda _text: None),
        status_to_console=True,
        system_prompt="SYSTEM",
        log_messages_path=None,
        log_meta={},
        max_history_turns=None,
        _behavior_executor=SimpleNamespace(execute=lambda _cmd: None),
        _cmdrec=None,
        _debug_cmdrec=False,
    )
    monkeypatch.setattr(webapp_server, "build_pipeline_from_config", lambda *_a, **_k: base_pipeline)
    monkeypatch.setattr(webapp_server, "make_stt_backend_from_config", lambda *_a, **_k: StubSTTBackend())
    app, _, _ = webapp_server.create_app(cfg={}, config_path="<memory>")
    return app


def _runtime_cfg_events(client):
    payload = client.get("/api/dm_events?event=runtime_config_changed&limit=200").get_json()
    return payload["events"]


def test_runtime_config_audit_diff_scope_and_noop(monkeypatch):
    app = _make_app(monkeypatch)
    client = app.test_client()
    client.post("/api/dm_events_clear", json={})

    set_robot = client.post("/api/runtime_config", json={"config": {"robot_name": "Alex"}})
    assert set_robot.status_code == 200
    events = _runtime_cfg_events(client)
    assert len(events) == 1
    first = events[-1]["data"]
    assert "robot_name" in first["changed_keys"]
    assert first["change_scope"] == "runtime"
    assert first["reason"] == "runtime_config_apply"
    assert "robot_name" in first["changes"]
    assert "old" in first["changes"]["robot_name"]
    assert "new" in first["changes"]["robot_name"]

    set_ui_pref = client.post("/api/runtime_config", json={"config": {"ui_active_tab": "logs"}})
    assert set_ui_pref.status_code == 200
    events = _runtime_cfg_events(client)
    assert len(events) == 2
    second = events[-1]["data"]
    assert second["change_scope"] == "ui_pref_only"
    assert second["reason"] == "runtime_config_apply"
    assert second["changed_keys"] == ["ui_active_tab"]

    no_op = client.post("/api/runtime_config", json={"config": {"ui_active_tab": "logs"}})
    assert no_op.status_code == 200
    events_after_noop = _runtime_cfg_events(client)
    assert len(events_after_noop) == 2


def test_runtime_config_audit_from_custom_life_endpoint(monkeypatch):
    app = _make_app(monkeypatch)
    client = app.test_client()
    client.post("/api/dm_events_clear", json={})

    resp = client.post("/api/nao_custom_life_set", json={"enabled": True})
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True

    events = _runtime_cfg_events(client)
    assert events
    reasons = [evt.get("data", {}).get("reason") for evt in events]
    assert "nao_custom_life_set" in reasons
