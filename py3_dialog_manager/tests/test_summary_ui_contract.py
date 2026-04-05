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
        "NAO Studio",
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
        'id="incidentBanner"',
        'id="summaryToast"',
        'id="summaryExecutionModeToggle"',
        'id="summaryHealthRow"',
        'id="summaryStalePublishDialog"',
        "Toch uitspreken",
        "Opnieuw genereren",
        "fetchJson('/api/summary')",
        "fetchJson('/api/summary/options')",
        "fetchJson('/api/summary/presets')",
        "fetchJson('/api/state')",
        "fetchJson('/api/process_start'",
        "fetchJson('/api/process_status')",
        "fetchJson('/api/runtime_health'",
        "function ensureBackendProcessesStarted()",
        "function waitForRuntimeHealthReady(",
        "await ensureBackendProcessesStarted();",
        "await waitForRuntimeHealthReady(cfg)",
        "function effectiveOutputDeviceSelection(cfg)",
        "function syncEditableSessionSnapshot(cfg)",
        "function reconcileBusyStateWithServer()",
        "if(state.recoveryWorking) return;",
        "busyActionResolvedByServerStatus(state.busyAction,serverStatus)",
        "status==='capturing'||status==='summarizing'||status==='publishing'",
        "Ga naar transcript- of summarycontrole om instellingen voor deze sessie aan te passen.",
        "fetchJson('/api/summary/navigate'",
        "fetchJson('/api/summary/capture/resume'",
        "fetchJson('/api/summary/execution_mode'",
        "fetchJson('/api/summary/capture/recover'",
        "fetchJson('/api/summary/repair'",
        "fetchJson('/api/summary/use_fallback_summary'",
        "fetchJson('/api/summary/complete_without_publish'",
        'id="summaryFallbackPreview"',
            "Spreek fallbacksamenvatting uit",
        "active.id!=='summaryTranscriptEditor'&&active.id!=='summaryDraftEditor'&&active.id!=='summaryRepairPrompt'",
        "function filteredConnectivityIssues(rawItems)",
        "item&&item.active!==false",
        "function summaryBusyStatus()",
        "function summaryActionLocked()",
        "function summaryRequestInFlight()",
        "node.disabled=!enabled||busy",
        "const disabled=summaryActionLocked()||opts.disabled===true",
        "if(summaryActionLocked()) return;",
        "setBusyState('capture_recover','capturing','Transcriptie wordt hervat...')",
        "setBusyState('stop_output',getStatusKey()||'publishing','Stop-signaal voor audio versturen...')",
        "if(summaryRequestInFlight()&&state.busyAction!=='publish') return;",
        "preserveValue:!active.readOnly&&!active.disabled",
        "sticky_banner_catalog",
        "Aan het denken...",
        "Aan het uitspreken...",
        "\\u2601 cloud",
        "\\u2302 lokaal",
        "summary_runtime.config.json",
        'data-target-phase="capture"',
        'data-target-phase="transcript_review"',
        'data-target-phase="summary_review"',
        "Samenvatting controleren",
        "Vervolg opname",
    ]
    for snippet in required_snippets:
        assert snippet in html
    assert "showStopOutput=status==='completed'" not in html
