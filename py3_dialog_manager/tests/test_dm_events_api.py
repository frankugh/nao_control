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


def test_dm_events_api_filter_limit_meta_and_clear(monkeypatch):
    app = _make_app(monkeypatch)
    client = app.test_client()

    clear_resp = client.post("/api/dm_events_clear", json={})
    assert clear_resp.status_code == 200
    assert clear_resp.get_json()["ok"] is True

    send_chat = client.post("/api/send", json={"text": "Hallo zonder STT"})
    assert send_chat.status_code == 200
    send_stt = client.post("/api/send", json={"text": "Hallo met STT", "input_meta": {"stt_used": True}})
    assert send_stt.status_code == 200

    filtered = client.get("/api/dm_events?event=dialog_reply&source=speech_ptt&limit=1")
    assert filtered.status_code == 200
    payload = filtered.get_json()
    assert payload["ok"] is True
    assert len(payload["events"]) <= 1
    assert all(evt["event"] == "dialog_reply" for evt in payload["events"])
    assert all(evt["source"] == "speech_ptt" for evt in payload["events"])

    meta = payload["meta"]
    assert "info" in meta["available_levels"]
    assert "dialog_reply" in meta["available_events"]
    assert "chat_text" in meta["available_sources"]
    assert "speech_ptt" in meta["available_sources"]

    clear_resp_2 = client.post("/api/dm_events_clear", json={})
    assert clear_resp_2.status_code == 200
    empty_after_clear = client.get("/api/dm_events?limit=10").get_json()
    assert empty_after_clear["ok"] is True
    assert empty_after_clear["events"] == []


def test_dm_events_http_error_has_structured_fields(monkeypatch):
    app = _make_app(monkeypatch)
    client = app.test_client()
    client.post("/api/dm_events_clear", json={})

    bad = client.post("/api/command_execute", json={})
    assert bad.status_code == 400

    events = client.get("/api/dm_events?event=http_error_response&limit=20").get_json()["events"]
    assert events
    target = None
    for evt in events:
        data = evt.get("data") or {}
        if data.get("endpoint") == "/api/command_execute":
            target = data
            break
    assert target is not None
    assert "type" in target
    assert "message" in target
    assert "endpoint" in target
    assert "status" in target
    assert "latency_ms" in target
    assert "correlation" in target
