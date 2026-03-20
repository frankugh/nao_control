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

    def emit(self, text: str) -> None:
        self.emitted.append(text)

    def emit_preloaded_wav_bytes(self, wav_bytes: bytes, *, filename: str = "preloaded.wav") -> bool:
        self.preloaded.append(bytes(wav_bytes))
        return True


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
        "summary_capture_start",
        "summary_capture_stop_and_draft",
        "summary_publish",
        "summary_cancel",
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


def test_script_do_summary_stop_without_start_returns_invalid_state(monkeypatch):
    app, _ = _make_app(monkeypatch)
    client = app.test_client()

    resp = client.post(
        "/api/script/do",
        json={"mode": "summary_capture_stop_and_draft", "input_prompt_template": "Transcript:\n{transcript}"},
    )
    assert resp.status_code == 400
    data = resp.get_json()
    assert data["ok"] is False
    assert data["error"] == "invalid_summary_state"


def test_script_do_summary_publish_without_draft_returns_error(monkeypatch):
    app, _ = _make_app(monkeypatch)
    client = app.test_client()

    resp = client.post("/api/script/do", json={"mode": "summary_publish"})
    assert resp.status_code == 400
    data = resp.get_json()
    assert data["ok"] is False
    assert data["error"] == "summary_draft_not_ready"


def test_script_do_summary_empty_transcript_includes_capture_detail(monkeypatch):
    stt = StubSTTBackend([])
    app, _ = _make_app(monkeypatch, stt_backend=stt)
    client = app.test_client()

    class FakeMic:
        def capture_utterance(self, timeout_s=10.0):
            raise RuntimeError("input stream unavailable")

    monkeypatch.setattr(webapp_server, "_make_mic_from_config", lambda *_a, **_k: FakeMic())

    start_resp = client.post("/api/script/do", json={"mode": "summary_capture_start"})
    assert start_resp.status_code == 200
    time.sleep(0.05)

    stop_resp = client.post(
        "/api/script/do",
        json={"mode": "summary_capture_stop_and_draft", "input_prompt_template": "Transcript:\n{transcript}"},
    )
    assert stop_resp.status_code == 400
    stop_data = stop_resp.get_json()
    assert stop_data["error"] == "summary_transcript_empty"
    assert "mic:" in str(stop_data.get("detail") or "")
    stats = stop_data.get("capture_stats") or {}
    assert int(stats.get("loops", 0)) >= 1
    assert int(stats.get("transcript_count", 0)) == 0


def test_script_do_summary_capture_to_draft_and_publish(monkeypatch):
    output = StubOutput()
    stt = StubSTTBackend(["Ik wil leren hoe robots beslissingen nemen."])
    app, _ = _make_app(monkeypatch, output=output, stt_backend=stt)
    client = app.test_client()
    cfg_resp = client.post(
        "/api/runtime_config",
        json={"config": {"output_target": "server", "tts_engine": "piper"}},
    )
    assert cfg_resp.status_code == 200

    class FakeMic:
        def __init__(self) -> None:
            self.calls = 0

        def capture_utterance(self, timeout_s=10.0):
            self.calls += 1
            if self.calls == 1:
                return SimpleNamespace(pcm=b"wav", sample_rate=16000, channels=1, sample_width=2)
            time.sleep(0.01)
            raise TimeoutError("no speech")

    seen_modes = []

    def _fake_make_mic(*_a, **kwargs):
        seen_modes.append(str(kwargs.get("mode") or ""))
        return FakeMic()

    monkeypatch.setattr(webapp_server, "_make_mic_from_config", _fake_make_mic)

    start_resp = client.post("/api/script/do", json={"mode": "summary_capture_start"})
    assert start_resp.status_code == 200
    assert start_resp.get_json()["phase"] == "capturing"
    assert seen_modes and seen_modes[0] == "continuous"

    time.sleep(0.05)

    stop_resp = client.post(
        "/api/script/do",
        json={
            "mode": "summary_capture_stop_and_draft",
            "input_prompt_template": "Transcript:\n{transcript}\n\nInstruction:\n{instruction}",
            "instruction": "Maak een compacte samenvatting voor de moderator.",
        },
    )
    assert stop_resp.status_code == 200
    stop_data = stop_resp.get_json()
    assert stop_data["mode"] == "summary_capture_stop_and_draft"
    assert stop_data["phase"] == "draft_ready"
    assert stop_data["draft"] == "ok"
    assert stop_data["transcript"] == ["Ik wil leren hoe robots beslissingen nemen."]

    state_resp = client.get("/api/state")
    assert state_resp.status_code == 200
    assert state_resp.get_json()["history"] == []

    publish_resp = client.post("/api/script/do", json={"mode": "summary_publish"})
    assert publish_resp.status_code == 200
    publish_data = publish_resp.get_json()
    assert publish_data["mode"] == "summary_publish"
    assert publish_data["phase"] == "idle"
    assert output.emitted == ["ok"]


