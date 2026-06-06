from __future__ import annotations

from types import SimpleNamespace
import threading
import time

import requests

from dialog.interfaces import LLMResult

import webapp_server


class StubLLMBackend:
    def generate(self, messages):
        return LLMResult(reply="ok", messages=list(messages))


class StubSTTBackend:
    def __init__(self, texts=None) -> None:
        self._texts = list(texts or [])

    def transcribe(self, audio):
        text = self._texts.pop(0) if self._texts else ""
        return SimpleNamespace(text=text, language="nl", confidence=0.9)


class StubOutput:
    def __init__(self) -> None:
        self.emitted: list[str] = []
        self.preloaded: list[bytes] = []
        self.preloaded_result = True

    def emit(self, text: str) -> None:
        self.emitted.append(text)

    def emit_preloaded_wav_bytes(self, wav_bytes: bytes, *, filename: str = "preloaded.wav") -> bool:
        self.preloaded.append(bytes(wav_bytes))
        return self.preloaded_result


class StubExecutor:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def execute(self, cmd) -> None:
        self.calls.append(str(getattr(cmd, "label", "")))


class FailingExecutor:
    def __init__(self, message: str) -> None:
        self.message = str(message)
        self.calls: list[str] = []

    def execute(self, cmd) -> None:
        self.calls.append(str(getattr(cmd, "label", "")))
        raise RuntimeError(self.message)


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


def _make_app(monkeypatch, *, executor=None, cmdrec=None, output=None, stt_backend=None):
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
    monkeypatch.setattr(
        webapp_server,
        "make_stt_backend_from_config",
        lambda *_a, **_k: stt_backend if stt_backend is not None else StubSTTBackend(),
    )
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
    assert supports.get("do_modes") == [
        "command",
        "behavior_start",
        "behavior_stop",
        "dance",
    ]


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
    assert data == {"ok": True, "status": "accepted", "action": "say", "preloaded_audio": False}
    assert output.emitted == ["Welkom bij de workshop"]


def test_script_say_uses_preloaded_audio_when_provided(monkeypatch):
    output = StubOutput()
    app, _ = _make_app(monkeypatch, output=output)
    client = app.test_client()

    cfg_resp = client.post(
        "/api/runtime_config",
        json={"config": {"output_target": "server", "tts_engine": "piper"}},
    )
    assert cfg_resp.status_code == 200

    resp = client.post(
        "/api/script/say",
        json={
            "text": "Offline welkom",
            "preloaded_audio_b64": "UklGRg==",
            "preloaded_audio_format": "wav",
        },
    )
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["preloaded_audio"] is True
    assert output.preloaded == [b"RIFF"]
    assert output.emitted == []


def test_script_say_does_not_live_fallback_after_preloaded_playback_failure(monkeypatch):
    output = StubOutput()
    output.preloaded_result = False
    app, _ = _make_app(monkeypatch, output=output)
    client = app.test_client()

    cfg_resp = client.post(
        "/api/runtime_config",
        json={"config": {"output_target": "server", "tts_engine": "azure"}},
    )
    assert cfg_resp.status_code == 200

    resp = client.post(
        "/api/script/say",
        json={
            "text": "Deze tekst mag niet nogmaals live worden uitgesproken",
            "preloaded_audio_b64": "UklGRg==",
            "preloaded_audio_format": "wav",
        },
    )

    assert resp.status_code == 200
    assert output.preloaded == [b"RIFF"]
    assert output.emitted == []


def test_script_tts_profile_reports_renderable_engine(monkeypatch):
    app, _ = _make_app(monkeypatch)
    client = app.test_client()

    cfg_resp = client.post(
        "/api/runtime_config",
        json={"config": {"output_target": "server", "tts_engine": "azure", "azure_tts_voice": "nl-NL-ColetteNeural"}},
    )
    assert cfg_resp.status_code == 200

    resp = client.post("/api/script/tts_profile", json={})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["supported"] is True
    assert data["engine"] == "azure"
    assert data["fingerprint"]
    assert "Colette" in data["summary"]


def test_script_tts_profile_reports_nao_native_as_not_renderable(monkeypatch):
    app, _ = _make_app(monkeypatch)
    client = app.test_client()

    cfg_resp = client.post(
        "/api/runtime_config",
        json={"config": {"output_target": "nao", "tts_engine": "nao_native"}},
    )
    assert cfg_resp.status_code == 200

    resp = client.post("/api/script/tts_profile", json={})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["supported"] is False
    assert data["engine"] == "nao_native"
    assert data["reason"] == "not_renderable"


