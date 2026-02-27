from __future__ import annotations

import time
from types import SimpleNamespace

from dialog.interfaces import LLMResult, STTResult, UtteranceAudio

import webapp_server


class StubLLMBackend:
    def generate(self, messages):
        return LLMResult(reply="ok", messages=list(messages))


class StubSTTBackend:
    def transcribe(self, _audio):
        return STTResult(text="hallo", language="nl", confidence=1.0)


class _FakeContinuousMic:
    def __init__(self) -> None:
        self.calls = 0

    def capture_utterance(self, timeout_s=10.0):  # pragma: no cover - timing-sensitive path
        self.calls += 1
        if self.calls == 1:
            return UtteranceAudio(pcm=b"fake", sample_rate=16000, channels=1, sample_width=2)
        time.sleep(0.05)
        raise TimeoutError()


def _make_app(monkeypatch, *, fake_mic: _FakeContinuousMic | None = None):
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
    if fake_mic is not None:
        monkeypatch.setattr(webapp_server, "_make_mic_from_config", lambda *_a, **_k: fake_mic)
    app, _, _ = webapp_server.create_app(cfg={}, config_path="<memory>")
    return app


def test_dm_event_source_inference_for_send_manual_and_script(monkeypatch):
    app = _make_app(monkeypatch)
    client = app.test_client()
    client.post("/api/dm_events_clear", json={})

    assert client.post("/api/send", json={"text": "chat turn"}).status_code == 200
    assert client.post("/api/send", json={"text": "speech turn", "input_meta": {"stt_used": True}}).status_code == 200
    assert client.post("/api/command_execute", json={"label": "DANCE"}).status_code == 200
    assert client.post("/api/script/say", json={"text": "script says hi"}).status_code == 200

    events = client.get("/api/dm_events?limit=200").get_json()["events"]
    sources = [str(evt.get("source") or "") for evt in events]
    assert "chat_text" in sources
    assert "speech_ptt" in sources
    assert "manual_command_ui" in sources
    assert "script_api" in sources


def test_dm_event_source_continuous_worker_forced_source(monkeypatch):
    fake_mic = _FakeContinuousMic()
    app = _make_app(monkeypatch, fake_mic=fake_mic)
    client = app.test_client()
    client.post("/api/dm_events_clear", json={})

    start = client.post("/api/continuous_start", json={})
    assert start.status_code == 200
    deadline = time.time() + 2.0
    found = False
    while time.time() < deadline:
        events = client.get("/api/dm_events?event=dialog_reply&source=speech_continuous&limit=50").get_json()["events"]
        if events:
            found = True
            break
        time.sleep(0.05)
    client.post("/api/continuous_stop", json={})
    assert found is True