def test_script_do_summary_capture_keeps_capturing_while_stt_busy(monkeypatch):
    stt_started = threading.Event()

    class SlowSTTBackend:
        def __init__(self) -> None:
            self.calls = 0

        def transcribe(self, _audio):
            self.calls += 1
            if self.calls == 1:
                stt_started.set()
                time.sleep(0.2)
                return SimpleNamespace(text="eerste zin", language="nl", confidence=0.9)
            return SimpleNamespace(text="tweede zin", language="nl", confidence=0.9)

    stt = SlowSTTBackend()
    app, _ = _make_app(monkeypatch, stt_backend=stt)
    client = app.test_client()

    class FakeMic:
        def __init__(self) -> None:
            self.calls = 0

        def capture_utterance(self, timeout_s=10.0):
            self.calls += 1
            if self.calls <= 2:
                return SimpleNamespace(pcm=b"wav", sample_rate=16000, channels=1, sample_width=2)
            time.sleep(0.01)
            raise TimeoutError("no speech")

    monkeypatch.setattr(webapp_server, "_make_mic_from_config", lambda *_a, **_k: FakeMic())

    start_resp = client.post("/api/script/do", json={"mode": "summary_capture_start"})
    assert start_resp.status_code == 200
    assert stt_started.wait(timeout=1.0)
    time.sleep(0.05)

    poll_resp = client.post("/api/script/do", json={"mode": "summary_capture_start"})
    assert poll_resp.status_code == 200
    poll_data = poll_resp.get_json()
    stats = poll_data.get("capture_stats") or {}
    assert int(stats.get("audio_chunks", 0)) >= 2
    assert int(stats.get("stt_calls", 0)) >= 1

    cancel_resp = client.post("/api/script/do", json={"mode": "summary_cancel"})
    assert cancel_resp.status_code == 200


def test_script_do_summary_capture_applies_relaxed_vad_floor(monkeypatch):
    app, _ = _make_app(monkeypatch)
    client = app.test_client()
    seen: dict[str, object] = {}

    class FakeMic:
        def capture_utterance(self, timeout_s=10.0):
            time.sleep(0.01)
            raise TimeoutError("no speech")

    def _fake_make_mic(cfg, **kwargs):
        seen["cfg"] = cfg
        seen["mode"] = kwargs.get("mode")
        return FakeMic()

    monkeypatch.setattr(webapp_server, "_make_mic_from_config", _fake_make_mic)

    start_resp = client.post("/api/script/do", json={"mode": "summary_capture_start"})
    assert start_resp.status_code == 200
    assert seen.get("mode") == "continuous"

    cfg = seen.get("cfg") or {}
    mic_cfg = (cfg.get("input") or {}).get("mic") or {}
    cont = mic_cfg.get("params_continuous") or {}
    assert int(cont.get("stop_silence_ms", 0)) >= int(webapp_server._SUMMARY_CAPTURE_MIN_STOP_SILENCE_MS)
    assert int(cont.get("pre_roll_ms", 0)) >= int(webapp_server._SUMMARY_CAPTURE_MIN_PRE_ROLL_MS)

    cancel_resp = client.post("/api/script/do", json={"mode": "summary_cancel"})
    assert cancel_resp.status_code == 200


def test_script_do_summary_cancel_clears_draft(monkeypatch):
    output = StubOutput()
    stt = StubSTTBackend(["Ik wil meer leren over AI-toepassingen."])
    app, _ = _make_app(monkeypatch, output=output, stt_backend=stt)
    client = app.test_client()

    class FakeMic:
        def __init__(self) -> None:
            self.calls = 0

        def capture_utterance(self, timeout_s=10.0):
            self.calls += 1
            if self.calls == 1:
                return SimpleNamespace(pcm=b"wav", sample_rate=16000, channels=1, sample_width=2)
            time.sleep(0.01)
            raise TimeoutError("no speech")

    monkeypatch.setattr(webapp_server, "_make_mic_from_config", lambda *_a, **_k: FakeMic())

    assert client.post("/api/script/do", json={"mode": "summary_capture_start"}).status_code == 200
    time.sleep(0.05)
    stop_resp = client.post(
        "/api/script/do",
        json={"mode": "summary_capture_stop_and_draft", "input_prompt_template": "Transcript:\n{transcript}"},
    )
    assert stop_resp.status_code == 200

    cancel_resp = client.post("/api/script/do", json={"mode": "summary_cancel"})
    assert cancel_resp.status_code == 200
    cancel_data = cancel_resp.get_json()
    assert cancel_data["mode"] == "summary_cancel"
    assert cancel_data["phase"] == "idle"

    publish_resp = client.post("/api/script/do", json={"mode": "summary_publish"})
    assert publish_resp.status_code == 400
    assert publish_resp.get_json()["error"] == "summary_draft_not_ready"
    assert output.emitted == []