def test_script_tts_render_returns_wav_bytes(monkeypatch):
    monkeypatch.setattr(
        webapp_server.OutputRouterBackend,
        "render_wav_bytes",
        lambda self, text: b"RIFFfakewav",
    )
    app, _ = _make_app(monkeypatch)
    client = app.test_client()

    cfg_resp = client.post(
        "/api/runtime_config",
        json={"config": {"output_target": "server", "tts_engine": "azure", "azure_tts_voice": "nl-NL-ColetteNeural"}},
    )
    assert cfg_resp.status_code == 200

    resp = client.post("/api/script/tts_render", json={"text": "Render mij"})
    assert resp.status_code == 200
    assert resp.mimetype == "audio/wav"
    assert resp.data == b"RIFFfakewav"


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


def test_script_do_command_returns_executor_failure_and_logs_script_action_error(monkeypatch):
    executor = FailingExecutor("Behavior greetings/wave niet uitgevoerd: robot meldt rust/wake-state false.")
    app, _ = _make_app(monkeypatch, executor=executor, cmdrec=StubCmdrec())
    client = app.test_client()

    resp = client.post("/api/script/do", json={"mode": "command", "label": "WAVE"})
    assert resp.status_code == 400
    data = resp.get_json()
    assert data["ok"] is False
    assert "wake-state false" in data["error"]

    events = client.get("/api/dm_events?event=script_action_error&limit=10").get_json()["events"]
    assert any(evt.get("data", {}).get("mode") == "command" for evt in events)


def test_api_dance_catalog_filters_unavailable_behaviors(monkeypatch):
    def fake_get(url, timeout=0, **_kwargs):
        if url == "http://base:5000/ping":
            return _Resp({"status": "ok"})
        if url == "http://base:5000/list_behaviors":
            return _Resp({"status": "ok", "data": ["dances/happy"]})
        raise AssertionError(f"unexpected GET url: {url}")

    monkeypatch.setattr(webapp_server.requests, "get", fake_get)
    app, _ = _make_app(monkeypatch, executor=StubExecutor(), cmdrec=StubCmdrec())
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

    resp = client.get("/api/dance_catalog")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["dances"] == [{"key": "happy", "behavior": "dances/happy"}]


def test_script_do_dance_rejects_unavailable_behavior(monkeypatch):
    def fake_get(url, timeout=0, **_kwargs):
        if url == "http://base:5000/ping":
            return _Resp({"status": "ok"})
        if url == "http://base:5000/list_behaviors":
            return _Resp({"status": "ok", "data": ["dances/happy"]})
        raise AssertionError(f"unexpected GET url: {url}")

    monkeypatch.setattr(webapp_server.requests, "get", fake_get)
    app, _ = _make_app(monkeypatch, executor=StubExecutor(), cmdrec=StubCmdrec())
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

    resp = client.post("/api/script/do", json={"mode": "dance", "dance_key": "funky"})
    assert resp.status_code == 400
    data = resp.get_json()
    assert data["ok"] is False
    assert data["error"] == "dance_not_found"
    assert data["dance_key"] == "funky"


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


def test_script_do_behavior_modes_use_production_behavior_timeout(monkeypatch):
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
                "base_enabled": False,
            }
        },
    )
    assert cfg_resp.status_code == 200

    start_resp = client.post("/api/script/do", json={"mode": "behavior_start", "behavior": "walkwithme/walkwithme"})
    assert start_resp.status_code == 200

    stop_resp = client.post("/api/script/do", json={"mode": "behavior_stop", "behavior": "walkwithme/walkwithme"})
    assert stop_resp.status_code == 200

    behavior_calls = [kwargs for (url, kwargs) in calls if url.endswith("/nao/do_behavior") or url.endswith("/nao/stop_behavior")]
    assert behavior_calls
    assert all(kwargs.get("timeout") == 60.0 for kwargs in behavior_calls)


def test_script_do_behavior_start_surfaces_warning_payload_as_failure(monkeypatch):
    def fake_post(url, **kwargs):
        return _Resp({"status": "warning", "data": {"behavior": "walkwithme/walkwithme", "is_awake": False}})

    monkeypatch.setattr(webapp_server.requests, "post", fake_post)
    app, _ = _make_app(monkeypatch, executor=StubExecutor(), cmdrec=StubCmdrec())
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

    resp = client.post("/api/script/do", json={"mode": "behavior_start", "behavior": "walkwithme/walkwithme"})
    assert resp.status_code == 400
    data = resp.get_json()
    assert data["ok"] is False
    assert "wake-state false" in data["error"]


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


def test_script_do_legacy_summary_mode_returns_invalid_mode(monkeypatch):
    app, _ = _make_app(monkeypatch)
    client = app.test_client()
    resp = client.post("/api/script/do", json={"mode": "summary_publish"})
    assert resp.status_code == 400
    data = resp.get_json()
    assert data["ok"] is False
    assert data["error"] == "invalid_mode"
