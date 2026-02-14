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


class StubOutput:
    def __init__(self) -> None:
        self.emitted: list[str] = []

    def emit(self, text: str) -> None:
        self.emitted.append(text)


class StubExecutor:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def execute(self, cmd) -> None:
        self.calls.append(str(getattr(cmd, "label", "")))


class StubCmdrec:
    def __init__(self) -> None:
        self._labels = ["STOP", "DANCE", "WALK_WITH_ME"]
        self._dances = [
            {"key": "happy", "behavior": "dances/happy"},
            {"key": "funky", "behavior": "dances/funky"},
        ]

    def get_labels(self):
        return list(self._labels)

    def get_dance_catalog(self):
        return list(self._dances)


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


def _make_app(monkeypatch, *, executor=None, cmdrec=None, output=None):
    base_pipeline = SimpleNamespace(
        llm=StubLLMBackend(),
        output=output if output is not None else StubOutput(),
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
    return app, base_pipeline


def test_script_capabilities_contract(monkeypatch):
    app, _ = _make_app(monkeypatch)
    client = app.test_client()

    resp = client.get("/api/script/capabilities")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    supports = data.get("supports", {})
    assert supports.get("say") is True
    assert supports.get("dance_catalog") is True
    assert supports.get("do_modes") == ["command", "behavior_start", "behavior_stop", "dance"]


def test_script_say_requires_text(monkeypatch):
    app, _ = _make_app(monkeypatch)
    client = app.test_client()

    resp = client.post("/api/script/say", json={})
    assert resp.status_code == 400
    assert resp.get_json()["ok"] is False


def test_script_say_uses_pipeline_output_when_enabled(monkeypatch):
    output = StubOutput()
    app, _ = _make_app(monkeypatch, output=output)
    client = app.test_client()

    cfg_resp = client.post(
        "/api/runtime_config",
        json={"config": {"output_target": "server", "tts_engine": "piper"}},
    )
    assert cfg_resp.status_code == 200

    resp = client.post("/api/script/say", json={"text": "Welkom bij de workshop"})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data == {"ok": True, "status": "accepted", "action": "say"}
    assert output.emitted == ["Welkom bij de workshop"]


def test_script_do_command_stop_uses_existing_stop_logic(monkeypatch):
    calls = []
    executor = StubExecutor()

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        return _Resp({"status": "ok", "data": {"actions": {"tts_stop_called": True}}})

    monkeypatch.setattr(webapp_server.requests, "post", fake_post)
    app, _ = _make_app(monkeypatch, executor=executor, cmdrec=StubCmdrec())
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

    resp = client.post("/api/script/do", json={"mode": "command", "label": "STOP"})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["status"] == "accepted"
    assert data["mode"] == "command"
    assert executor.calls[-1] == "STOP"
    assert calls[-1][0] == "http://base:5000/stop_audio"


def test_script_do_dance_resolves_by_key(monkeypatch):
    executor = StubExecutor()
    app, _ = _make_app(monkeypatch, executor=executor, cmdrec=StubCmdrec())
    client = app.test_client()

    resp = client.post("/api/script/do", json={"mode": "dance", "dance_key": "HAPPY"})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["status"] == "accepted"
    assert data["mode"] == "dance"
    assert executor.calls[-1] == "DANCE"


def test_script_do_dance_unknown_key_returns_dance_not_found(monkeypatch):
    app, _ = _make_app(monkeypatch, executor=StubExecutor(), cmdrec=StubCmdrec())
    client = app.test_client()

    resp = client.post("/api/script/do", json={"mode": "dance", "dance_key": "does_not_exist"})
    assert resp.status_code == 400
    data = resp.get_json()
    assert data["ok"] is False
    assert data["error"] == "dance_not_found"


def test_script_do_behavior_modes_reuse_behavior_endpoints(monkeypatch):
    calls = []

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        return _Resp({"status": "ok", "data": {}})

    monkeypatch.setattr(webapp_server.requests, "post", fake_post)
    app, _ = _make_app(monkeypatch, executor=StubExecutor(), cmdrec=StubCmdrec())
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

    start_resp = client.post("/api/script/do", json={"mode": "behavior_start", "behavior": "walkwithme/walkwithme"})
    assert start_resp.status_code == 200
    assert start_resp.get_json()["ok"] is True

    stop_resp = client.post("/api/script/do", json={"mode": "behavior_stop", "behavior": "walkwithme/walkwithme"})
    assert stop_resp.status_code == 200
    assert stop_resp.get_json()["ok"] is True

    urls = [u for (u, _k) in calls]
    assert any(url.endswith("/nao/do_behavior") for url in urls)
    assert any(url.endswith("/nao/stop_behavior") for url in urls)


def test_script_do_command_builds_runtime_pipeline_for_new_sid(monkeypatch):
    executors = []

    class Exec:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def execute(self, cmd) -> None:
            self.calls.append(str(getattr(cmd, "label", "")))

    def fake_build_pipeline(cfg, **_kwargs):
        backend = str((cfg or {}).get("behavior_backend") or "")
        executor = Exec() if backend == "nao" else None
        if executor is not None:
            executors.append(executor)
        return SimpleNamespace(
            llm=StubLLMBackend(),
            output=StubOutput(),
            status_to_console=True,
            system_prompt="SYSTEM",
            log_messages_path=None,
            log_meta={},
            max_history_turns=None,
            _behavior_executor=executor,
            _cmdrec=StubCmdrec(),
            _debug_cmdrec=False,
        )

    monkeypatch.setattr(webapp_server, "build_pipeline_from_config", fake_build_pipeline)
    monkeypatch.setattr(webapp_server, "make_stt_backend_from_config", lambda *_a, **_k: StubSTTBackend())

    app, _, _ = webapp_server.create_app(
        cfg={"behavior_backend": "print", "base_enabled": True},
        config_path="<memory>",
    )
    client = app.test_client()

    resp = client.post("/api/script/do", json={"mode": "command", "label": "WAVE"})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert executors, "expected runtime pipeline rebuild with nao backend executor"
    assert any("WAVE" in ex.calls for ex in executors)
