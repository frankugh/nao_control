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


def test_runtime_health_flags_conflict_when_base_url_points_to_webapp(monkeypatch):
    app = _make_app(monkeypatch)
    client = app.test_client()

    get_calls = []

    def fake_get(url, timeout=0, **_kwargs):  # pragma: no cover
        get_calls.append((url, timeout))
        return _Resp({"status": "ok"})

    monkeypatch.setattr(webapp_server.requests, "get", fake_get)
    monkeypatch.setattr(webapp_server.socket, "create_connection", lambda *_a, **_k: _ConnOk())

    resp = client.post(
        "/api/runtime_health",
        base_url="http://127.0.0.1:5102",
        json={
            "nao_base_url": "http://127.0.0.1:5102",
            "behavior_manager_url": "http://behavior:5202",
            "base_enabled": True,
            "behavior_enabled": False,
            "nao_ip_enabled": False,
        },
    )
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["base"]["ping"] is False
    assert data["base"]["nao_ping"] is False
    assert data["base"]["conflict"] is True
    assert data["behavior"]["conflict"] is False
    assert get_calls == []
    assert any(
        issue["issue_key"] == "dm_local:base_conflict"
        and issue["issue_type"] == "dm_local"
        and issue["active"] is True
        for issue in data["connectivity_issues"]
    )


def test_runtime_health_reports_local_connectivity_issues(monkeypatch):
    app = _make_app(monkeypatch)
    client = app.test_client()

    def fake_get(_url, timeout=0, **_kwargs):
        return _Resp({"status": "error", "timeout": timeout}, status_code=503)

    monkeypatch.setattr(webapp_server.requests, "get", fake_get)
    monkeypatch.setattr(webapp_server.socket, "create_connection", lambda *_a, **_k: _ConnOk())

    resp = client.post(
        "/api/runtime_health",
        json={
            "nao_base_url": "http://base:5000",
            "behavior_enabled": False,
            "base_enabled": True,
            "nao_ip_enabled": False,
        },
    )
    assert resp.status_code == 200
    data = resp.get_json()
    issue = next(issue for issue in data["connectivity_issues"] if issue["issue_key"] == "dm_local:base_ping")
    assert issue["issue_type"] == "dm_local"
    assert issue["severity"] == "error"
    assert issue["message"] == "Base connector is niet bereikbaar."
    assert issue["source"] == "runtime_health.base_ping"
    assert issue["first_seen_at"]
    assert issue["active"] is True


def test_cloud_tts_connectivity_issue_sets_and_clears(monkeypatch):
    app = _make_app(monkeypatch)
    client = app.test_client()

    outcomes = [
        {"wav_bytes": None, "error": "azure offline"},
        {"wav_bytes": b"RIFF....WAVE", "error": ""},
    ]

    class FakeRouter:
        def __init__(self, *args, **kwargs) -> None:
            self._last_error_message = ""

        def describe_tts_profile(self):
            return {"supported": True, "engine": "azure"}

        def render_wav_bytes(self, _text):
            outcome = outcomes.pop(0)
            self._last_error_message = outcome["error"]
            return outcome["wav_bytes"]

        def last_error_message(self):
            return self._last_error_message

    monkeypatch.setattr(webapp_server, "OutputRouterBackend", FakeRouter)

    cfg_resp = client.post(
        "/api/runtime_config",
        json={
            "config": {
                "tts_engine": "azure",
                "output_target": "server",
                "base_enabled": False,
                "behavior_enabled": False,
                "nao_ip_enabled": False,
            }
        },
    )
    assert cfg_resp.status_code == 200

    fail_resp = client.post("/api/script/tts_render", json={"text": "Hallo"})
    assert fail_resp.status_code == 502
    fail_payload = fail_resp.get_json()
    assert fail_payload["error"] == "tts_render_failed"
    assert fail_payload["detail"] == "azure offline"

    health_resp = client.post(
        "/api/runtime_health",
        json={"base_enabled": False, "behavior_enabled": False, "nao_ip_enabled": False},
    )
    assert health_resp.status_code == 200
    health_payload = health_resp.get_json()
    issue = next(issue for issue in health_payload["connectivity_issues"] if issue["issue_key"] == "cloud:tts")
    assert issue["issue_type"] == "cloud"
    assert issue["message"] == "Cloud TTS mislukt: azure offline"
    assert issue["source"] == "tts.azure"
    assert issue["active"] is True

    ok_resp = client.post("/api/script/tts_render", json={"text": "Hallo"})
    assert ok_resp.status_code == 200
    assert ok_resp.data == b"RIFF....WAVE"

    recovered_resp = client.post(
        "/api/runtime_health",
        json={"base_enabled": False, "behavior_enabled": False, "nao_ip_enabled": False},
    )
    assert recovered_resp.status_code == 200
    recovered_payload = recovered_resp.get_json()
    issue = next(issue for issue in recovered_payload["connectivity_issues"] if issue["issue_key"] == "cloud:tts")
    assert issue["active"] is False
