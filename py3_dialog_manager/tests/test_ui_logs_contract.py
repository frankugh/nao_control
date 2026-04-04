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
def test_commands_tab_renders_disabled_state_when_nao_is_disabled(monkeypatch):
    app = _make_app(monkeypatch)
    client = app.test_client()

    resp = client.get("/")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)

    render_body = _extract_function_body(html, "renderCmdNaoState")
    assert "motoren: disabled" in render_body
    assert "aut life: disabled" in render_body
    assert "posture: disabled" in render_body
    assert "auto-rest: disabled" in render_body
    assert "cmdNaoLifeStatus.textContent = 'disabled';" in render_body
    assert "async function refreshCmdNaoState(opts = {})" in html
    assert "if (cmdNaoDisabledByConfig) {" in html
    assert "resetCmdNaoState({ disabled: true });" in html
    assert "cmdNaoDisabledByConfig = !cfg.nao_ip_enabled;" in html
    assert "cmdNaoHealthAvailable = audioOk && !!cfg.nao_ip_enabled;" in html
    assert "if (cmdNaoHealthAvailable && !wasCmdNaoAvailable)" in html


@pytest.mark.ui_contract
def test_connectivity_polling_stays_fast_while_active_issues_exist(monkeypatch):
    app = _make_app(monkeypatch)
    client = app.test_client()

    resp = client.get("/")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    update_body = _extract_function_body(html, "updateConnectivityBanner")
    schedule_body = _extract_function_body(html, "scheduleHealth")

    assert "let healthHasActiveConnectivityIssue = false;" in html
    assert "healthHasActiveConnectivityIssue = activeIssues.length > 0;" in update_body
    assert "const shouldPollFast = !isHealthy || healthHasActiveConnectivityIssue;" in schedule_body
    assert "const interval = boost ? 1000 : (shouldPollFast ? 5000 : 30000);" in schedule_body


@pytest.mark.ui_contract
def test_manual_process_start_is_not_blocked_by_last_nao_health_snapshot(monkeypatch):
    app = _make_app(monkeypatch)
    client = app.test_client()

    resp = client.get("/")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    start_body = _extract_function_body(html, "startProcess")

    assert "lastNaoOk === false" not in start_body
    assert "setStatus('NAO is down; proces niet gestart.');" not in start_body
    assert "const r = await fetch('/api/process_start'" in start_body


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


@pytest.mark.ui_contract
def test_chat_ui_keeps_send_button_stable_and_exposes_separate_stop_buttons(monkeypatch):
    app = _make_app(monkeypatch)
    client = app.test_client()

    resp = client.get("/")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    update_body = _extract_function_body(html, "updateSendButton")
    stop_buttons_body = _extract_function_body(html, "updateStopButtons")

    assert 'id="btnSend"' in html
    assert 'id="btnStopCommand"' in html
    assert 'id="btnStopAll"' in html
    assert "btnSend.textContent = 'Send';" in update_body
    assert "btnSend.textContent = sendStopInFlight ? 'Stopping...' : 'Stop';" not in html
    assert "if (sendStopInFlight || sendBusyPhase || activeActionController) return;" in html
    assert "if (sendStopInFlight || sendBusyPhase || activeActionController) {\n          await stopCurrentAction();\n          return;\n        }" not in html
    assert "btnStopCommand.hidden = !hasCommandStop;" in stop_buttons_body
    assert "btnStopAll.textContent = sendStopInFlight ? 'Stop all...' : 'Stop all';" in stop_buttons_body


@pytest.mark.ui_contract
def test_chat_ui_renders_local_pending_turn_while_llm_is_thinking(monkeypatch):
    app = _make_app(monkeypatch)
    client = app.test_client()

    resp = client.get("/")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)

    assert "let localPendingTurn = null;" in html
    assert "function setLocalPendingTurn(text, emitMode = 'pipeline')" in html
    assert "function appendLocalPendingTurn()" in html
    assert "Antwoord wordt gemaakt..." in html
    assert "appendLocalPendingTurn();" in html
    assert "setStatus('LLM antwoord aan het maken...');" in html
    assert "setLocalPendingTurn(text, emitMode);" in html


@pytest.mark.ui_contract
def test_chat_ui_polls_state_and_promotes_pending_reply_while_tts_is_running(monkeypatch):
    app = _make_app(monkeypatch)
    client = app.test_client()

    resp = client.get("/")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    refresh_body = _extract_function_body(html, "refreshState")

    assert "const SEND_STATE_POLL_MS = 700;" in html
    assert "let sendStatePollTimer = null;" in html
    assert "function updateSendStatePolling()" in html
    assert "function historyHasCommittedLocalPendingTurn(history)" in html
    assert "function syncPendingTurnFromHistory(history)" in html
    assert "setStatus('Antwoord zichtbaar; spraak wordt nog uitgesproken...');" in html
    assert "const pendingReplyCommitted = syncPendingTurnFromHistory(j.history || []);" in refresh_body
    assert "pendingReplyCommitted || !hasVersion" in refresh_body


@pytest.mark.ui_contract
def test_chat_stop_buttons_split_behavior_stop_and_stop_all(monkeypatch):
    app = _make_app(monkeypatch)
    client = app.test_client()

    resp = client.get("/")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    stop_behavior_body = _extract_function_body(html, "stopCurrentBehavior")
    stop_all_body = _extract_function_body(html, "stopCurrentAction")

    assert "await requestSpecificBehaviorStop(behaviorTarget)" in stop_behavior_body
    assert "await requestCommandStop()" in stop_behavior_body
    assert "setStatus(`Stop-commando verstuurd voor ${label}.`);" in stop_behavior_body
    assert "await requestCommandStop();" in stop_all_body
    assert "await requestNaoAudioStop();" not in stop_all_body
    assert "setStatus('Alles gestopt.');" in stop_all_body
    assert "function beginOptimisticStopAction(label, opts = {})" in html
    assert "function hasActiveStopAction()" in html
    assert "btnStopAll.disabled = sendStopInFlight;" in html
    assert "if (!hasActiveStopAction()) return;" not in stop_all_body


@pytest.mark.ui_contract
def test_chat_ui_shows_stop_actions_optimistically_for_stoppable_commands_and_behaviors(monkeypatch):
    app = _make_app(monkeypatch)
    client = app.test_client()

    resp = client.get("/")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    command_body = _extract_function_body(html, "runSelectedCommand")
    behavior_body = _extract_function_body(html, "runSelectedBehavior")

    assert "beginOptimisticStopAction(normalizedLabel)" in command_body
    assert "normalizedLabel === 'DANCE' || normalizedLabel === 'WALK_WITH_ME'" in command_body
    assert "beginOptimisticStopAction(stopLabel, { behaviorTarget: behavior })" in behavior_body
    assert "syncStopAvailability(j);" in behavior_body


@pytest.mark.ui_contract
def test_chat_ui_uses_server_runtime_config_without_localstorage_runtime_merge(monkeypatch):
    app = _make_app(monkeypatch)
    client = app.test_client()

    resp = client.get("/")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    apply_body = _extract_function_body(html, "applyRuntimeConfig")

    assert "nao_runtime_config_v1" not in html
    assert "function loadLocalConfig()" not in html
    assert "function saveLocalConfig(cfg)" not in html
    assert "saveLocalConfig(configToSend);" not in apply_body
    assert "const local = loadLocalConfig();" not in html
    assert "await applyRuntimeConfig(cfg, true);" not in html
    assert "localStorage.setItem(CFG_SUBTAB_KEY, which);" in html
    assert "nao_client_audio_prefs_v1" in html
