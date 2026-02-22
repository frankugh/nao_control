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


class _Resp:
    def __init__(self, payload, status_code=200) -> None:
        self._payload = payload
        self.status_code = status_code
        self.ok = status_code < 400

    def json(self):
        return self._payload


class _ConnOk:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


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


def test_runtime_health_skips_is_awake_when_nao_disabled(monkeypatch):
    app = _make_app(monkeypatch)
    client = app.test_client()

    get_calls = []

    def fake_get(url, timeout=0, **_kwargs):
        get_calls.append((url, timeout))
        return _Resp({"status": "ok"})

    def fake_create_connection(_addr, timeout=0, **_kwargs):  # pragma: no cover
        raise AssertionError("TCP probe must not run when NAO is disabled")

    monkeypatch.setattr(webapp_server.requests, "get", fake_get)
    monkeypatch.setattr(webapp_server.socket, "create_connection", fake_create_connection)

    resp = client.post(
        "/api/runtime_health",
        json={
            "nao_base_url": "http://base:5000",
            "behavior_manager_url": "http://behavior:5001",
            "base_enabled": True,
            "behavior_enabled": True,
            "nao_ip_enabled": False,
            "nao_ip": "192.168.1.50",
        },
    )
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["nao"]["ping"] is False
    assert data["base"]["ping"] is True
    assert data["behavior"]["ping"] is True
    assert data["base"]["nao_ping"] is False
    assert data["behavior"]["nao_ping"] is False
    assert [u for (u, _t) in get_calls] == [
        "http://base:5000/ping",
        "http://behavior:5001/ping",
    ]


def test_runtime_health_checks_is_awake_when_nao_enabled_and_tcp_up(monkeypatch):
    app = _make_app(monkeypatch)
    client = app.test_client()

    get_calls = []
    tcp_calls = []

    def fake_get(url, timeout=0, **_kwargs):
        get_calls.append((url, timeout))
        return _Resp({"status": "ok"})

    def fake_create_connection(addr, timeout=0, **_kwargs):
        tcp_calls.append((addr, timeout))
        return _ConnOk()

    monkeypatch.setattr(webapp_server.requests, "get", fake_get)
    monkeypatch.setattr(webapp_server.socket, "create_connection", fake_create_connection)

    resp = client.post(
        "/api/runtime_health",
        json={
            "nao_base_url": "http://base:5000",
            "behavior_manager_url": "http://behavior:5001",
            "base_enabled": True,
            "behavior_enabled": True,
            "nao_ip_enabled": True,
            "nao_ip": "192.168.1.50",
        },
    )
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["nao"]["ping"] is True
    assert data["base"]["ping"] is True
    assert data["behavior"]["ping"] is True
    assert data["base"]["nao_ping"] is True
    assert data["behavior"]["nao_ping"] is True
    assert [u for (u, _t) in get_calls] == [
        "http://base:5000/ping",
        "http://base:5000/is_awake",
        "http://behavior:5001/ping",
        "http://behavior:5001/nao/is_awake",
    ]
    assert tcp_calls == [(("192.168.1.50", 9559), 2.5)]


def test_runtime_health_skips_is_awake_when_nao_enabled_but_tcp_down(monkeypatch):
    app = _make_app(monkeypatch)
    client = app.test_client()

    get_calls = []

    def fake_get(url, timeout=0, **_kwargs):
        get_calls.append((url, timeout))
        return _Resp({"status": "ok"})

    def fake_create_connection(_addr, timeout=0, **_kwargs):
        raise OSError("no route")

    monkeypatch.setattr(webapp_server.requests, "get", fake_get)
    monkeypatch.setattr(webapp_server.socket, "create_connection", fake_create_connection)

    resp = client.post(
        "/api/runtime_health",
        json={
            "nao_base_url": "http://base:5000",
            "behavior_manager_url": "http://behavior:5001",
            "base_enabled": True,
            "behavior_enabled": True,
            "nao_ip_enabled": True,
            "nao_ip": "192.168.1.50",
        },
    )
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["nao"]["ping"] is False
    assert data["base"]["ping"] is True
    assert data["behavior"]["ping"] is True
    assert data["base"]["nao_ping"] is False
    assert data["behavior"]["nao_ping"] is False
    assert [u for (u, _t) in get_calls] == [
        "http://base:5000/ping",
        "http://behavior:5001/ping",
    ]


def test_runtime_health_respects_base_and_behavior_enable_flags(monkeypatch):
    app = _make_app(monkeypatch)
    client = app.test_client()

    get_calls = []

    def fake_get(url, timeout=0, **_kwargs):
        get_calls.append((url, timeout))
        return _Resp({"status": "ok"})

    monkeypatch.setattr(webapp_server.requests, "get", fake_get)
    monkeypatch.setattr(webapp_server.socket, "create_connection", lambda *_a, **_k: _ConnOk())

    resp = client.post(
        "/api/runtime_health",
        json={
            "nao_base_url": "http://base:5000",
            "behavior_manager_url": "http://behavior:5001",
            "base_enabled": False,
            "behavior_enabled": True,
            "nao_ip_enabled": True,
            "nao_ip": "192.168.1.50",
        },
    )
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["nao"]["ping"] is True
    assert data["base"]["ping"] is False
    assert data["base"]["nao_ping"] is False
    assert data["behavior"]["ping"] is True
    assert data["behavior"]["nao_ping"] is True
    assert [u for (u, _t) in get_calls] == [
        "http://behavior:5001/ping",
        "http://behavior:5001/nao/is_awake",
    ]
