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
    assert 'id="btnCmdNaoRefresh"' in html
    assert "function currentCmdNaoAutoRestRemaining()" in html
    assert "fetch('/api/nao_command_state')" in html


@pytest.mark.ui_contract
def test_chat_ui_exposes_hybrid_nao_header_actions(monkeypatch):
    app = _make_app(monkeypatch)
    client = app.test_client()

    resp = client.get("/")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)

    assert 'id="navBadgePreviewTrigger"' in html
    assert 'id="btnNaoConnectPlaceholder"' in html
    assert 'id="btnNaoDisconnectPlaceholder"' in html
    assert 'id="btnCmdWakeUp"' in html
    assert 'id="btnCmdRest"' in html
    assert 'id="btnCmdAutLifeToggle"' in html
    assert 'id="naoHeaderActionStatus"' in html
    assert html.index('id="navBadgePreviewTrigger"') < html.index('id="btnNaoConnectPlaceholder"') < html.index('id="navToMain"')
    assert 'id="btnNaoConnectPlaceholder" type="button" class="app-nav-action nao-header-action nao-header-placeholder" title="Connect met NAO (later)" aria-label="Connect met NAO" disabled' in html
    assert 'id="btnNaoDisconnectPlaceholder" type="button" class="app-nav-action nao-header-action nao-header-placeholder" title="Disconnect met NAO (later)" aria-label="Disconnect met NAO" disabled' in html
    assert 'id="btnCmdWakeUp" type="button" class="app-nav-action nao-header-action" title="NAO wakker maken" aria-label="NAO wakker maken"' in html
    assert 'id="btnCmdRest" type="button" class="app-nav-action nao-header-action" title="NAO laten slapen" aria-label="NAO laten slapen"' in html
    assert 'id="btnCmdAutLifeToggle" type="button" class="app-nav-action nao-header-action" title="Autonoom leven" aria-label="Autonoom leven" aria-pressed="false"' in html
    assert 'class="nao-header-icon connect"' in html
    assert 'class="nao-header-icon disconnect"' in html
    assert 'body[data-color-scheme="night_blue"] .nao-header-status.error' in html


@pytest.mark.ui_contract
def test_commands_tab_keeps_rich_status_and_removes_duplicate_nao_actions(monkeypatch):
    app = _make_app(monkeypatch)
    client = app.test_client()

    resp = client.get("/")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)

    assert html.count('id="btnCmdWakeUp"') == 1
    assert html.count('id="btnCmdRest"') == 1
    assert html.count('id="btnCmdAutLifeToggle"') == 1
    assert 'id="cmdRobotNameBadge"' not in html
    assert 'id="cmdNaoLifeStatus"' not in html
    assert '>Commands</label>' in html
    assert 'Laat de NAO robot lopen' in html
    assert 'Hier zie je geïnstalleerde behaviors van een verbonden NAO robot.' in html
    assert 'Verbind met een NAO robot om beschikbare behaviors te bekijken.' in html
    assert 'id="behaviorActions" class="row" style="margin-top:8px" hidden' in html
    assert 'id="btnMoveForward"' in html
    assert 'id="locomotionFrequency"' in html
    assert 'id="behaviorTree" hidden' in html
    assert html.index('>Commands</label>') < html.index('Laat de NAO robot lopen')
    assert html.index('Laat de NAO robot lopen') < html.index('>NAO ogen</label>')
    assert html.index('>NAO ogen</label>') < html.index('>Behaviors</label>')
    assert 'id="btnCmdStopCommand" type="button" hidden>Stop</button>' in html
    assert 'id="btnCmdStopAll" type="button">Stop all</button>' in html
    assert "cmdList.size = Math.max(1, Math.min(10, commands.length));" in html
    assert "btnCmdStopCommand.hidden = !hasCommandStop;" in html
    assert "btnCmdStopAll.textContent = sendStopInFlight ? 'Stop all...' : 'Stop all';" in html
    assert 'id="reviewCounter" class="small" style="margin-bottom:6px" hidden' in html
    assert 'id="reviewCounterReview" class="small" style="margin-bottom:8px" hidden' in html
    assert 'id="retrainDelta" style="margin-bottom:6px" hidden' in html
    assert 'Nieuwe reviewed sinds laatste retrain:' not in html
    assert 'laatste bundle:' not in html
    assert 'Reviewed sinds laatste retrain:' not in html
    assert "el.textContent = '';" in html
    assert "el.hidden = true;" in html


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
    assert "btnCmdWakeUp.disabled = true;" in render_body
    assert "btnCmdRest.disabled = true;" in render_body
    assert "btnCmdAutLifeToggle.setAttribute('aria-pressed', 'false');" in render_body
    assert "renderNaoHeaderStatus();" in render_body
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
def test_cloud_llm_banner_suggests_opening_model_picker(monkeypatch):
    app = _make_app(monkeypatch)
    client = app.test_client()

    resp = client.get("/")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    render_body = _extract_function_body(html, "renderConnectivityBanner")
    focus_body = _extract_function_body(html, "focusCloudLlmModelControl")
    picker_body = _extract_function_body(html, "openCloudLlmModelPicker")

    assert "Overweeg tijdelijk een ander cloudmodel." in render_body
    assert "Open modelkeuze" in render_body
    assert "issue.issue_type === 'cloud' && String(issue.source || '').startsWith('llm.')" in render_body
    assert "if (cloudLlmIssue && isServerUi()) {" in render_body
    assert 'id="cfgLlmModel" data-cloud-llm-model-control' in html
    assert "document.querySelector('[data-cloud-llm-model-control]') || cfgLlmModel || cfgLlmType" in focus_body
    assert "const settingsReasoningSection = settingsModal instanceof HTMLElement" in picker_body
    assert "settingsModal.querySelector('[data-settings-section=\"reasoning\"]')" in picker_body
    assert "openSettingsModal();" in picker_body
    assert "settingsReasoningSection.click();" in picker_body
    assert "setTab('runtime');" in picker_body
    assert "setConfigTab('reasoning');" in picker_body
    assert "focusCloudLlmModelControl();" in picker_body


