from __future__ import annotations

from types import SimpleNamespace

from dialog.interfaces import LLMResult

import webapp_server


class SpyOutputBackend:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def emit(self, text: str) -> None:
        self.calls.append(text)


class StubLLMBackend:
    def __init__(self, reply: str) -> None:
        self._reply = reply

    def generate(self, messages):
        return LLMResult(reply=self._reply, messages=list(messages))


class StubSTTBackend:
    def transcribe(self, audio):  # pragma: no cover
        raise AssertionError("STT not used in these tests")


def _make_app_with_spy(monkeypatch, *, reply: str = "ok"):
    spy = SpyOutputBackend()
    base_pipeline = SimpleNamespace(
        llm=StubLLMBackend(reply),
        output=spy,
        status_to_console=True,
        system_prompt="SYSTEM",
        log_messages_path=None,
        log_meta={},
        max_history_turns=None,
    )

    monkeypatch.setattr(webapp_server, "build_pipeline_from_config", lambda *_a, **_k: base_pipeline)
    monkeypatch.setattr(webapp_server, "make_stt_backend_from_config", lambda *_a, **_k: StubSTTBackend())

    app, _, _ = webapp_server.create_app(cfg={}, config_path="<memory>")
    return app, spy


def test_web_send_emit_none_does_not_call_output(monkeypatch):
    app, spy = _make_app_with_spy(monkeypatch, reply="hi")
    client = app.test_client()

    resp = client.post("/api/send", json={"text": "hello"})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["emit_used"] == "none"
    assert spy.calls == []


def test_web_send_emit_pipeline_calls_output(monkeypatch):
    app, spy = _make_app_with_spy(monkeypatch, reply="hi")
    client = app.test_client()

    resp = client.post("/api/send", json={"text": "hello", "emit": "pipeline"})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["emit_used"] == "pipeline"
    assert spy.calls == ["hi"]

