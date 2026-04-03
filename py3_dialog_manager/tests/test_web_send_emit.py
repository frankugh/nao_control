from __future__ import annotations

import threading
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


class _Resp:
    def __init__(self, payload, status_code=200) -> None:
        self._payload = payload
        self.status_code = status_code
        self.ok = status_code < 400

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"status={self.status_code}")


def _make_app_with_spy(monkeypatch, *, reply: str = "ok", base_pipeline=None):
    spy = SpyOutputBackend()
    if base_pipeline is None:
        base_pipeline = SimpleNamespace(
            llm=StubLLMBackend(reply),
            output=spy,
            status_to_console=True,
            system_prompt="SYSTEM",
            log_messages_path=None,
            log_meta={},
            max_history_turns=None,
        )
    else:
        base_pipeline.output = spy

    monkeypatch.setattr(webapp_server, "build_pipeline_from_config", lambda *_a, **_k: base_pipeline)
    monkeypatch.setattr(webapp_server, "make_stt_backend_from_config", lambda *_a, **_k: StubSTTBackend())

    app, _, _ = webapp_server.create_app(
        cfg={"output": {"type": "console"}},
        config_path="<memory>",
    )
    return app, spy, base_pipeline


def test_web_send_emit_none_does_not_call_output(monkeypatch):
    app, spy, _ = _make_app_with_spy(monkeypatch, reply="hi")
    client = app.test_client()

    resp = client.post("/api/send", json={"text": "hello", "emit": "none"})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["emit_used"] == "none"
    assert spy.calls == []


def test_web_send_emit_pipeline_calls_output(monkeypatch):
    app, spy, _ = _make_app_with_spy(monkeypatch, reply="hi")
    client = app.test_client()

    resp = client.post("/api/send", json={"text": "hello", "emit": "pipeline"})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["emit_used"] == "pipeline"
    assert spy.calls == ["hi"]


def test_web_send_commits_history_before_pipeline_output_finishes(monkeypatch):
    output_started = threading.Event()
    output_release = threading.Event()

    class BlockingOutput:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def emit(self, text: str) -> None:
            self.calls.append(text)
            output_started.set()
            assert output_release.wait(timeout=2.0)

    base_pipeline = SimpleNamespace(
        llm=StubLLMBackend("hi"),
        output=BlockingOutput(),
        status_to_console=True,
        system_prompt="SYSTEM",
        log_messages_path=None,
        log_meta={},
        max_history_turns=None,
    )
    monkeypatch.setattr(webapp_server, "build_pipeline_from_config", lambda *_a, **_k: base_pipeline)
    monkeypatch.setattr(webapp_server, "make_stt_backend_from_config", lambda *_a, **_k: StubSTTBackend())

    app, _, _ = webapp_server.create_app(
        cfg={"output": {"type": "console"}},
        config_path="<memory>",
    )
    worker_client = app.test_client()
    observer_client = app.test_client()
    worker_client.set_cookie("sid", "send-early-history")
    observer_client.set_cookie("sid", "send-early-history")
    result = {}

    def _send() -> None:
        result["resp"] = worker_client.post("/api/send", json={"text": "hello", "emit": "pipeline"})

    thread = threading.Thread(target=_send, daemon=True)
    thread.start()
    assert output_started.wait(timeout=1.0)

    state_resp = observer_client.get("/api/state")
    assert state_resp.status_code == 200
    state = state_resp.get_json()
    assert state["ok"] is True
    history = state["history"]
    assert [item["role"] for item in history] == ["user", "assistant"]
    assert history[0]["content"] == "hello"
    assert history[1]["content"] == "hi"

    output_release.set()
    thread.join(timeout=2.0)
    assert result["resp"].status_code == 200


def test_api_state_history_version_is_stable_without_changes(monkeypatch):
    app, _spy, _ = _make_app_with_spy(monkeypatch, reply="hi")
    client = app.test_client()

    first = client.get("/api/state")
    assert first.status_code == 200
    first_data = first.get_json()
    assert first_data["ok"] is True
    v1 = int(first_data.get("history_version", -1))
    assert v1 >= 0

    second = client.get("/api/state")
    assert second.status_code == 200
    second_data = second.get_json()
    assert second_data["ok"] is True
    v2 = int(second_data.get("history_version", -1))
    assert v2 == v1


def test_api_state_history_version_increments_when_history_changes(monkeypatch):
    app, _spy, _ = _make_app_with_spy(monkeypatch, reply="hi")
    client = app.test_client()

    before = client.get("/api/state")
    assert before.status_code == 200
    v_before = int(before.get_json().get("history_version", -1))
    assert v_before >= 0

    send_resp = client.post("/api/send", json={"text": "hello", "emit": "none"})
    assert send_resp.status_code == 200
    assert send_resp.get_json()["ok"] is True

    after_send = client.get("/api/state")
    assert after_send.status_code == 200
    after_send_data = after_send.get_json()
    v_after_send = int(after_send_data.get("history_version", -1))
    assert v_after_send > v_before
    assert len(after_send_data.get("history") or []) == 2

    reset_resp = client.post("/api/reset")
    assert reset_resp.status_code == 200
    assert reset_resp.get_json()["ok"] is True

    after_reset = client.get("/api/state")
    assert after_reset.status_code == 200
    after_reset_data = after_reset.get_json()
    v_after_reset = int(after_reset_data.get("history_version", -1))
    assert v_after_reset > v_after_send
    assert (after_reset_data.get("history") or []) == []


def test_web_send_filters_dance_catalog_in_runtime_system_prompt(monkeypatch):
    class StubCmdrec:
        def route(self, text: str, mode: str, active_behavior):
            del text, mode, active_behavior
            return SimpleNamespace(is_command=False, command=None, reason="low_confidence", top3=[("NONE", 1.0)])

        def get_dance_catalog(self):
            return [
                {"key": "happy", "behavior": "dances/happy"},
                {"key": "funky", "behavior": "dances/funky"},
            ]

    cmdrec = StubCmdrec()
    base_pipeline = SimpleNamespace(
        llm=StubLLMBackend("hi"),
        output=SpyOutputBackend(),
        status_to_console=True,
        system_prompt="SYSTEM",
        system_prompt_base="SYSTEM",
        log_messages_path=None,
        log_meta={},
        max_history_turns=None,
        _cmdrec=cmdrec,
        _behavior_executor=None,
        _debug_cmdrec=False,
        runtime_context_enabled=True,
        _runtime_context_enabled=True,
        runtime_context_static={"available_commands": "", "dance_catalog": "- happy\n- funky"},
        _runtime_context_static={"available_commands": "", "dance_catalog": "- happy\n- funky"},
    )

    def get_system_prompt(*, last_action=None):
        del last_action
        dance_block = base_pipeline.runtime_context_static.get("dance_catalog", "")
        return f"SYSTEM\n{dance_block}".strip()

    base_pipeline.get_system_prompt = get_system_prompt

    def fake_get(url, timeout=0, **_kwargs):
        if url == "http://base:5000/ping":
            return _Resp({"status": "ok"})
        if url == "http://base:5000/list_behaviors":
            return _Resp({"status": "ok", "data": ["dances/happy"]})
        raise AssertionError(f"unexpected GET url: {url}")

    monkeypatch.setattr(webapp_server.requests, "get", fake_get)
    app, _spy, _ = _make_app_with_spy(monkeypatch, base_pipeline=base_pipeline)
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

    resp = client.post("/api/send", json={"text": "hallo", "emit": "none"})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert "- happy" in str(data.get("system_prompt") or "")
    assert "funky" not in str(data.get("system_prompt") or "")
