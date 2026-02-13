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


class StubExecutor:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def execute(self, cmd) -> None:
        self.calls.append(str(getattr(cmd, "label", "")))


class _Resp:
    def __init__(self, payload, status_code=200) -> None:
        self._payload = payload
        self.status_code = status_code
        self.ok = status_code < 400

    def json(self):
        return self._payload


def _make_app(monkeypatch, *, executor=None):
    base_pipeline = SimpleNamespace(
        llm=StubLLMBackend(),
        output=None,
        status_to_console=True,
        system_prompt="SYSTEM",
        log_messages_path=None,
        log_meta={},
        max_history_turns=None,
        _behavior_executor=executor,
        _cmdrec=None,
        _debug_cmdrec=False,
    )
    monkeypatch.setattr(webapp_server, "build_pipeline_from_config", lambda *_a, **_k: base_pipeline)
    monkeypatch.setattr(webapp_server, "make_stt_backend_from_config", lambda *_a, **_k: StubSTTBackend())
    app, _, _ = webapp_server.create_app(cfg={}, config_path="<memory>")
    return app


def test_nao_stop_audio_prefers_behavior_endpoint(monkeypatch):
    calls = []

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        return _Resp({"status": "ok", "data": {"actions": {"tts_stop_called": True}}})

    monkeypatch.setattr(webapp_server.requests, "post", fake_post)
    app = _make_app(monkeypatch)
    client = app.test_client()

    cfg_resp = client.post(
        "/api/runtime_config",
        json={
            "config": {
                "behavior_enabled": True,
                "behavior_manager_url": "http://behavior:5001",
                "base_enabled": True,
                "nao_base_url": "http://base:5000",
            }
        },
    )
    assert cfg_resp.status_code == 200

    resp = client.post("/api/nao_stop_audio")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["mode"] == "behavior"
    assert calls[-1][0] == "http://behavior:5001/nao/stop_audio"


def test_nao_stop_audio_uses_base_endpoint_when_behavior_disabled(monkeypatch):
    calls = []

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        return _Resp({"status": "ok", "data": {"actions": {"tts_stop_called": True}}})

    monkeypatch.setattr(webapp_server.requests, "post", fake_post)
    app = _make_app(monkeypatch)
    client = app.test_client()

    cfg_resp = client.post(
        "/api/runtime_config",
        json={
            "config": {
                "behavior_enabled": False,
                "behavior_manager_url": "http://behavior:5001",
                "base_enabled": True,
                "nao_base_url": "http://base:5000",
            }
        },
    )
    assert cfg_resp.status_code == 200

    resp = client.post("/api/nao_stop_audio")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["mode"] == "base"
    assert calls[-1][0] == "http://base:5000/stop_audio"


def test_command_execute_marks_dance_as_stoppable(monkeypatch):
    executor = StubExecutor()
    app = _make_app(monkeypatch, executor=executor)
    client = app.test_client()

    resp = client.post(
        "/api/command_execute",
        json={"label": "DANCE", "resolved": {"dance_key": "happy", "dance_behavior": "dances/happy"}},
    )
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["command_stop_available"] is True
    assert data["command_stop_label"] == "DANCE"
    assert executor.calls == ["DANCE"]


def test_command_execute_stop_stops_audio_and_clears_stop_state(monkeypatch):
    calls = []
    executor = StubExecutor()

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        return _Resp({"status": "ok", "data": {"actions": {"tts_stop_called": True}}})

    monkeypatch.setattr(webapp_server.requests, "post", fake_post)
    app = _make_app(monkeypatch, executor=executor)
    client = app.test_client()

    cfg_resp = client.post(
        "/api/runtime_config",
        json={
            "config": {
                "behavior_enabled": False,
                "base_enabled": True,
                "nao_base_url": "http://base:5000",
            }
        },
    )
    assert cfg_resp.status_code == 200

    # Seed a stoppable command first.
    dance_resp = client.post(
        "/api/command_execute",
        json={"label": "DANCE", "resolved": {"dance_key": "happy", "dance_behavior": "dances/happy"}},
    )
    assert dance_resp.status_code == 200
    assert dance_resp.get_json()["command_stop_available"] is True

    stop_resp = client.post("/api/command_execute", json={"label": "STOP"})
    assert stop_resp.status_code == 200
    stop_data = stop_resp.get_json()
    assert stop_data["ok"] is True
    assert stop_data["command_stop_available"] is False
    assert stop_data["command_stop_label"] is None
    assert executor.calls[-1] == "STOP"
    assert calls[-1][0] == "http://base:5000/stop_audio"