@pytest.mark.ui_contract
def test_reasoning_tab_exposes_model_dependent_thinking_control(monkeypatch):
    app = _make_app(monkeypatch)
    client = app.test_client()

    resp = client.get("/")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    render_body = _extract_function_body(html, "renderLlmThinkingControl")
    refresh_body = _extract_function_body(html, "refreshLlmThinkingCapability")
    form_body = _extract_function_body(html, "getConfigFromForm")

    assert 'id="cfgLlmThinkingRow" hidden' in html
    assert 'id="cfgLlmThinking"' in html
    assert "function normalizeLlmThinkingMode(value)" in html
    assert "function allowedLlmThinkingModes(modeType)" in html
    assert "function renderLlmThinkingControl(modeType, preferredMode)" in html
    assert "function refreshLlmThinkingCapability()" in html
    assert "['off', 'Uit']" in render_body
    assert "['on', 'Aan']" in render_body
    assert "['low', 'Low']" in render_body
    assert "['medium', 'Medium']" in render_body
    assert "['high', 'High']" in render_body
    assert "cfgLlmThinkingRow.hidden = normalizedType === 'none' || cfgLlmType.value === 'echo';" in render_body
    assert "fetch(`/api/ollama_model_capabilities?${params.toString()}`)" in refresh_body
    assert "llm_thinking_mode: llmThinkingMode" in form_body
    assert "desiredLlmThinkingMode = normalizeLlmThinkingMode(cfg.llm_thinking_mode || 'default');" in html
    assert "cfgLlmThinking.addEventListener('change'" in html


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
def test_nao_refresh_follows_header_actions_outside_commands_tab(monkeypatch):
    app = _make_app(monkeypatch)
    client = app.test_client()

    resp = client.get("/")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    command_body = _extract_function_body(html, "runSelectedCommand")

    assert "scheduleCmdNaoRefresh(commandNaoRefreshDelayMs(label), false);" in command_body
    assert "if (cmdNaoHealthAvailable) {" in command_body
    assert "activeTab === 'commands'" not in html
    assert "activeTab !== 'commands'" not in html
    assert "NAO status verversen na" not in html
    assert html.count("scheduleCmdNaoRefresh(150, false);") >= 2
    assert html.count("refreshCmdNaoState({ showErrors: false }).catch(() => {});") >= 4


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


