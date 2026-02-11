from __future__ import annotations

from types import SimpleNamespace

from dialog.interfaces import LLMResult

import webapp_server


class StubLLMBackend:
    def generate(self, messages):
        return LLMResult(reply="ok", messages=list(messages))


class NoOpOutput:
    def emit(self, text: str) -> None:
        return None


class StubSTTBackend:
    def transcribe(self, audio):  # pragma: no cover
        raise AssertionError("STT not used in this test")


class FakeResponse:
    def __init__(self, status_code: int, payload: dict | None = None) -> None:
        self.status_code = status_code
        self.ok = status_code < 400
        self._payload = payload if payload is not None else {}

    def json(self):
        return self._payload


def _make_app(monkeypatch):
    base_pipeline = SimpleNamespace(
        llm=StubLLMBackend(),
        output=NoOpOutput(),
        status_to_console=True,
        system_prompt="SYSTEM",
        log_messages_path=None,
        log_meta={},
        max_history_turns=None,
    )
    monkeypatch.setattr(webapp_server, "build_pipeline_from_config", lambda *_a, **_k: base_pipeline)
    monkeypatch.setattr(webapp_server, "make_stt_backend_from_config", lambda *_a, **_k: StubSTTBackend())
    app, _, _ = webapp_server.create_app(
        cfg={
            "nao_connection": {
                "primary": {"base_url": "http://behavior.local/nao"},
                "fallback": {"base_url": "http://base.local"},
            },
            "output": {"type": "console"},
        },
        config_path="<memory>",
    )
    return app


def test_shutdown_rest_falls_back_to_base_when_behavior_rest_fails(monkeypatch):
    app = _make_app(monkeypatch)
    calls: list[str] = []

    monkeypatch.setattr(
        webapp_server.requests,
        "get",
        lambda *args, **kwargs: FakeResponse(200, {"status": "ok"}),
    )

    def fake_post(url, *args, **kwargs):
        calls.append(url)
        if url == "http://behavior.local/nao/rest":
            return FakeResponse(404, {"status": "error"})
        if url == "http://base.local/rest":
            return FakeResponse(200, {"status": "ok"})
        return FakeResponse(200, {"status": "ok"})

    monkeypatch.setattr(webapp_server.requests, "post", fake_post)

    app._shutdown_rest()

    assert calls == ["http://behavior.local/nao/rest", "http://base.local/rest"]
