from __future__ import annotations

from types import SimpleNamespace

from dialog.interfaces import LLMResult

import pytest
import webapp_server


class StubLLMBackend:
    def generate(self, messages):
        return LLMResult(reply="ok", messages=list(messages))


class StubSTTBackend:
    def transcribe(self, _audio):  # pragma: no cover
        raise AssertionError("STT not used in UI contract tests")


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


def _extract_function_body(text: str, function_name: str) -> str:
    signature = f"function {function_name}("
    start = text.find(signature)
    assert start >= 0, f"{function_name} not found"
    open_brace = text.find("{", start)
    assert open_brace >= 0, f"opening brace for {function_name} not found"
    depth = 0
    for idx in range(open_brace, len(text)):
        ch = text[idx]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[open_brace + 1 : idx]
    raise AssertionError(f"closing brace for {function_name} not found")


@pytest.mark.ui_contract
def test_logs_tab_exposes_dm_events_controls(monkeypatch):
    app = _make_app(monkeypatch)
    client = app.test_client()

    resp = client.get("/")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)

    required_snippets = [
        '<option value="dm_events">dialog manager events</option>',
        'id="dmLogLevel"',
        'id="dmLogEvent"',
        'id="dmLogSource"',
        'id="dmLogQuery"',
        'id="dmLogLimit"',
    ]
    for snippet in required_snippets:
        assert snippet in html


@pytest.mark.ui_contract
def test_commands_tab_exposes_posture_and_auto_rest_pills(monkeypatch):
    app = _make_app(monkeypatch)
    client = app.test_client()

    resp = client.get("/")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)

    assert 'id="cmdNaoPostureState"' in html
    assert 'id="cmdNaoAutoRestState"' in html
    assert "function currentCmdNaoAutoRestRemaining()" in html
    assert "fetch('/api/nao_command_state')" in html


@pytest.mark.ui_contract
def test_logs_sync_behavior_is_manual_when_logs_tab_is_active(monkeypatch):
    app = _make_app(monkeypatch)
    client = app.test_client()

    resp = client.get("/")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    body = _extract_function_body(html, "syncLogsPolling")

    assert "refreshProcessLogs().catch(() => {});" in body
    assert "setInterval(" not in body


@pytest.mark.ui_contract
def test_refresh_state_gates_history_render_on_history_version(monkeypatch):
    app = _make_app(monkeypatch)
    client = app.test_client()

    resp = client.get("/")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    body = _extract_function_body(html, "refreshState")

    assert "let lastHistoryVersion = null;" in html
    assert "history_version" in body
    assert "lastHistoryVersion" in body
    assert "version !== lastHistoryVersion" in body
    assert "renderHistory(j.history || [], null, { forceScroll: false });" in body
