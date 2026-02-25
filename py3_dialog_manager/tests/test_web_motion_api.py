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


class _Resp:
    def __init__(self, payload, status_code=200, text="") -> None:
        self._payload = payload
        self.status_code = status_code
        self.ok = status_code < 400
        self.text = text
        self.content = b"{}"

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"status={self.status_code}")


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


def test_nao_move_toward_calls_expected_almotion_sequence(monkeypatch):
    calls = []

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        return _Resp({"status": "ok", "data": {"result": None}})

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

    resp = client.post(
        "/api/nao_move_toward",
        json={"x": 1.0, "y": 0.0, "theta": 0.0, "frequency": 0.2, "arms_enabled": True, "seq": 12},
    )
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["mode"] == "behavior"
    assert data["ignored"] is False
    assert data["seq"] == 12
    assert data["applied"]["collision"]["tangential_m"] == 0.1
    assert data["applied"]["collision"]["orthogonal_m"] == 0.4

    urls = [u for (u, _k) in calls]
    assert len(urls) == 5
    assert all(url == "http://behavior:5001/nao/naoqi/call" for url in urls)
    methods = [call_kwargs["json"]["method"] for (_u, call_kwargs) in calls]
    assert methods == [
        "setExternalCollisionProtectionEnabled",
        "setTangentialSecurityDistance",
        "setOrthogonalSecurityDistance",
        "setMoveArmsEnabled",
        "moveToward",
    ]


def test_nao_move_toward_falls_back_to_base_when_behavior_fails(monkeypatch):
    calls = []

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        if url.startswith("http://behavior:5001/nao/naoqi/call"):
            raise requests.RequestException("behavior unavailable")
        return _Resp({"status": "ok", "data": {"result": None}})

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

    resp = client.post("/api/nao_move_toward", json={"x": 0.0, "y": 0.0, "theta": 1.0, "seq": 1})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["mode"] == "base"

    behavior_calls = [u for (u, _k) in calls if u.startswith("http://behavior:5001/nao/naoqi/call")]
    base_calls = [u for (u, _k) in calls if u.startswith("http://base:5000/naoqi/call")]
    assert len(behavior_calls) >= 1
    assert len(base_calls) == 5


def test_nao_move_toward_rejects_invalid_payload(monkeypatch):
    app = _make_app(monkeypatch)
    client = app.test_client()

    resp = client.post("/api/nao_move_toward", json={"x": "bad", "y": 0.0, "theta": 0.0})
    assert resp.status_code == 400
    data = resp.get_json()
    assert data["ok"] is False


def test_nao_move_toward_ignores_stale_seq(monkeypatch):
    calls = []

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        return _Resp({"status": "ok", "data": {"result": None}})

    monkeypatch.setattr(webapp_server.requests, "post", fake_post)
    app = _make_app(monkeypatch)
    client = app.test_client()

    cfg_resp = client.post(
        "/api/runtime_config",
        json={"config": {"behavior_enabled": False, "base_enabled": True, "nao_base_url": "http://base:5000"}},
    )
    assert cfg_resp.status_code == 200

    first = client.post("/api/nao_move_toward", json={"x": 1.0, "y": 0.0, "theta": 0.0, "seq": 5})
    assert first.status_code == 200
    first_data = first.get_json()
    assert first_data["ok"] is True
    assert first_data["ignored"] is False
    assert len(calls) == 5

    second = client.post("/api/nao_move_toward", json={"x": 0.0, "y": 0.0, "theta": -1.0, "seq": 4})
    assert second.status_code == 200
    second_data = second.get_json()
    assert second_data["ok"] is True
    assert second_data["ignored"] is True
    assert second_data["mode"] == "stale"
    assert len(calls) == 5


def test_nao_stop_move_falls_back_to_base(monkeypatch):
    calls = []

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        if url == "http://behavior:5001/nao/stop_move":
            raise requests.RequestException("behavior unavailable")
        return _Resp({"status": "ok", "data": {}})

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

    resp = client.post("/api/nao_stop_move")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["mode"] == "base"
    assert any(u == "http://behavior:5001/nao/stop_move" for (u, _k) in calls)
    assert any(u == "http://base:5000/stop_move" for (u, _k) in calls)


def test_nao_move_toward_uses_runtime_defaults_for_frequency_and_arms(monkeypatch):
    calls = []

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        return _Resp({"status": "ok", "data": {"result": None}})

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
                "locomotion_frequency": 0.65,
                "locomotion_arms_enabled": False,
            }
        },
    )
    assert cfg_resp.status_code == 200

    resp = client.post("/api/nao_move_toward", json={"x": 1.0, "y": 0.0, "theta": 0.0})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["applied"]["frequency"] == 0.65
    assert data["applied"]["arms_enabled"] is False

    methods = [call_kwargs["json"]["method"] for (_u, call_kwargs) in calls]
    arms_idx = methods.index("setMoveArmsEnabled")
    move_idx = methods.index("moveToward")
    assert calls[arms_idx][1]["json"]["args"] == [False, False]
    assert calls[move_idx][1]["json"]["args"] == [1.0, 0.0, 0.0, [["Frequency", 0.65]]]