@pytest.mark.ui_contract
def test_chat_ui_exposes_settings_modal_for_ui_preferences(monkeypatch):
    app = _make_app(monkeypatch)
    client = app.test_client()

    resp = client.get("/")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    actions_body = _extract_function_body(html, "updateSettingsModalActions")
    open_body = _extract_function_body(html, "openSettingsModal")
    identity_body = _extract_function_body(html, "updateAppIdentity")
    visibility_body = _extract_function_body(html, "applyUiSettingsVisibility")

    assert 'id="btnOpenSettings"' in html
    assert 'id="navToActiveLearning" class="small" href="/active_learning" hidden>Actief leren</a>' in html
    assert html.index('id="navToMain"') < html.index('id="btnOpenSettings"')
    assert 'id="btnOpenSettings" type="button" class="app-nav-action" aria-label="Instellingen" title="Instellingen" hidden' in html
    assert 'id="settingsModal"' in html
    assert 'data-settings-section="ui"' in html
    assert 'id="uiSettingsRobotName"' in html
    assert 'id="uiSettingsColorScheme"' in html
    assert 'id="uiSettingsShowLogsTab"' in html
    assert 'id="uiSettingsShowActiveLearningNav"' in html
    assert 'id="tabLogs" class="tab" type="button" hidden>Logs</button>' in html
    assert 'title="Wordt gebruikt in badges en de paginatitel. Wijzigt pas na opslaan."' in html
    assert 'title="Previewt direct in deze UI. Annuleren herstelt het vorige schema."' in html
    assert "btnOpenSettings.hidden = !isServerUi();" in actions_body
    assert "if (!settingsModal || !isServerUi()) return;" in open_body
    assert "const baseTitle = isActiveLearningMode() ? 'Actief leren' : 'NAO Studio';" in identity_body
    assert "navToActiveLearning.hidden = isActiveLearningMode() || !uiSettingsState.ui_show_active_learning_nav;" in visibility_body
    assert "tabLogs.hidden = isActiveLearningMode() || !uiSettingsState.ui_show_logs_tab;" in visibility_body
    assert 'Centrale plek voor UI- en runtime-instellingen. Deze eerste versie bevat alleen UI.' not in html
    assert 'Persoonlijke UI-instellingen voor deze instance. Kleur previewt direct; opslaan maakt de wijziging definitief.' not in html
    assert 'Alleen-lezen in clientmodus.' not in html
    assert 'Wordt gebruikt in badges en de paginatitel. Wijzigt pas na opslaan.</div>' not in html
    assert 'Previewt direct in deze UI. Annuleren herstelt het vorige schema.</div>' not in html
    assert 'id="cfgRobotName"' not in html
    assert 'id="cfgColorScheme"' not in html


@pytest.mark.ui_contract
def test_chat_ui_settings_modal_previews_and_restores_color_scheme_locally(monkeypatch):
    app = _make_app(monkeypatch)
    client = app.test_client()

    resp = client.get("/")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    sync_body = _extract_function_body(html, "syncUiSettingsDraftFromForm")
    close_body = _extract_function_body(html, "closeSettingsModal")

    assert "applyColorScheme(normalizedDraft.ui_color_scheme);" in sync_body
    assert "const restorePreview = opts.restorePreview !== false;" in close_body
    assert "applyColorScheme(uiSettingsState.ui_color_scheme);" in close_body


@pytest.mark.ui_contract
def test_chat_ui_settings_modal_saves_partial_runtime_config_and_uses_state_in_runtime_form(monkeypatch):
    app = _make_app(monkeypatch)
    client = app.test_client()

    resp = client.get("/")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    save_body = _extract_function_body(html, "saveUiSettings")
    config_body = _extract_function_body(html, "getConfigFromForm")

    assert "robot_name: normalizedDraft.robot_name" in save_body
    assert "ui_color_scheme: normalizedDraft.ui_color_scheme" in save_body
    assert "ui_show_logs_tab: normalizedDraft.ui_show_logs_tab" in save_body
    assert "ui_show_active_learning_nav: normalizedDraft.ui_show_active_learning_nav" in save_body
    assert "body: JSON.stringify({ config: payload })" in save_body
    assert "robot_name: uiSettingsState.robot_name" in config_body
    assert "ui_color_scheme: uiSettingsState.ui_color_scheme" in config_body
    assert "ui_show_logs_tab: uiSettingsState.ui_show_logs_tab" in config_body
    assert "ui_show_active_learning_nav: uiSettingsState.ui_show_active_learning_nav" in config_body
    assert "addApplyButtonForInput(cfgRobotName" not in html


@pytest.mark.ui_contract
def test_chat_ui_settings_modal_has_dark_theme_overrides(monkeypatch):
    app = _make_app(monkeypatch)
    client = app.test_client()

    resp = client.get("/")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)

    assert 'body[data-color-scheme="night_blue"] .settings-sidebar' in html
    assert 'body[data-color-scheme="night_brown"] .settings-nav-item.active' in html
    assert 'body[data-color-scheme="night_teal"] .settings-card' in html
    assert 'body[data-color-scheme="night_blue"] .settings-close' in html
