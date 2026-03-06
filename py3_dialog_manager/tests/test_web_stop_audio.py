from __future__ import annotations

from types import SimpleNamespace

import requests

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


class StubCmdrec:
    def __init__(self, labels):
        self._labels = list(labels)

    def get_labels(self):
        return list(self._labels)


class _Resp:
    def __init__(self, payload, status_code=200) -> None:
        self._payload = payload
        self.status_code = status_code
        self.ok = status_code < 400

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"status={self.status_code}")


def _make_app(monkeypatch, *, executor=None, cmdrec=None):
    base_pipeline = SimpleNamespace(
        llm=StubLLMBackend(),
        output=None,
        status_to_console=True,
        system_prompt="SYSTEM",
        log_messages_path=None,
        log_meta={},
        max_history_turns=None,
        _behavior_executor=executor,
        _cmdrec=cmdrec,
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


def test_command_execute_dance_is_marked_as_stoppable(monkeypatch):
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
    walk_resp = client.post(
        "/api/command_execute",
        json={"label": "WALK_WITH_ME"},
    )
    assert walk_resp.status_code == 200
    assert walk_resp.get_json()["command_stop_available"] is True
    assert walk_resp.get_json()["command_stop_label"] == "WALK_WITH_ME"

    stop_resp = client.post("/api/command_execute", json={"label": "STOP"})
    assert stop_resp.status_code == 200
    stop_data = stop_resp.get_json()
    assert stop_data["ok"] is True
    assert stop_data["command_stop_available"] is False
    assert stop_data["command_stop_label"] is None
    assert executor.calls[-1] == "STOP"
    assert calls[-1][0] == "http://base:5000/stop_audio"


def test_nao_wake_up_timeout_returns_pending(monkeypatch):
    def fake_post(url, **kwargs):
        raise requests.exceptions.ReadTimeout("HTTPConnectionPool(host='127.0.0.1', port=5000): Read timed out. (read timeout=3.0)")

    monkeypatch.setattr(webapp_server.requests, "post", fake_post)
    app = _make_app(monkeypatch)
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

    resp = client.post("/api/nao_wake_up")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["pending"] is True
    assert data["is_awake"] is None


def test_command_catalog_excludes_rest(monkeypatch):
    cmdrec = StubCmdrec(["REST", "DANCE", "WALK_WITH_ME", "LOCOMOTION_REQUEST"])
    app = _make_app(monkeypatch, cmdrec=cmdrec)
    client = app.test_client()

    resp = client.get("/api/command_catalog")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    labels = [item["label"] for item in data.get("commands", [])]
    assert "DANCE" in labels
    assert "REST" not in labels
    assert "WALK_WITH_ME" not in labels
    assert "LOCOMOTION_REQUEST" not in labels


def test_nao_custom_life_set_uses_custom_life_path_and_updates_runtime(monkeypatch):
    post_calls = []

    def fake_post(url, **kwargs):
        post_calls.append((url, kwargs))
        if url.endswith("/custom_life_apply"):
            return _Resp({"status": "ok", "data": {"prev_state": {"life_state": "solitary"}}})
        return _Resp({"status": "ok", "data": {}})

    def fake_get(url, **kwargs):
        if url.endswith("/custom_life_state"):
            return _Resp(
                {
                    "status": "ok",
                    "data": {
                        "modules": {
                            "basic_awareness": True,
                            "background_movement": False,
                            "breathing": True,
                        },
                        "life_state": "disabled",
                        "is_awake": True,
                    },
                }
            )
        return _Resp({"status": "ok"})

    monkeypatch.setattr(webapp_server.requests, "post", fake_post)
    monkeypatch.setattr(webapp_server.requests, "get", fake_get)
    app = _make_app(monkeypatch)
    client = app.test_client()

    cfg_resp = client.post(
        "/api/runtime_config",
        json={
            "config": {
                "behavior_enabled": False,
                "base_enabled": True,
                "nao_base_url": "http://base:5000",
                "custom_life_enabled": False,
            }
        },
    )
    assert cfg_resp.status_code == 200

    resp = client.post("/api/nao_custom_life_set", json={"enabled": True})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["enabled"] is True
    assert data["active_modules"] is True
    assert any(url.endswith("/custom_life_apply") for url, _ in post_calls)

    runtime_resp = client.get("/api/runtime_config")
    runtime_data = runtime_resp.get_json()
    assert runtime_data["ok"] is True
    assert runtime_data["config"]["custom_life_enabled"] is True
