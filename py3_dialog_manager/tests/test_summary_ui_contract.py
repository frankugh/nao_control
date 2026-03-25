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
        raise AssertionError("STT not used in summary UI contract test")


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


@pytest.mark.ui_contract
def test_summary_page_exposes_workflow_config_and_bootstrap_calls(monkeypatch):
    app = _make_app(monkeypatch)
    client = app.test_client()

    resp = client.get("/summary")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)

    required_snippets = [
        "NAO Control",
        'id="summaryPresetSelect"',
        'id="cfgInputDevice"',
        'id="cfgOutputDevice"',
        'id="cfgFallbackSummary"',
        'id="summaryPromptViewMode"',
        'id="summaryPromptEditor"',
        'id="summaryPromptPreviewMd"',
        'id="btnSummaryPromptNew"',
        'id="btnSummaryPromptLoad"',
        'id="btnSummaryPromptSave"',
        'id="btnSummaryPromptSaveAs"',
        'data-view="markdown"',
        "fetch(`/api/summary_prompt_content?name=",
        "fetchJson('/api/summary_prompt_save'",
        '<div class="section-title">Master prompt</div>',
        '<div class="section-title">Agent config</div>',
        'data-tab="basis"',
        'data-tab="advanced"',
        'data-tab="raw"',
        'id="connectivityBanner"',
        'id="summaryHealthRow"',
        "fetchJson('/api/summary')",
        "fetchJson('/api/summary/options')",
        "fetchJson('/api/summary/presets')",
        "\\u2601 cloud",
        "\\u2302 lokaal",
        "summary_runtime.config.json",
    ]
    for snippet in required_snippets:
        assert snippet in html
