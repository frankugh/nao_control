
import {
  ACTION_TYPES,
  DO_MODES,
  PPT_MODES,
  START_MODES,
  applyConfigPaneObject,
  buildConfigPaneObject,
  cloneJson,
  formatJson,
  hasBlockingBlockErrors,
  insertSnippetAfterSelection,
  isObject,
  moveStepInState,
  parseAdvancedStepJson,
  parseConfigPaneJson,
  parseEditorToScriptState,
  parseJsonText,
  parseSnippetForInsert,
  resolveInsertIndex,
  scriptStateToEditorText,
  shouldWarnWithPrevReorder,
  updateObjectPath,
} from "./blocks_model.js";

(function () {
  "use strict";

  const PLACEHOLDER_FILE_LABEL = "Niet opgeslagen";
  const CONFIG_KEYS = ["robots", "ppt", "defaults"];

  const editorJson = document.getElementById("editorJson");
  const selCategory = document.getElementById("selCategory");
  const selTemplate = document.getElementById("selTemplate");
  const btnNew = document.getElementById("btnNew");
  const btnLoad = document.getElementById("btnLoad");
  const btnSave = document.getElementById("btnSave");
  const btnSaveAs = document.getElementById("btnSaveAs");
  const btnCopyTemplate = document.getElementById("btnCopyTemplate");
  const btnInsertTemplate = document.getElementById("btnInsertTemplate");
  const btnDmStart = document.getElementById("btnDmStart");
  const btnRunStart = document.getElementById("btnRunStart");
  const btnRunNext = document.getElementById("btnRunNext");
  const btnRunAbort = document.getElementById("btnRunAbort");
  const btnTabJson = document.getElementById("btnTabJson");
  const btnTabBlocks = document.getElementById("btnTabBlocks");
  const jsonView = document.getElementById("jsonView");
  const blocksView = document.getElementById("blocksView");
  const blocksConfigSection = document.getElementById("blocksConfigSection");
  const blocksConfigSummary = document.getElementById("blocksConfigSummary");
  const blocksConfigJson = document.getElementById("blocksConfigJson");
  const btnApplyConfig = document.getElementById("btnApplyConfig");
  const blocksConfigError = document.getElementById("blocksConfigError");
  const stepsCards = document.getElementById("stepsCards");
  const blocksStepCount = document.getElementById("blocksStepCount");
  const stepInspector = document.getElementById("stepInspector");
  const blocksEmpty = document.getElementById("blocksEmpty");
  const fileLabel = document.getElementById("fileLabel");
  const saveState = document.getElementById("saveState");
  const statusMessage = document.getElementById("statusMessage");
  const runStatus = document.getElementById("runStatus");
  const runProgress = document.getElementById("runProgress");
  const runStep = document.getElementById("runStep");
  const runLogDetails = document.getElementById("runLogDetails");
  const runLog = document.getElementById("runLog");
  const dmStartResults = document.getElementById("dmStartResults");
  const templateInspector = document.getElementById("templateInspector");
  const templateStepCount = document.getElementById("templateStepCount");
  const quickOpenButtons = Array.from(document.querySelectorAll(".btnQuickOpen"));

  let templatesData = null;
  let catalog = [];
  let defaultScript = null;
  let currentCategoryKey = "";
  let currentTemplateKey = "";
  let previewBaseline = "";
  let templateDraftSteps = [];
  let templateDraftIsArray = false;
  let templateAdvancedDrafts = new Map();
  let templateAdvancedOpen = new Set();
  let templateStepErrors = new Map();
  let currentFileHandle = null;
  let currentFileLabel = PLACEHOLDER_FILE_LABEL;
  let editorDirty = false;
  let runtimeState = null;
  let runPollTimer = null;
  let runRequestInFlight = false;
  let lastRuntimeError = "";
  let runPollFailures = 0;
  let pollPausedByNetworkError = false;
  let dmStartResultState = [];
  let lastRunLogAutoOpenSignal = "";

  let viewMode = "json";
  let blocksSessionActive = false;
  let scriptState = null;
  let selectedStepIndex = null;
  let blocksConfigDraft = "{}";
  let blocksConfigErrorMessage = "";
  let blocksStepErrors = new Map();
  let advancedDrafts = new Map();
  let advancedOpen = new Set();
  let dragSourceIndex = null;
  let dragOverIndex = null;
  let lastAutoFollowKey = "";

  function setStatus(message, level) {
    statusMessage.textContent = message;
    statusMessage.classList.remove("status-info", "status-ok", "status-warn", "status-error");
    if (level === "ok") {
      statusMessage.classList.add("status-ok");
      return;
    }
    if (level === "warn") {
      statusMessage.classList.add("status-warn");
      return;
    }
    if (level === "error") {
      statusMessage.classList.add("status-error");
      return;
    }
    statusMessage.classList.add("status-info");
  }

  function updateFileMeta() {
    fileLabel.textContent = currentFileLabel;
    saveState.textContent = editorDirty ? "Onopgeslagen wijzigingen" : "Clean";
  }

  function setDirty(value) {
    editorDirty = !!value;
    updateFileMeta();
  }

  function setFileLabel(name) {
    currentFileLabel = name || PLACEHOLDER_FILE_LABEL;
    updateFileMeta();
  }

  function setEditorText(text, options) {
    const dirty = options && options.dirty === true;
    editorJson.value = text;
    setDirty(dirty);
  }

  function clearBlockValidationErrors() {
    blocksConfigErrorMessage = "";
    blocksStepErrors = new Map();
    renderBlocksConfigError();
    renderRuntimeState(runtimeState);
  }

  function resetBlocksSession() {
    blocksSessionActive = false;
    scriptState = null;
    selectedStepIndex = null;
    blocksConfigDraft = "{}";
    blocksConfigErrorMessage = "";
    blocksStepErrors = new Map();
    advancedDrafts = new Map();
    advancedOpen = new Set();
    lastAutoFollowKey = "";
    renderBlocks();
  }

  function setViewMode(mode) {
    viewMode = mode === "blocks" ? "blocks" : "json";
    const isBlocks = viewMode === "blocks";
    btnTabJson.classList.toggle("is-active", !isBlocks);
    btnTabBlocks.classList.toggle("is-active", isBlocks);
    jsonView.classList.toggle("is-hidden", isBlocks);
    blocksView.classList.toggle("is-hidden", !isBlocks);
  }

  function isActiveRunStatus(status) {
    return status === "preflight" || status === "running" || status === "waiting";
  }

  function setRuntimeButtons(state) {
    const status = state && state.status ? String(state.status) : "idle";
    const waiting = !!(state && state.waiting_for_next);
    const blockedByBlocks = hasBlockingBlockErrors(blocksConfigErrorMessage, blocksStepErrors);
    btnDmStart.disabled = blockedByBlocks;
    btnRunStart.disabled =
      !(
        status === "idle" ||
        status === "completed" ||
        status === "aborted" ||
        status === "failed"
      ) || blockedByBlocks;
    btnRunNext.disabled = !waiting;
    btnRunAbort.disabled = !isActiveRunStatus(status);
  }

  function renderDmStartResults(results) {
    dmStartResultState = Array.isArray(results) ? results.slice() : [];
    dmStartResults.replaceChildren();
    if (dmStartResultState.length === 0) {
      dmStartResults.classList.add("is-hidden");
      return;
    }
    dmStartResultState.forEach(function (item) {
      const started = !!(item && item.started);
      const robotId = item && item.robot_id ? String(item.robot_id) : "?";
      const dmUrl = item && item.dm_url ? String(item.dm_url) : "";
      const instanceId = item && item.instance_id ? String(item.instance_id) : "";
      const message =
        item && item.message ? String(item.message) : item && item.error ? String(item.error) : "";

      const card = document.createElement("div");
      card.className = "dm-start-result " + (started ? "is-ok" : "is-error");

      const title = document.createElement("div");
      title.className = "dm-start-result-title";
      title.textContent = robotId + (started ? " gestart" : " niet gestart");
      card.appendChild(title);

      const meta = document.createElement("div");
      meta.className = "dm-start-result-meta";
      const parts = [];
      if (dmUrl) {
        parts.push(dmUrl);
      }
      if (instanceId) {
        parts.push("instance_id=" + instanceId);
      }
      meta.textContent = parts.join(" | ");
      card.appendChild(meta);

      const body = document.createElement("div");
      body.className = "dm-start-result-message";
      body.textContent = message;
      card.appendChild(body);

      dmStartResults.appendChild(card);
    });
    dmStartResults.classList.remove("is-hidden");
  }

  function _resolveRunCurrentIndex(state) {
    if (!state || !scriptState || !Array.isArray(scriptState.root.steps)) {
      return null;
    }
    const status = String(state.status || "");
    if (!isActiveRunStatus(status)) {
      return null;
    }
    const steps = scriptState.root.steps;
    const currentStepId = String(state.current_step_id || "").trim();
    if (currentStepId) {
      const byId = steps.findIndex((step) => String((step && step.id) || "").trim() === currentStepId);
      if (byId >= 0) {
        return byId;
      }
      return null;
    }
    const idx = Number(state.current_step_index);
    if (Number.isInteger(idx) && idx >= 0 && idx < steps.length) {
      return idx;
    }
    return null;
  }

  function _resolveRunNextIndex(state, currentIndex) {
    if (!state || !scriptState || !Array.isArray(scriptState.root.steps)) {
      return null;
    }
    const status = String(state.status || "");
    if (status === "idle" || status === "completed" || status === "aborted" || status === "failed") {
      return null;
    }
    const steps = scriptState.root.steps;
    if (currentIndex !== null && Number.isInteger(currentIndex)) {
      const next = currentIndex + 1;
      return next < steps.length ? next : null;
    }
    const currentStepId = String(state.current_step_id || "").trim();
    if (currentStepId) {
      return null;
    }
    const completed = Number(state.completed_steps);
    if (Number.isInteger(completed) && completed >= 0 && completed < steps.length) {
      return completed;
    }
    return steps.length > 0 ? 0 : null;
  }

  function applyRunStepHighlights(state) {
    if (!stepsCards) {
      return;
    }
    const currentIndex = _resolveRunCurrentIndex(state);
    const nextIndex = _resolveRunNextIndex(state, currentIndex);
    const rows = stepsCards.querySelectorAll(".step-row");
    rows.forEach((row) => {
      const index = Number(row.getAttribute("data-index"));
      const isCurrent = Number.isInteger(index) && currentIndex !== null && index === currentIndex;
      const isNext =
        Number.isInteger(index) &&
        nextIndex !== null &&
        index === nextIndex &&
        !(currentIndex !== null && index === currentIndex);
      row.classList.toggle("is-run-current", isCurrent);
      row.classList.toggle("is-run-next", isNext);
    });
    autoFollowRunStep(state, currentIndex, nextIndex);
  }

  function autoFollowRunStep(state, currentIndex, nextIndex) {
    if (!stepsCards || viewMode !== "blocks") {
      return;
    }
    const status = state && state.status ? String(state.status) : "";
    if (!isActiveRunStatus(status)) {
      lastAutoFollowKey = "";
      return;
    }
    const targetIndex = currentIndex;
    if (targetIndex === null || !Number.isInteger(targetIndex)) {
      return;
    }
    const runId = state && state.run_id ? String(state.run_id) : "";
    const key = runId + ":" + String(targetIndex);
    if (key === lastAutoFollowKey) {
      return;
    }
    lastAutoFollowKey = key;
    const row = stepsCards.querySelector('.step-row[data-index="' + String(targetIndex) + '"]');
    if (!(row instanceof HTMLElement)) {
      return;
    }
    const containerRect = stepsCards.getBoundingClientRect();
    const rowRect = row.getBoundingClientRect();
    const rawTop = stepsCards.scrollTop + (rowRect.top - containerRect.top) - 6;
    const maxScrollTop = Math.max(0, stepsCards.scrollHeight - stepsCards.clientHeight);
    const desiredTop = Math.min(maxScrollTop, Math.max(0, rawTop));
    stepsCards.scrollTop = desiredTop;
  }

  function updateRunLogTail(logLines) {
    const nextText = Array.isArray(logLines) ? logLines.join("\n") : "";
    const prevText = runLog.value;
    const prevTop = runLog.scrollTop;
    const prevHeight = runLog.scrollHeight;
    const threshold = 12;
    const wasNearBottom = prevHeight - (prevTop + runLog.clientHeight) <= threshold;
    if (nextText !== prevText) {
      runLog.value = nextText;
    }
    if (wasNearBottom) {
      runLog.scrollTop = runLog.scrollHeight;
      return;
    }
    const maxScrollTop = Math.max(0, runLog.scrollHeight - runLog.clientHeight);
    runLog.scrollTop = Math.min(maxScrollTop, prevTop);
  }

  function buildRunLogAutoOpenSignal(state, logLines, runError) {
    const status = state && state.status ? String(state.status) : "idle";
    const hasAutoOpenReason = status === "failed" || (isActiveRunStatus(status) && !!runError);
    if (!hasAutoOpenReason) {
      return "";
    }
    const runId = state && state.run_id ? String(state.run_id) : "";
    const stepLabel = state && state.current_step_id ? String(state.current_step_id) : "";
    const completed = state && typeof state.completed_steps === "number" ? state.completed_steps : 0;
    const total = state && typeof state.total_steps === "number" ? state.total_steps : 0;
    return [status, runId, stepLabel, String(completed), String(total), String(runError || ""), String(logLines.length)].join("|");
  }

  function maybeAutoOpenRunLog(state, logLines, runError) {
    if (!(runLogDetails instanceof HTMLDetailsElement)) {
      return;
    }
    const nextSignal = buildRunLogAutoOpenSignal(state, logLines, runError);
    if (!nextSignal) {
      lastRunLogAutoOpenSignal = "";
      return;
    }
    if (nextSignal === lastRunLogAutoOpenSignal) {
      return;
    }
    lastRunLogAutoOpenSignal = nextSignal;
    runLogDetails.open = true;
  }

  function renderRuntimeState(state) {
    runtimeState = state || null;
    const status = state && state.status ? String(state.status) : "idle";
    const completed = state && typeof state.completed_steps === "number" ? state.completed_steps : 0;
    const total = state && typeof state.total_steps === "number" ? state.total_steps : 0;
    const stepLabel = state && state.current_step_id ? String(state.current_step_id) : "-";
    const logLines = state && Array.isArray(state.log_tail) ? state.log_tail : [];
    const runError = state && typeof state.last_error === "string" ? String(state.last_error).trim() : "";
    runStatus.textContent = status;
    runProgress.textContent = String(completed) + " / " + String(total);
    runStep.textContent = stepLabel;
    updateRunLogTail(logLines);
    maybeAutoOpenRunLog(state, logLines, runError);
    setRuntimeButtons(state);
    applyRunStepHighlights(runtimeState);
  }

  async function fetchJson(url, options) {
    const response = await fetch(url, options);
    let payload = {};
    try {
      payload = await response.json();
    } catch (_err) {
      payload = {};
    }
    if (!response.ok) {
      const message =
        payload && typeof payload.error === "string" && payload.error
          ? payload.error
          : "HTTP " + String(response.status);
      throw new Error(message);
    }
    return payload;
  }

  function parseEditorScriptForRun() {
    const parsed = parseJsonText(editorJson.value, "Editor");
    if (!parsed.ok) {
      setStatus(parsed.error, "error");
      return null;
    }
    if (!isObject(parsed.value)) {
      setStatus("Editor root moet een JSON object zijn.", "error");
      return null;
    }
    return parsed.value;
  }

  function hasPendingBlockErrors() {
    return hasBlockingBlockErrors(blocksConfigErrorMessage, blocksStepErrors);
  }

  function ensureNoBlockingBlockErrors(actionLabel) {
    if (!hasPendingBlockErrors()) {
      return true;
    }
    setStatus(actionLabel + " geblokkeerd: corrigeer eerst Blocks fouten.", "error");
    return false;
  }

  async function refreshRunState(options) {
    if (runRequestInFlight) {
      return;
    }
    const silent = options && options.silent === true;
    runRequestInFlight = true;
    try {
      const state = await fetchJson("/api/run/state", { cache: "no-store" });
      runPollFailures = 0;
      if (pollPausedByNetworkError) {
        pollPausedByNetworkError = false;
        setStatus("Verbinding met run API hersteld.", "ok");
      }
      renderRuntimeState(state);
      const err = state && typeof state.last_error === "string" ? state.last_error.trim() : "";
      const status = state && typeof state.status === "string" ? state.status : "";
      if (err && err !== lastRuntimeError && (status === "failed" || !silent)) {
        if (status === "failed") {
          setStatus("Run failed: " + err, "error");
        } else {
          setStatus("Run melding: " + err, "warn");
        }
        lastRuntimeError = err;
      }
      if (!err) {
        lastRuntimeError = "";
      }
    } catch (err) {
      const message = err && err.message ? err.message : String(err);
      runPollFailures += 1;
      if (runPollFailures >= 3 && runPollTimer !== null) {
        window.clearInterval(runPollTimer);
        runPollTimer = null;
        pollPausedByNetworkError = true;
        setStatus(
          "Geen verbinding met run API. Start de Script Builder app opnieuw; polling is gepauzeerd.",
          "error"
        );
        return;
      }
      setStatus("Run state ophalen mislukt: " + message, "error");
    } finally {
      runRequestInFlight = false;
    }
  }

  function ensureRunPolling() {
    if (runPollTimer !== null) {
      return;
    }
    runPollTimer = window.setInterval(function () {
      refreshRunState({ silent: true });
    }, 500);
  }
  function normalizeTopLevelConfigObject(value) {
    const normalized = isObject(value) ? cloneJson(value) : {};
    for (const key of CONFIG_KEYS) {
      if (!Object.prototype.hasOwnProperty.call(normalized, key)) {
        normalized[key] = {};
      }
      if (typeof normalized[key] === "undefined") {
        normalized[key] = {};
      }
    }
    return normalized;
  }

  function ensureBlocksSessionFromEditor() {
    if (blocksSessionActive && scriptState && isObject(scriptState.root)) {
      return true;
    }
    const parsed = parseEditorToScriptState(editorJson.value);
    if (!parsed.ok) {
      setStatus(parsed.error, "error");
      return false;
    }
    scriptState = parsed.value;
    blocksSessionActive = true;
    selectedStepIndex =
      Array.isArray(scriptState.root.steps) && scriptState.root.steps.length > 0 ? 0 : null;
    blocksConfigDraft = formatJson(normalizeTopLevelConfigObject(buildConfigPaneObject(scriptState.root)));
    blocksConfigErrorMessage = "";
    blocksStepErrors = new Map();
    advancedDrafts = new Map();
    advancedOpen = new Set();
    renderBlocks();
    return true;
  }

  function renderBlocksConfigError() {
    const hasError = String(blocksConfigErrorMessage || "").trim().length > 0;
    blocksConfigError.textContent = hasError ? blocksConfigErrorMessage : "";
    blocksConfigError.classList.toggle("is-hidden", !hasError);
    renderRuntimeState(runtimeState);
  }

  function coerceNumberInput(raw, isInteger) {
    const text = String(raw || "").trim();
    if (!text) {
      return { ok: true, value: undefined, error: "" };
    }
    const parsed = isInteger ? parseInt(text, 10) : Number(text);
    if (!Number.isFinite(parsed)) {
      return { ok: false, value: undefined, error: "Getal verwacht." };
    }
    return { ok: true, value: parsed, error: "" };
  }

  function parseFieldValue(field, inputEl) {
    const raw = inputEl.type === "checkbox" ? inputEl.checked : inputEl.value;
    if (field === "start.delay_s" || field === "action.seconds" || field === "action.duration") {
      return coerceNumberInput(raw, false);
    }
    if (field === "action.slide") {
      return coerceNumberInput(raw, true);
    }
    if (field === "action.hold_until_continue") {
      return { ok: true, value: !!raw, error: "" };
    }
    const text = String(raw || "");
    if (field === "id" || field === "robot_id") {
      return { ok: true, value: text.trim(), error: "" };
    }
    return { ok: true, value: text, error: "" };
  }

  function mapAfterMove(sourceMap, from, to) {
    const next = new Map();
    sourceMap.forEach((value, key) => {
      let nextKey = Number(key);
      if (nextKey === from) {
        nextKey = to;
      } else if (from < to && nextKey > from && nextKey <= to) {
        nextKey -= 1;
      } else if (to < from && nextKey >= to && nextKey < from) {
        nextKey += 1;
      }
      next.set(nextKey, value);
    });
    return next;
  }

  function setAfterMove(sourceSet, from, to) {
    const next = new Set();
    sourceSet.forEach((key) => {
      let nextKey = Number(key);
      if (nextKey === from) {
        nextKey = to;
      } else if (from < to && nextKey > from && nextKey <= to) {
        nextKey -= 1;
      } else if (to < from && nextKey >= to && nextKey < from) {
        nextKey += 1;
      }
      next.add(nextKey);
    });
    return next;
  }

  function mapAfterDelete(sourceMap, index) {
    const next = new Map();
    sourceMap.forEach((value, key) => {
      const k = Number(key);
      if (k === index) {
        return;
      }
      if (k > index) {
        next.set(k - 1, value);
        return;
      }
      next.set(k, value);
    });
    return next;
  }

  function setAfterDelete(sourceSet, index) {
    const next = new Set();
    sourceSet.forEach((key) => {
      const k = Number(key);
      if (k === index) {
        return;
      }
      if (k > index) {
        next.add(k - 1);
        return;
      }
      next.add(k);
    });
    return next;
  }

  function mapAfterInsert(sourceMap, index, count) {
    const next = new Map();
    sourceMap.forEach((value, key) => {
      const k = Number(key);
      if (k >= index) {
        next.set(k + count, value);
        return;
      }
      next.set(k, value);
    });
    return next;
  }

  function setAfterInsert(sourceSet, index, count) {
    const next = new Set();
    sourceSet.forEach((key) => {
      const k = Number(key);
      if (k >= index) {
        next.add(k + count);
        return;
      }
      next.add(k);
    });
    return next;
  }

  function writeEditorFromScriptState(markDirty) {
    if (!scriptState || !isObject(scriptState.root)) {
      return;
    }
    const nextText = scriptStateToEditorText(scriptState);
    if (editorJson.value !== nextText) {
      editorJson.value = nextText;
      if (markDirty) {
        setDirty(true);
      }
    }
  }

  function snapshotBlocksDraftBuffers() {
    if (viewMode !== "blocks") {
      return;
    }
    blocksConfigDraft = blocksConfigJson.value;
    const advancedFields = Array.from(stepInspector.querySelectorAll(".step-advanced"));
    for (const field of advancedFields) {
      const index = Number(field.getAttribute("data-index"));
      if (!Number.isInteger(index) || index < 0) {
        continue;
      }
      advancedDrafts.set(index, field.value);
    }
  }

  function validateConfigDraft() {
    const parsed = parseConfigPaneJson(blocksConfigDraft);
    if (!parsed.ok) {
      blocksConfigErrorMessage = parsed.error;
      renderBlocksConfigError();
      return false;
    }
    blocksConfigErrorMessage = "";
    renderBlocksConfigError();
    return true;
  }

  function applyConfigDraft(options) {
    const switchToJsonOnError = options && options.switchToJsonOnError === true;
    const markDirty = options && options.markDirty === true;
    const parsed = parseConfigPaneJson(blocksConfigDraft);
    if (!parsed.ok) {
      blocksConfigErrorMessage = parsed.error;
      renderBlocksConfigError();
      if (switchToJsonOnError) {
        setViewMode("json");
      }
      return false;
    }
    blocksConfigErrorMessage = "";
    scriptState.root = applyConfigPaneObject(scriptState.root, parsed.value);
    blocksConfigDraft = formatJson(normalizeTopLevelConfigObject(buildConfigPaneObject(scriptState.root)));
    renderBlocksConfigError();
    writeEditorFromScriptState(markDirty);
    return true;
  }

  function validateAdvancedDraft(index, draftText) {
    const parsed = parseAdvancedStepJson(draftText);
    if (!parsed.ok) {
      blocksStepErrors.set(index, parsed.error);
      return false;
    }
    blocksStepErrors.delete(index);
    return true;
  }

  function applyAdvancedDraft(index, options) {
    const switchToJsonOnError = options && options.switchToJsonOnError === true;
    const markDirty = options && options.markDirty === true;
    const step = scriptState.root.steps[index];
    if (!isObject(step)) {
      return false;
    }
    const draft = advancedDrafts.has(index) ? advancedDrafts.get(index) : formatJson(step);
    const parsed = parseAdvancedStepJson(draft);
    if (!parsed.ok) {
      blocksStepErrors.set(index, parsed.error);
      renderBlocks();
      if (switchToJsonOnError) {
        setViewMode("json");
      }
      return false;
    }
    scriptState.root.steps[index] = cloneJson(parsed.value);
    advancedDrafts.set(index, formatJson(parsed.value));
    blocksStepErrors.delete(index);
    renderBlocks();
    writeEditorFromScriptState(markDirty);
    return true;
  }

  function applyAllAdvancedDrafts(options) {
    const switchToJsonOnError = options && options.switchToJsonOnError === true;
    let ok = true;
    const total = scriptState.root.steps.length;
    for (let index = 0; index < total; index += 1) {
      const step = scriptState.root.steps[index];
      if (!isObject(step)) {
        continue;
      }
      const draft = advancedDrafts.has(index) ? advancedDrafts.get(index) : formatJson(step);
      const parsed = parseAdvancedStepJson(draft);
      if (!parsed.ok) {
        blocksStepErrors.set(index, parsed.error);
        ok = false;
        continue;
      }
      blocksStepErrors.delete(index);
      scriptState.root.steps[index] = cloneJson(parsed.value);
      advancedDrafts.set(index, formatJson(scriptState.root.steps[index]));
    }
    const cleaned = new Map();
    blocksStepErrors.forEach((value, index) => {
      if (index >= 0 && index < total) {
        cleaned.set(index, value);
      }
    });
    blocksStepErrors = cleaned;
    if (!ok && switchToJsonOnError) {
      setViewMode("json");
    }
    return ok;
  }

  function syncBlocksToEditor(options) {
    if (!blocksSessionActive || !scriptState || !isObject(scriptState.root)) {
      return true;
    }
    const switchToJsonOnError = options && options.switchToJsonOnError === true;
    const markDirty = options && options.markDirty === true;
    snapshotBlocksDraftBuffers();
    const configOk = applyConfigDraft({ switchToJsonOnError: switchToJsonOnError, markDirty: false });
    const advancedOk = applyAllAdvancedDrafts({ switchToJsonOnError: switchToJsonOnError });
    renderBlocks();
    if (!configOk || !advancedOk) {
      return false;
    }
    writeEditorFromScriptState(markDirty);
    return true;
  }

  function ensureViewSyncedForAction(actionLabel) {
    if (viewMode === "blocks") {
      const ok = syncBlocksToEditor({ switchToJsonOnError: true, markDirty: true });
      if (!ok) {
        setStatus(actionLabel + " geblokkeerd: corrigeer eerst Blocks fouten.", "error");
        return false;
      }
    }
    return ensureNoBlockingBlockErrors(actionLabel);
  }

  function getStepActionType(step) {
    if (!isObject(step) || !isObject(step.action)) {
      return "";
    }
    return String(step.action.type || "").trim().toLowerCase();
  }

  function getStepStartMode(step) {
    if (!isObject(step) || !isObject(step.start)) {
      return "";
    }
    return String(step.start.mode || "").trim().toLowerCase();
  }

  function getActionMode(step) {
    if (!isObject(step) || !isObject(step.action)) {
      return "";
    }
    return String(step.action.mode || "").trim().toLowerCase();
  }

  function formatInlineText(value, maxLength) {
    const normalized = String(value || "").replace(/\s+/g, " ").trim();
    if (!normalized) {
      return "-";
    }
    if (normalized.length <= maxLength) {
      return normalized;
    }
    return normalized.slice(0, Math.max(0, maxLength - 1)).trimEnd() + "...";
  }

  function summarizeDoAction(step) {
    const mode = getActionMode(step);
    const action = isObject(step.action) ? step.action : {};
    if (!mode) {
      return "do";
    }
    if (mode === "command") {
      return "command: " + formatInlineText(action.label, 54);
    }
    if (mode === "behavior_start" || mode === "behavior_stop") {
      return mode + ": " + formatInlineText(action.behavior, 54);
    }
    if (mode === "dance") {
      return "dance: " + formatInlineText(action.dance_key, 54);
    }
    if (mode === "nao_set_eye_color") {
      const color = formatInlineText(action.color, 20);
      const duration = typeof action.duration === "undefined" ? "" : " / " + String(action.duration) + "s";
      return "eye color: " + color + duration;
    }
    if (mode === "summary_capture_stop_and_draft") {
      return "summary: stop capture + draft";
    }
    if (mode === "summary_capture_start") {
      return "summary: start capture";
    }
    return mode.replace(/_/g, " ");
  }

  function summarizeStep(step) {
    const actionType = getStepActionType(step);
    const action = isObject(step.action) ? step.action : {};
    if (actionType === "say") {
      return formatInlineText(action.text, 76);
    }
    if (actionType === "ppt") {
      const mode = getActionMode(step);
      if (mode === "goto") {
        const slide = typeof action.slide === "undefined" ? "?" : String(action.slide);
        const clickValue =
          typeof action.click !== "undefined"
            ? " / click " + String(action.click)
            : typeof action.build !== "undefined"
              ? " / click " + String(action.build)
              : "";
        return "goto slide " + slide + clickValue;
      }
      return mode ? mode.replace(/_/g, " ") : "ppt";
    }
    if (actionType === "pause") {
      return "pause " + String(typeof action.seconds === "undefined" ? 0 : action.seconds) + "s";
    }
    if (actionType === "do") {
      return summarizeDoAction(step);
    }
    return actionType || "onbekende stap";
  }

  function summarizeConfig(root) {
    const robots = isObject(root) && isObject(root.robots) ? Object.keys(root.robots) : [];
    const pptEnabled = !!(isObject(root) && isObject(root.ppt) && root.ppt.enabled);
    const defaults = isObject(root) && isObject(root.defaults) ? root.defaults : {};
    const parts = [String(robots.length) + " robot" + (robots.length === 1 ? "" : "s"), pptEnabled ? "PPT aan" : "PPT uit"];
    if (typeof defaults.request_timeout_s !== "undefined") {
      parts.push("timeout " + String(defaults.request_timeout_s) + "s");
    }
    return parts.join(" | ");
  }

  function ensureSelectedStepIndex() {
    const steps = scriptState && isObject(scriptState.root) && Array.isArray(scriptState.root.steps) ? scriptState.root.steps : [];
    if (steps.length === 0) {
      selectedStepIndex = null;
      return;
    }
    if (!Number.isInteger(selectedStepIndex) || selectedStepIndex < 0 || selectedStepIndex >= steps.length) {
      selectedStepIndex = 0;
    }
  }

  function collectUsedStepIds(steps) {
    const used = new Set();
    if (!Array.isArray(steps)) {
      return used;
    }
    for (const step of steps) {
      if (!isObject(step)) {
        continue;
      }
      const id = String(step.id || "").trim();
      if (id) {
        used.add(id);
      }
    }
    return used;
  }

  function nextUniqueStepId(baseId, usedIds) {
    let base = String(baseId || "").trim();
    if (!base) {
      base = "step";
    }
    if (!usedIds.has(base)) {
      usedIds.add(base);
      return base;
    }
    const suffixMatch = base.match(/^(.*?)(?:_(\d+))?$/);
    let stem = suffixMatch && suffixMatch[1] ? suffixMatch[1] : base;
    if (!stem) {
      stem = "step";
    }
    let counter = suffixMatch && suffixMatch[2] ? Number(suffixMatch[2]) + 1 : 2;
    if (!Number.isFinite(counter) || counter < 2) {
      counter = 2;
    }
    let candidate = stem + "_" + String(counter);
    while (usedIds.has(candidate)) {
      counter += 1;
      candidate = stem + "_" + String(counter);
    }
    usedIds.add(candidate);
    return candidate;
  }

  function ensureUniqueStepId(step, usedIds) {
    const nextStep = cloneJson(step);
    const currentId = String(nextStep.id || "").trim();
    if (currentId && !usedIds.has(currentId)) {
      usedIds.add(currentId);
      nextStep.id = currentId;
      return nextStep;
    }
    const fallbackBase = getStepActionType(nextStep) || "step";
    const base = currentId || fallbackBase;
    nextStep.id = nextUniqueStepId(base, usedIds);
    return nextStep;
  }

  function buildSnippetWithUniqueIds(snippetValue, existingSteps) {
    const parsed = parseSnippetForInsert(snippetValue);
    if (!parsed.ok) {
      return { ok: false, snippet: null, added: 0, error: parsed.error };
    }
    const usedIds = collectUsedStepIds(existingSteps);
    const uniqueSteps = parsed.steps.map((step) => ensureUniqueStepId(step, usedIds));
    return {
      ok: true,
      snippet: Array.isArray(snippetValue) ? uniqueSteps : uniqueSteps[0],
      added: uniqueSteps.length,
      error: "",
    };
  }

  function applyStepFieldUpdate(step, field, value) {
    let nextStep = updateObjectPath(step, field, value);
    if (field === "start.mode") {
      const mode = String(value || "").trim().toLowerCase();
      if (mode !== "after_prev") {
        nextStep = updateObjectPath(nextStep, "start.delay_s", undefined);
      }
    }
    if (field === "action.type") {
      const type = String(value || "").trim().toLowerCase();
      if (type !== "say" && type !== "do") {
        nextStep = updateObjectPath(nextStep, "robot_id", undefined);
      }
    }
    if (field === "action.mode") {
      const actionType = String(nextStep.action && nextStep.action.type ? nextStep.action.type : "")
        .trim()
        .toLowerCase();
      if (actionType === "ppt" && String(value || "").trim().toLowerCase() !== "goto") {
        nextStep = updateObjectPath(nextStep, "action.slide", undefined);
        nextStep = updateObjectPath(nextStep, "action.click", undefined);
        nextStep = updateObjectPath(nextStep, "action.build", undefined);
      }
    }
    return nextStep;
  }

  function createField(label, control, wide) {
    const wrapper = document.createElement("label");
    wrapper.className = "field";
    if (wide) {
      wrapper.classList.add("is-wide");
    }
    const caption = document.createElement("span");
    caption.textContent = label;
    wrapper.appendChild(caption);
    wrapper.appendChild(control);
    return wrapper;
  }

  function createInput(type, value, index, field) {
    const input = document.createElement("input");
    input.type = type;
    input.setAttribute("data-index", String(index));
    input.setAttribute("data-field", field);
    if (type === "checkbox") {
      input.checked = !!value;
    } else {
      input.value = value === null || typeof value === "undefined" ? "" : String(value);
    }
    return input;
  }

  function createTextarea(value, index, field) {
    const textarea = document.createElement("textarea");
    textarea.setAttribute("data-index", String(index));
    textarea.setAttribute("data-field", field);
    textarea.value = value === null || typeof value === "undefined" ? "" : String(value);
    return textarea;
  }

  function createSelect(value, index, field, options) {
    const select = document.createElement("select");
    select.setAttribute("data-index", String(index));
    select.setAttribute("data-field", field);
    options.forEach((item) => {
      const option = document.createElement("option");
      option.value = item.value;
      option.textContent = item.label;
      if (item.value === value) {
        option.selected = true;
      }
      select.appendChild(option);
    });
    return select;
  }

  function describeStep(step, index) {
    const sid = String(step.id || "").trim() || "zonder-id";
    const type = getStepActionType(step) || "onbekend";
    return "[" + String(index + 1) + "] " + sid + " (" + type + ")";
  }

  function pptModeLabel(mode) {
    if (mode === "next_slide") {
      return "next slide";
    }
    if (mode === "previous_slide") {
      return "previous slide";
    }
    return mode;
  }

  function createStepGrid(step, index) {
    const actionType = getStepActionType(step);
    const startMode = getStepStartMode(step);
    const actionMode = getActionMode(step);

    const grid = document.createElement("div");
    grid.className = "step-grid";

    grid.appendChild(
      createField(
        "start.mode",
        createSelect(startMode, index, "start.mode", START_MODES.map((mode) => ({ value: mode, label: mode }))),
        false
      )
    );

    if (startMode === "after_prev") {
      const delayInput = createInput("number", step.start && step.start.delay_s, index, "start.delay_s");
      delayInput.step = "0.1";
      grid.appendChild(createField("start.delay_s", delayInput, false));
    }

    grid.appendChild(
      createField(
        "action.type",
        createSelect(actionType, index, "action.type", ACTION_TYPES.map((mode) => ({ value: mode, label: mode }))),
        false
      )
    );

    if (actionType === "say" || actionType === "do") {
      grid.appendChild(createField("robot_id", createInput("text", step.robot_id, index, "robot_id"), false));
    }

    if (actionType === "say") {
      grid.appendChild(createField("action.text", createTextarea(step.action && step.action.text, index, "action.text"), true));
    } else if (actionType === "pause") {
      const secondsInput = createInput("number", step.action && step.action.seconds, index, "action.seconds");
      secondsInput.step = "0.1";
      grid.appendChild(createField("action.seconds", secondsInput, false));
    } else if (actionType === "ppt") {
      grid.appendChild(
        createField(
          "action.mode",
          createSelect(actionMode, index, "action.mode", PPT_MODES.map((mode) => ({ value: mode, label: pptModeLabel(mode) }))),
          false
        )
      );
      if (actionMode === "goto") {
        const slideInput = createInput("number", step.action && step.action.slide, index, "action.slide");
        slideInput.step = "1";
        grid.appendChild(createField("slide", slideInput, false));
      }
    } else if (actionType === "do") {
      grid.appendChild(
        createField(
          "action.mode",
          createSelect(actionMode, index, "action.mode", DO_MODES.map((mode) => ({ value: mode, label: mode }))),
          false
        )
      );
      if (actionMode === "command") {
        grid.appendChild(createField("action.label", createInput("text", step.action && step.action.label, index, "action.label"), false));
      } else if (actionMode === "behavior_start" || actionMode === "behavior_stop") {
        grid.appendChild(createField("action.behavior", createInput("text", step.action && step.action.behavior, index, "action.behavior"), false));
      } else if (actionMode === "dance") {
        grid.appendChild(createField("action.dance_key", createInput("text", step.action && step.action.dance_key, index, "action.dance_key"), false));
      } else if (actionMode === "nao_set_eye_color") {
        grid.appendChild(createField("action.color", createInput("text", step.action && step.action.color, index, "action.color"), false));
        const durationInput = createInput("number", step.action && step.action.duration, index, "action.duration");
        durationInput.step = "0.1";
        grid.appendChild(createField("action.duration", durationInput, false));
      } else if (actionMode === "summary_capture_start") {
        grid.appendChild(
          createField(
            "action.hold_until_continue",
            createInput("checkbox", step.action && step.action.hold_until_continue, index, "action.hold_until_continue"),
            false
          )
        );
      } else if (actionMode === "summary_capture_stop_and_draft") {
        grid.appendChild(createField("action.input_prompt_template", createTextarea(step.action && step.action.input_prompt_template, index, "action.input_prompt_template"), true));
        grid.appendChild(createField("action.instruction", createTextarea(step.action && step.action.instruction, index, "action.instruction"), true));
        grid.appendChild(createField("action.system_prompt", createTextarea(step.action && step.action.system_prompt, index, "action.system_prompt"), true));
        grid.appendChild(createField("action.system_prompt_file", createInput("text", step.action && step.action.system_prompt_file, index, "action.system_prompt_file"), false));
      }
    }

    return grid;
  }

  function createAdvancedWrap(options) {
    const advancedWrap = document.createElement("details");
    advancedWrap.className = options.wrapClass;
    if (options.openSet.has(options.index)) {
      advancedWrap.open = true;
    }
    advancedWrap.setAttribute("data-index", String(options.index));

    const advancedSummary = document.createElement("summary");
    advancedSummary.textContent = "Advanced JSON";
    advancedWrap.appendChild(advancedSummary);

    const advancedTextarea = document.createElement("textarea");
    advancedTextarea.className = options.textareaClass;
    advancedTextarea.setAttribute("data-index", String(options.index));
    const advancedValue = options.drafts.has(options.index)
      ? options.drafts.get(options.index)
      : formatJson(options.step);
    advancedTextarea.value = advancedValue;
    advancedWrap.appendChild(advancedTextarea);
    options.drafts.set(options.index, advancedTextarea.value);

    const errorEl = document.createElement("div");
    errorEl.className = "inline-error " + options.errorClass;
    const errorText = options.errors.get(options.index) || "";
    errorEl.textContent = errorText;
    errorEl.classList.toggle("is-hidden", !errorText);
    advancedWrap.appendChild(errorEl);

    const applyAdvanced = document.createElement("button");
    applyAdvanced.type = "button";
    applyAdvanced.textContent = "Apply advanced";
    applyAdvanced.setAttribute(options.actionAttr, "apply-advanced");
    applyAdvanced.setAttribute("data-index", String(options.index));
    advancedWrap.appendChild(applyAdvanced);

    return advancedWrap;
  }

  function createStepRowActions(index, totalSteps) {
    const actions = document.createElement("div");
    actions.className = "step-row-actions";
    [
      { id: "move-up", label: "Up", disabled: index === 0 },
      { id: "move-down", label: "Down", disabled: index >= totalSteps - 1 },
    ].forEach((cfg) => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "step-row-action-btn";
      btn.textContent = cfg.label;
      btn.setAttribute("data-step-action", cfg.id);
      btn.setAttribute("data-index", String(index));
      btn.disabled = cfg.disabled;
      actions.appendChild(btn);
    });
    return actions;
  }

  function createStepRow(step, index, totalSteps) {
    const row = document.createElement("article");
    row.className = "step-row";
    row.setAttribute("data-index", String(index));
    row.setAttribute("role", "option");
    row.setAttribute("draggable", "true");
    row.setAttribute("aria-selected", selectedStepIndex === index ? "true" : "false");
    if (selectedStepIndex === index) {
      row.classList.add("is-selected");
    }
    if (dragOverIndex === index) {
      row.classList.add("is-drag-over");
    }

    const handle = document.createElement("button");
    handle.type = "button";
    handle.className = "drag-handle";
    handle.textContent = ":::";
    handle.setAttribute("title", "Sleep om te reorderen");
    handle.setAttribute("draggable", "true");
    handle.setAttribute("data-index", String(index));
    row.appendChild(handle);

    const main = document.createElement("div");
    main.className = "step-row-main";

    const top = document.createElement("div");
    top.className = "step-row-top";

    const order = document.createElement("div");
    order.className = "step-row-index";
    order.textContent = "[" + String(index + 1) + "]";
    top.appendChild(order);

    const title = document.createElement("div");
    title.className = "step-row-title";
    title.textContent = String(step.id || "").trim() || "zonder-id";
    top.appendChild(title);
    main.appendChild(top);

    const meta = document.createElement("div");
    meta.className = "step-row-meta";

    const typeBadge = document.createElement("span");
    typeBadge.className = "step-type";
    typeBadge.textContent = getStepActionType(step) || "-";
    meta.appendChild(typeBadge);

    const startBadge = document.createElement("span");
    startBadge.className = "step-mode-badge";
    startBadge.textContent = getStepStartMode(step) || "-";
    meta.appendChild(startBadge);
    main.appendChild(meta);

    const summary = document.createElement("div");
    summary.className = "step-row-summary";
    summary.textContent = summarizeStep(step);
    main.appendChild(summary);
    row.appendChild(main);

    const status = document.createElement("div");
    status.className = "step-row-status";
    status.appendChild(createStepRowActions(index, totalSteps));
    if (getStepStartMode(step) === "after_prev" && isObject(step.start) && typeof step.start.delay_s !== "undefined") {
      const delay = document.createElement("div");
      delay.className = "step-row-delay";
      delay.textContent = String(step.start.delay_s) + "s";
      status.appendChild(delay);
    }
    row.appendChild(status);

    return row;
  }

  function createInspectorActions(index, totalSteps) {
    const actions = document.createElement("div");
    actions.className = "step-header-actions";
    [
      { id: "move-up", label: "Up", disabled: index === 0 },
      { id: "move-down", label: "Down", disabled: index >= totalSteps - 1 },
      { id: "duplicate", label: "Duplicate", disabled: false },
      { id: "delete", label: "Delete", disabled: false },
    ].forEach((cfg) => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.textContent = cfg.label;
      btn.setAttribute("data-step-action", cfg.id);
      btn.setAttribute("data-index", String(index));
      btn.disabled = cfg.disabled;
      actions.appendChild(btn);
    });
    return actions;
  }

  function createStepInspectorCard(step, index, totalSteps) {
    const card = document.createElement("article");
    card.className = "step-inspector-card";
    card.setAttribute("data-index", String(index));

    const header = document.createElement("div");
    header.className = "inspector-header";

    const titleWrap = document.createElement("div");
    titleWrap.className = "inspector-title-wrap";

    const title = document.createElement("h3");
    title.className = "inspector-title";
    title.textContent = describeStep(step, index);
    titleWrap.appendChild(title);

    const meta = document.createElement("div");
    meta.className = "inspector-meta";

    const typeBadge = document.createElement("span");
    typeBadge.className = "step-type";
    typeBadge.textContent = getStepActionType(step) || "-";
    meta.appendChild(typeBadge);

    const startBadge = document.createElement("span");
    startBadge.className = "step-mode-badge";
    startBadge.textContent = getStepStartMode(step) || "-";
    meta.appendChild(startBadge);
    titleWrap.appendChild(meta);

    const summary = document.createElement("div");
    summary.className = "inspector-summary";
    summary.textContent = summarizeStep(step);
    titleWrap.appendChild(summary);

    header.appendChild(titleWrap);
    header.appendChild(createInspectorActions(index, totalSteps));
    card.appendChild(header);
    card.appendChild(createStepGrid(step, index));
    card.appendChild(
      createAdvancedWrap({
        index: index,
        step: step,
        drafts: advancedDrafts,
        openSet: advancedOpen,
        errors: blocksStepErrors,
        wrapClass: "step-advanced-wrap",
        textareaClass: "step-advanced",
        errorClass: "step-inline-error",
        actionAttr: "data-step-action",
      })
    );
    return card;
  }

  function createTemplateInspectorCard(step, index) {
    const card = document.createElement("article");
    card.className = "template-inspector-card";
    card.setAttribute("data-index", String(index));

    const header = document.createElement("div");
    header.className = "inspector-header";

    const titleWrap = document.createElement("div");
    titleWrap.className = "inspector-title-wrap";

    const title = document.createElement("h3");
    title.className = "inspector-title";
    title.textContent = "Template " + describeStep(step, index);
    titleWrap.appendChild(title);

    const meta = document.createElement("div");
    meta.className = "inspector-meta";

    const typeBadge = document.createElement("span");
    typeBadge.className = "step-type";
    typeBadge.textContent = getStepActionType(step) || "-";
    meta.appendChild(typeBadge);

    const startBadge = document.createElement("span");
    startBadge.className = "step-mode-badge";
    startBadge.textContent = getStepStartMode(step) || "-";
    meta.appendChild(startBadge);
    titleWrap.appendChild(meta);

    const summary = document.createElement("div");
    summary.className = "inspector-summary";
    summary.textContent = summarizeStep(step);
    titleWrap.appendChild(summary);

    header.appendChild(titleWrap);
    card.appendChild(header);
    card.appendChild(createStepGrid(step, index));
    card.appendChild(
      createAdvancedWrap({
        index: index,
        step: step,
        drafts: templateAdvancedDrafts,
        openSet: templateAdvancedOpen,
        errors: templateStepErrors,
        wrapClass: "template-advanced-wrap",
        textareaClass: "template-advanced",
        errorClass: "template-inline-error",
        actionAttr: "data-template-action",
      })
    );
    return card;
  }

  function renderBlocks() {
    const canRender = blocksSessionActive && scriptState && isObject(scriptState.root);
    stepsCards.innerHTML = "";
    stepInspector.replaceChildren();
    if (!canRender) {
      blocksStepCount.textContent = "0 steps";
      blocksConfigSummary.textContent = "Geen configuratie geladen.";
      blocksEmpty.classList.remove("is-hidden");
      blocksConfigJson.value = "{}";
      const emptyInspector = document.createElement("div");
      emptyInspector.className = "step-inspector-empty";
      emptyInspector.textContent = "Kies een step om deze hier te bewerken.";
      stepInspector.appendChild(emptyInspector);
      renderBlocksConfigError();
      return;
    }

    ensureSelectedStepIndex();
    blocksConfigJson.value = blocksConfigDraft;
    blocksConfigSummary.textContent = summarizeConfig(scriptState.root);

    const steps = Array.isArray(scriptState.root.steps) ? scriptState.root.steps : [];
    blocksStepCount.textContent = String(steps.length) + " step" + (steps.length === 1 ? "" : "s");
    blocksEmpty.classList.toggle("is-hidden", steps.length > 0);
    steps.forEach((step, index) => {
      stepsCards.appendChild(createStepRow(step, index, steps.length));
    });

    if (steps.length === 0 || selectedStepIndex === null) {
      const emptyInspector = document.createElement("div");
      emptyInspector.className = "step-inspector-empty";
      emptyInspector.textContent = "Voeg een step toe om de inspector te vullen.";
      stepInspector.appendChild(emptyInspector);
    } else {
      stepInspector.appendChild(createStepInspectorCard(steps[selectedStepIndex], selectedStepIndex, steps.length));
    }

    applyRunStepHighlights(runtimeState);
    renderBlocksConfigError();
  }

  function renderTemplateCards() {
    templateInspector.replaceChildren();
    const steps = Array.isArray(templateDraftSteps) ? templateDraftSteps : [];
    templateStepCount.textContent = String(steps.length) + " blok" + (steps.length === 1 ? "" : "ken");
    if (steps.length === 0) {
      const emptyInspector = document.createElement("div");
      emptyInspector.className = "template-inspector-empty";
      emptyInspector.textContent = "Geen template blocks beschikbaar.";
      templateInspector.appendChild(emptyInspector);
      return;
    }
    steps.forEach((step, index) => {
      templateInspector.appendChild(createTemplateInspectorCard(step, index));
    });
  }

  function applyBasicFieldChange(index, field, inputEl) {
    if (!blocksSessionActive || !scriptState || !Array.isArray(scriptState.root.steps)) {
      return;
    }
    const step = scriptState.root.steps[index];
    if (!isObject(step)) {
      return;
    }

    const parsed = parseFieldValue(field, inputEl);
    if (!parsed.ok) {
      setStatus("Ongeldige waarde voor " + field + ": " + parsed.error, "error");
      return;
    }
    const nextStep = applyStepFieldUpdate(step, field, parsed.value);

    scriptState.root.steps[index] = nextStep;
    advancedDrafts.set(index, formatJson(nextStep));
    blocksStepErrors.delete(index);
    writeEditorFromScriptState(true);
    renderBlocks();
  }

  function maybeWarnWithPrevReorder(from, to) {
    if (!scriptState || !Array.isArray(scriptState.root.steps)) {
      return true;
    }
    if (!shouldWarnWithPrevReorder(scriptState.root.steps, from, to)) {
      return true;
    }
    return window.confirm("Deze reorder raakt steps met start.mode=with_prev. Toch doorgaan?");
  }

  function moveStep(from, to) {
    if (!scriptState || !Array.isArray(scriptState.root.steps)) {
      return;
    }
    if (!maybeWarnWithPrevReorder(from, to)) {
      return;
    }
    const moved = moveStepInState(scriptState, from, to);
    if (!moved.moved) {
      return;
    }
    scriptState = moved.state;
    selectedStepIndex = to;
    advancedDrafts = mapAfterMove(advancedDrafts, from, to);
    blocksStepErrors = mapAfterMove(blocksStepErrors, from, to);
    advancedOpen = setAfterMove(advancedOpen, from, to);
    writeEditorFromScriptState(true);
    renderBlocks();
  }

  function deleteStep(index) {
    scriptState.root.steps.splice(index, 1);
    advancedDrafts = mapAfterDelete(advancedDrafts, index);
    blocksStepErrors = mapAfterDelete(blocksStepErrors, index);
    advancedOpen = setAfterDelete(advancedOpen, index);
    if (!scriptState.root.steps.length) {
      selectedStepIndex = null;
    } else if (selectedStepIndex === index) {
      selectedStepIndex = Math.min(index, scriptState.root.steps.length - 1);
    } else if (selectedStepIndex !== null && selectedStepIndex > index) {
      selectedStepIndex -= 1;
    }
    writeEditorFromScriptState(true);
    renderBlocks();
  }

  function duplicateStep(index) {
    const usedIds = collectUsedStepIds(scriptState.root.steps);
    const clone = ensureUniqueStepId(scriptState.root.steps[index], usedIds);
    const insertAt = index + 1;
    scriptState.root.steps.splice(insertAt, 0, clone);
    advancedDrafts = mapAfterInsert(advancedDrafts, insertAt, 1);
    blocksStepErrors = mapAfterInsert(blocksStepErrors, insertAt, 1);
    advancedOpen = setAfterInsert(advancedOpen, insertAt, 1);
    advancedDrafts.set(insertAt, formatJson(clone));
    selectedStepIndex = insertAt;
    writeEditorFromScriptState(true);
    renderBlocks();
  }

  function handleStepAction(action, index) {
    if (!Number.isInteger(index) || index < 0) {
      return;
    }
    if (action === "move-up") {
      moveStep(index, index - 1);
      return;
    }
    if (action === "move-down") {
      moveStep(index, index + 1);
      return;
    }
    if (action === "delete") {
      deleteStep(index);
      return;
    }
    if (action === "duplicate") {
      duplicateStep(index);
      return;
    }
    if (action === "apply-advanced") {
      const ok = applyAdvancedDraft(index, { switchToJsonOnError: true, markDirty: true });
      setStatus(ok ? "Advanced JSON toegepast." : "Advanced JSON bevat fouten.", ok ? "ok" : "error");
    }
  }

  function handleTemplateAction(action, index) {
    if (!Number.isInteger(index) || index < 0) {
      return;
    }
    if (action === "apply-advanced") {
      const ok = applyTemplateAdvancedDraft(index);
      setStatus(ok ? "Template Advanced JSON toegepast." : "Template Advanced JSON bevat fouten.", ok ? "ok" : "error");
    }
  }

  function syncStepRowDragState() {
    const rows = Array.from(stepsCards.querySelectorAll(".step-row"));
    rows.forEach((row) => {
      const index = Number(row.getAttribute("data-index"));
      row.classList.toggle("is-drag-over", Number.isInteger(dragOverIndex) && index === dragOverIndex);
    });
  }

  function activateJsonTab() {
    if (viewMode === "json") {
      return;
    }
    syncBlocksToEditor({ switchToJsonOnError: false, markDirty: true });
    setViewMode("json");
  }

  function activateBlocksTab() {
    if (!ensureBlocksSessionFromEditor()) {
      return;
    }
    setViewMode("blocks");
    renderBlocks();
  }
  async function startRunFromEditor() {
    ensureRunPolling();
    if (!ensureViewSyncedForAction("Run starten")) {
      return;
    }
    renderDmStartResults([]);
    const script = parseEditorScriptForRun();
    if (!script) {
      return;
    }
    try {
      const payload = await fetchJson("/api/run/start", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ script: script }),
      });
      renderRuntimeState(payload);
      setStatus("Run gestart.", "ok");
    } catch (err) {
      const message = err && err.message ? err.message : String(err);
      setStatus("Run starten mislukt: " + message, "error");
    }
    await refreshRunState({ silent: true });
  }

  async function startDmsFromEditor() {
    if (!ensureViewSyncedForAction("DM's starten")) {
      return;
    }
    renderDmStartResults([]);
    const script = parseEditorScriptForRun();
    if (!script) {
      return;
    }
    try {
      const payload = await fetchJson("/api/dm/start", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ script: script }),
      });
      renderDmStartResults(payload && Array.isArray(payload.results) ? payload.results : []);
      const startedCount = payload && typeof payload.started_count === "number" ? payload.started_count : 0;
      const errorCount = payload && typeof payload.error_count === "number" ? payload.error_count : 0;
      const level = errorCount > 0 ? (startedCount > 0 ? "warn" : "error") : "ok";
      setStatus(
        payload && payload.message ? String(payload.message) : "DM start afgerond.",
        level
      );
    } catch (err) {
      const message = err && err.message ? err.message : String(err);
      setStatus("DM's starten mislukt: " + message, "error");
    }
  }

  async function sendNext() {
    ensureRunPolling();
    renderDmStartResults([]);
    try {
      const payload = await fetchJson("/api/run/next", { method: "POST" });
      renderRuntimeState(payload);
      setStatus("Next verstuurd.", "ok");
    } catch (err) {
      const message = err && err.message ? err.message : String(err);
      setStatus("Next mislukt: " + message, "error");
    }
    await refreshRunState({ silent: true });
  }

  async function sendAbort() {
    ensureRunPolling();
    renderDmStartResults([]);
    try {
      const payload = await fetchJson("/api/run/abort", { method: "POST" });
      renderRuntimeState(payload);
      setStatus("Abort verstuurd.", "warn");
    } catch (err) {
      const message = err && err.message ? err.message : String(err);
      setStatus("Abort mislukt: " + message, "error");
    }
    await refreshRunState({ silent: true });
  }

  function getCategoryByKey(categoryKey) {
    return catalog.find((item) => item.category_key === categoryKey) || null;
  }

  function getTemplateByKey(category, templateKey) {
    if (!category || !Array.isArray(category.templates)) {
      return null;
    }
    return category.templates.find((item) => item.template_key === templateKey) || null;
  }

  function confirmDiscardEditorChanges() {
    if (!editorDirty) {
      return true;
    }
    return window.confirm("Je hebt onopgeslagen wijzigingen. Doorgaan en wijzigingen weggooien?");
  }

  function buildTemplateSnippetFromDraft() {
    const steps = Array.isArray(templateDraftSteps) ? templateDraftSteps : [];
    if (steps.length === 0) {
      return null;
    }
    if (templateDraftIsArray) {
      return cloneJson(steps);
    }
    return cloneJson(steps[0]);
  }

  function serializeTemplateDraft() {
    const snippet = buildTemplateSnippetFromDraft();
    if (snippet === null) {
      return "";
    }
    return formatJson(snippet);
  }

  function snapshotTemplateDraftBuffers() {
    const advancedFields = Array.from(templateInspector.querySelectorAll(".template-advanced"));
    for (const field of advancedFields) {
      const index = Number(field.getAttribute("data-index"));
      if (!Number.isInteger(index) || index < 0) {
        continue;
      }
      templateAdvancedDrafts.set(index, field.value);
    }
  }

  function validateTemplateAdvancedDraft(index, draftText) {
    const parsed = parseAdvancedStepJson(draftText);
    if (!parsed.ok) {
      templateStepErrors.set(index, parsed.error);
      return false;
    }
    templateStepErrors.delete(index);
    return true;
  }

  function applyTemplateAdvancedDraft(index) {
    const step = templateDraftSteps[index];
    if (!isObject(step)) {
      return false;
    }
    const draft = templateAdvancedDrafts.has(index) ? templateAdvancedDrafts.get(index) : formatJson(step);
    const parsed = parseAdvancedStepJson(draft);
    if (!parsed.ok) {
      templateStepErrors.set(index, parsed.error);
      renderTemplateCards();
      return false;
    }
    templateDraftSteps[index] = cloneJson(parsed.value);
    templateAdvancedDrafts.set(index, formatJson(parsed.value));
    templateStepErrors.delete(index);
    renderTemplateCards();
    return true;
  }

  function applyAllTemplateAdvancedDrafts() {
    let ok = true;
    const total = templateDraftSteps.length;
    for (let index = 0; index < total; index += 1) {
      const step = templateDraftSteps[index];
      if (!isObject(step)) {
        continue;
      }
      const draft = templateAdvancedDrafts.has(index) ? templateAdvancedDrafts.get(index) : formatJson(step);
      const parsed = parseAdvancedStepJson(draft);
      if (!parsed.ok) {
        templateStepErrors.set(index, parsed.error);
        ok = false;
        continue;
      }
      templateStepErrors.delete(index);
      templateDraftSteps[index] = cloneJson(parsed.value);
      templateAdvancedDrafts.set(index, formatJson(templateDraftSteps[index]));
    }
    const cleaned = new Map();
    templateStepErrors.forEach((value, index) => {
      if (index >= 0 && index < total) {
        cleaned.set(index, value);
      }
    });
    templateStepErrors = cleaned;
    return ok;
  }

  function applyTemplateFieldChange(index, field, inputEl) {
    const step = templateDraftSteps[index];
    if (!isObject(step)) {
      return;
    }
    const parsed = parseFieldValue(field, inputEl);
    if (!parsed.ok) {
      setStatus("Ongeldige template waarde voor " + field + ": " + parsed.error, "error");
      return;
    }
    const nextStep = applyStepFieldUpdate(step, field, parsed.value);
    templateDraftSteps[index] = nextStep;
    templateAdvancedDrafts.set(index, formatJson(nextStep));
    templateStepErrors.delete(index);
    renderTemplateCards();
  }

  function previewIsEdited() {
    return serializeTemplateDraft() !== previewBaseline;
  }

  function confirmReplacePreviewIfNeeded() {
    if (!previewIsEdited()) {
      return true;
    }
    return window.confirm("Template blocks zijn aangepast. Wil je die vervangen door het nieuwe template?");
  }

  function renderSelectedTemplate(categoryKey, templateKey) {
    const category = getCategoryByKey(categoryKey);
    const template = getTemplateByKey(category, templateKey);
    if (!category || !template) {
      setStatus("Template selectie is ongeldig.", "error");
      return;
    }
    const parsed = parseSnippetForInsert(template.snippet);
    if (!parsed.ok) {
      setStatus(parsed.error, "error");
      return;
    }
    templateDraftSteps = parsed.steps;
    templateDraftIsArray = Array.isArray(template.snippet);
    templateAdvancedDrafts = new Map();
    templateAdvancedOpen = new Set();
    templateStepErrors = new Map();
    renderTemplateCards();
    previewBaseline = serializeTemplateDraft();
    currentCategoryKey = categoryKey;
    currentTemplateKey = templateKey;
  }

  function populateCategorySelect() {
    selCategory.innerHTML = "";
    catalog.forEach((category) => {
      const option = document.createElement("option");
      option.value = category.category_key;
      option.textContent = category.category_label;
      selCategory.appendChild(option);
    });
  }

  function populateTemplateSelect(categoryKey) {
    const category = getCategoryByKey(categoryKey);
    selTemplate.innerHTML = "";
    if (!category || !Array.isArray(category.templates) || category.templates.length === 0) {
      return "";
    }
    category.templates.forEach((template) => {
      const option = document.createElement("option");
      option.value = template.template_key;
      option.textContent = template.template_label;
      selTemplate.appendChild(option);
    });
    return category.templates[0].template_key;
  }

  function fileApiSupported() {
    return (
      typeof window.showOpenFilePicker === "function" &&
      typeof window.showSaveFilePicker === "function"
    );
  }

  function suggestedFileName() {
    if (currentFileLabel && currentFileLabel !== PLACEHOLDER_FILE_LABEL) {
      const clean = currentFileLabel.replace(" (voorbeeld)", "").trim();
      if (clean) {
        return clean;
      }
    }
    return "script.json";
  }

  async function writeToFileHandle(fileHandle, text) {
    const writable = await fileHandle.createWritable();
    await writable.write(text);
    await writable.close();
  }

  async function saveAs() {
    if (!ensureViewSyncedForAction("Opslaan als")) {
      return;
    }
    if (!fileApiSupported()) {
      setStatus("Deze browser ondersteunt de file picker API niet.", "error");
      return;
    }
    try {
      const fileHandle = await window.showSaveFilePicker({
        suggestedName: suggestedFileName(),
        types: [{ description: "JSON files", accept: { "application/json": [".json"] } }],
      });
      await writeToFileHandle(fileHandle, editorJson.value);
      currentFileHandle = fileHandle;
      setFileLabel(fileHandle.name || suggestedFileName());
      setDirty(false);
      setStatus("Bestand opgeslagen.", "ok");
    } catch (err) {
      if (err && err.name === "AbortError") {
        return;
      }
      const message = err && err.message ? err.message : String(err);
      setStatus("Opslaan als mislukt: " + message, "error");
    }
  }

  async function save() {
    if (!ensureViewSyncedForAction("Opslaan")) {
      return;
    }
    if (!currentFileHandle) {
      await saveAs();
      return;
    }
    try {
      await writeToFileHandle(currentFileHandle, editorJson.value);
      setDirty(false);
      setStatus("Bestand opgeslagen.", "ok");
    } catch (err) {
      if (err && err.name === "AbortError") {
        return;
      }
      const message = err && err.message ? err.message : String(err);
      setStatus("Opslaan mislukt: " + message, "error");
    }
  }

  function applyNewDefaultScript() {
    setEditorText(formatJson(cloneJson(defaultScript)), { dirty: false });
    currentFileHandle = null;
    setFileLabel(PLACEHOLDER_FILE_LABEL);
    resetBlocksSession();
    renderDmStartResults([]);
    setStatus("Nieuw script geladen met default configuratie.", "ok");
    activateBlocksTab();
  }

  async function loadFromDisk() {
    if (!fileApiSupported()) {
      setStatus("Deze browser ondersteunt de file picker API niet.", "error");
      return;
    }
    try {
      const handles = await window.showOpenFilePicker({
        multiple: false,
        types: [{ description: "JSON files", accept: { "application/json": [".json"] } }],
      });
      if (!handles || handles.length === 0) {
        return;
      }
      const fileHandle = handles[0];
      const file = await fileHandle.getFile();
      const text = await file.text();
      setEditorText(text, { dirty: false });
      currentFileHandle = fileHandle;
      setFileLabel(file.name);
      resetBlocksSession();
      renderDmStartResults([]);
      setStatus("Bestand geladen: " + file.name, "ok");
      activateBlocksTab();
    } catch (err) {
      if (err && err.name === "AbortError") {
        return;
      }
      const message = err && err.message ? err.message : String(err);
      setStatus("Laden mislukt: " + message, "error");
    }
  }

  async function loadExample(exampleName) {
    try {
      const response = await fetch("/examples/" + encodeURIComponent(exampleName), { cache: "no-store" });
      if (!response.ok) {
        throw new Error("HTTP " + response.status);
      }
      const text = await response.text();
      setEditorText(text, { dirty: false });
      currentFileHandle = null;
      setFileLabel(exampleName + " (voorbeeld)");
      resetBlocksSession();
      renderDmStartResults([]);
      setStatus("Voorbeeld geladen: " + exampleName, "ok");
      activateBlocksTab();
    } catch (err) {
      const message = err && err.message ? err.message : String(err);
      setStatus("Voorbeeld laden mislukt: " + message, "error");
    }
  }

  function insertPreviewIntoEditor() {
    snapshotTemplateDraftBuffers();
    const advancedOk = applyAllTemplateAdvancedDrafts();
    renderTemplateCards();
    if (!advancedOk) {
      setStatus("Template Advanced JSON bevat fouten.", "error");
      return;
    }
    const snippet = buildTemplateSnippetFromDraft();
    if (snippet === null) {
      setStatus("Template is leeg, niets om toe te voegen.", "warn");
      return;
    }

    if (viewMode === "blocks") {
      if (!ensureBlocksSessionFromEditor()) {
        return;
      }
      const uniqueInsert = buildSnippetWithUniqueIds(snippet, scriptState.root.steps);
      if (!uniqueInsert.ok) {
        setStatus(uniqueInsert.error, "error");
        return;
      }
      const insertAt = resolveInsertIndex(selectedStepIndex, scriptState.root.steps.length);
      const result = insertSnippetAfterSelection(scriptState, uniqueInsert.snippet, selectedStepIndex);
      if (!result.ok) {
        setStatus(result.error, "error");
        return;
      }
      scriptState = result.state;
      advancedDrafts = mapAfterInsert(advancedDrafts, insertAt, result.added);
      blocksStepErrors = mapAfterInsert(blocksStepErrors, insertAt, result.added);
      advancedOpen = setAfterInsert(advancedOpen, insertAt, result.added);
      selectedStepIndex = result.selectedIndex;
      writeEditorFromScriptState(true);
      renderBlocks();
      setStatus("Template toegevoegd aan steps (" + String(result.added) + ").", "ok");
      return;
    }

    const editorParsed = parseJsonText(editorJson.value, "Editor");
    if (!editorParsed.ok) {
      setStatus(editorParsed.error, "error");
      return;
    }
    const root = editorParsed.value;
    if (!isObject(root)) {
      setStatus("Editor root moet een JSON object zijn.", "error");
      return;
    }
    if (typeof root.steps === "undefined") {
      root.steps = [];
    }
    if (!Array.isArray(root.steps)) {
      setStatus("Editor field 'steps' moet een array zijn.", "error");
      return;
    }

    const uniqueInsert = buildSnippetWithUniqueIds(snippet, root.steps);
    if (!uniqueInsert.ok) {
      setStatus(uniqueInsert.error, "error");
      return;
    }

    let added = 0;
    if (Array.isArray(uniqueInsert.snippet)) {
      root.steps.push(...cloneJson(uniqueInsert.snippet));
      added = uniqueInsert.snippet.length;
    } else {
      root.steps.push(cloneJson(uniqueInsert.snippet));
      added = 1;
    }
    setEditorText(formatJson(root), { dirty: true });
    resetBlocksSession();
    setStatus("Template toegevoegd aan steps (" + String(added) + ").", "ok");
  }

  async function copyPreview() {
    snapshotTemplateDraftBuffers();
    const advancedOk = applyAllTemplateAdvancedDrafts();
    renderTemplateCards();
    if (!advancedOk) {
      setStatus("Template Advanced JSON bevat fouten.", "error");
      return;
    }
    const text = serializeTemplateDraft();
    if (!text.trim()) {
      setStatus("Template is leeg, niets om te kopieren.", "warn");
      return;
    }
    if (!navigator.clipboard || typeof navigator.clipboard.writeText !== "function") {
      setStatus("Clipboard API is niet beschikbaar in deze browser.", "error");
      return;
    }
    try {
      await navigator.clipboard.writeText(text);
      setStatus("Template naar clipboard gekopieerd.", "ok");
    } catch (err) {
      const message = err && err.message ? err.message : String(err);
      setStatus("Kopieren mislukt: " + message, "error");
    }
  }

  function handleCategoryChange() {
    const requestedCategoryKey = selCategory.value;
    if (!requestedCategoryKey || requestedCategoryKey === currentCategoryKey) {
      return;
    }
    if (!confirmReplacePreviewIfNeeded()) {
      selCategory.value = currentCategoryKey;
      return;
    }
    const templateKey = populateTemplateSelect(requestedCategoryKey);
    selTemplate.value = templateKey;
    renderSelectedTemplate(requestedCategoryKey, templateKey);
    setStatus("Template categorie gewijzigd.", "info");
  }

  function handleTemplateChange() {
    const requestedTemplateKey = selTemplate.value;
    if (!requestedTemplateKey || requestedTemplateKey === currentTemplateKey) {
      return;
    }
    if (!confirmReplacePreviewIfNeeded()) {
      selTemplate.value = currentTemplateKey;
      return;
    }
    renderSelectedTemplate(currentCategoryKey, requestedTemplateKey);
    setStatus("Template gewijzigd.", "info");
  }

  async function init() {
    try {
      const response = await fetch("./templates.json", { cache: "no-store" });
      if (!response.ok) {
        throw new Error("templates.json not available (HTTP " + response.status + ")");
      }
      const json = await response.json();
      if (!isObject(json) || !isObject(json.default_script) || !Array.isArray(json.catalog) || json.catalog.length === 0) {
        throw new Error("templates.json shape ongeldig");
      }

      templatesData = json;
      catalog = templatesData.catalog;
      defaultScript = templatesData.default_script;

      populateCategorySelect();
      const firstCategory = catalog[0].category_key;
      selCategory.value = firstCategory;
      const firstTemplate = populateTemplateSelect(firstCategory);
      selTemplate.value = firstTemplate;
      renderSelectedTemplate(firstCategory, firstTemplate);

      applyNewDefaultScript();
      renderRuntimeState({ status: "idle", waiting_for_next: false, waiting_reason: "none", current_step_id: "", completed_steps: 0, total_steps: 0, log_tail: [] });
      renderDmStartResults([]);
      ensureRunPolling();
      await refreshRunState({ silent: true });
      setStatus("Script Builder klaar.", "ok");
    } catch (err) {
      const message = err && err.message ? err.message : String(err);
      setStatus("Initialisatie mislukt: " + message, "error");
    }
  }
  editorJson.addEventListener("input", function () {
    setDirty(true);
    if (viewMode === "json") {
      resetBlocksSession();
      clearBlockValidationErrors();
    }
  });

  blocksConfigJson.addEventListener("input", function () {
    blocksConfigDraft = blocksConfigJson.value;
    validateConfigDraft();
  });

  btnApplyConfig.addEventListener("click", function () {
    if (!blocksSessionActive || !scriptState) {
      return;
    }
    snapshotBlocksDraftBuffers();
    const ok = applyConfigDraft({ switchToJsonOnError: true, markDirty: true });
    setStatus(ok ? "Config toegepast." : "Config JSON bevat fouten.", ok ? "ok" : "error");
    renderBlocks();
  });

  selCategory.addEventListener("change", handleCategoryChange);
  selTemplate.addEventListener("change", handleTemplateChange);
  btnTabJson.addEventListener("click", activateJsonTab);
  btnTabBlocks.addEventListener("click", activateBlocksTab);

  btnNew.addEventListener("click", function () {
    if (!confirmDiscardEditorChanges()) {
      return;
    }
    applyNewDefaultScript();
  });

  btnLoad.addEventListener("click", async function () {
    if (!confirmDiscardEditorChanges()) {
      return;
    }
    await loadFromDisk();
  });

  btnSave.addEventListener("click", async function () {
    await save();
  });

  btnSaveAs.addEventListener("click", async function () {
    await saveAs();
  });

  btnCopyTemplate.addEventListener("click", async function () {
    await copyPreview();
  });

  btnInsertTemplate.addEventListener("click", insertPreviewIntoEditor);
  btnDmStart.addEventListener("click", async function () {
    await startDmsFromEditor();
  });
  btnRunStart.addEventListener("click", async function () {
    await startRunFromEditor();
  });
  btnRunNext.addEventListener("click", async function () {
    await sendNext();
  });
  btnRunAbort.addEventListener("click", async function () {
    await sendAbort();
  });

  templateInspector.addEventListener("change", function (event) {
    const target = event.target;
    if (!(target instanceof HTMLElement)) {
      return;
    }
    const index = Number(target.getAttribute("data-index"));
    const field = String(target.getAttribute("data-field") || "");
    if (!Number.isInteger(index) || !field) {
      return;
    }
    applyTemplateFieldChange(index, field, target);
  });

  templateInspector.addEventListener("input", function (event) {
    const target = event.target;
    if (!(target instanceof HTMLElement) || !target.classList.contains("template-advanced")) {
      return;
    }
    const index = Number(target.getAttribute("data-index"));
    if (!Number.isInteger(index) || index < 0) {
      return;
    }
    templateAdvancedDrafts.set(index, target.value);
    validateTemplateAdvancedDraft(index, target.value);
    const wrap = target.closest(".template-advanced-wrap");
    const errorEl = wrap instanceof HTMLElement ? wrap.querySelector(".template-inline-error") : null;
    if (errorEl instanceof HTMLElement) {
      const message = templateStepErrors.get(index) || "";
      errorEl.textContent = message;
      errorEl.classList.toggle("is-hidden", !message);
    }
  });

  templateInspector.addEventListener("click", function (event) {
    const target = event.target;
    if (!(target instanceof HTMLElement)) {
      return;
    }
    const action = target.getAttribute("data-template-action");
    if (!action) {
      return;
    }
    handleTemplateAction(action, Number(target.getAttribute("data-index")));
  });

  templateInspector.addEventListener(
    "toggle",
    function (event) {
      const target = event.target;
      if (!(target instanceof HTMLDetailsElement) || !target.classList.contains("template-advanced-wrap")) {
        return;
      }
      const index = Number(target.getAttribute("data-index"));
      if (!Number.isInteger(index) || index < 0) {
        return;
      }
      if (target.open) {
        templateAdvancedOpen.add(index);
      } else {
        templateAdvancedOpen.delete(index);
      }
    },
    true
  );

  stepInspector.addEventListener("change", function (event) {
    const target = event.target;
    if (!(target instanceof HTMLElement)) {
      return;
    }
    const index = Number(target.getAttribute("data-index"));
    const field = String(target.getAttribute("data-field") || "");
    if (!Number.isInteger(index) || !field) {
      return;
    }
    applyBasicFieldChange(index, field, target);
  });

  stepInspector.addEventListener("input", function (event) {
    const target = event.target;
    if (!(target instanceof HTMLElement) || !target.classList.contains("step-advanced")) {
      return;
    }
    const index = Number(target.getAttribute("data-index"));
    if (!Number.isInteger(index) || index < 0) {
      return;
    }
    advancedDrafts.set(index, target.value);
    validateAdvancedDraft(index, target.value);
    renderRuntimeState(runtimeState);
    const errorEl = stepInspector.querySelector(".step-inline-error");
    if (errorEl instanceof HTMLElement) {
      const message = blocksStepErrors.get(index) || "";
      errorEl.textContent = message;
      errorEl.classList.toggle("is-hidden", !message);
    }
  });

  function isInteractiveCardElement(target) {
    if (!(target instanceof HTMLElement)) {
      return false;
    }
    return !!target.closest("input, select, textarea, button, summary, details");
  }

  stepsCards.addEventListener("click", function (event) {
    const target = event.target;
    if (!(target instanceof HTMLElement)) {
      return;
    }
    const action = target.getAttribute("data-step-action");
    if (action) {
      handleStepAction(action, Number(target.getAttribute("data-index")));
      return;
    }
    if (isInteractiveCardElement(target)) {
      return;
    }
    const row = target.closest(".step-row");
    if (!(row instanceof HTMLElement)) {
      return;
    }
    const index = Number(row.getAttribute("data-index"));
    if (!Number.isInteger(index)) {
      return;
    }
    if (selectedStepIndex !== index) {
      selectedStepIndex = index;
      renderBlocks();
    }
  });

  stepInspector.addEventListener("click", function (event) {
    const target = event.target;
    if (!(target instanceof HTMLElement)) {
      return;
    }
    const action = target.getAttribute("data-step-action");
    if (!action) {
      return;
    }
    handleStepAction(action, Number(target.getAttribute("data-index")));
  });

  stepInspector.addEventListener(
    "toggle",
    function (event) {
      const target = event.target;
      if (!(target instanceof HTMLDetailsElement) || !target.classList.contains("step-advanced-wrap")) {
        return;
      }
      const index = Number(target.getAttribute("data-index"));
      if (!Number.isInteger(index) || index < 0) {
        return;
      }
      if (target.open) {
        advancedOpen.add(index);
      } else {
        advancedOpen.delete(index);
      }
    },
    true
  );

  stepsCards.addEventListener("dragstart", function (event) {
    const target = event.target;
    if (!(target instanceof HTMLElement)) {
      return;
    }
    const row = target.closest(".step-row");
    const handle = target.closest(".drag-handle");
    if (!(row instanceof HTMLElement) || (isInteractiveCardElement(target) && !handle)) {
      return;
    }
    const index = Number(row.getAttribute("data-index"));
    if (!Number.isInteger(index)) {
      return;
    }
    dragSourceIndex = index;
    if (event.dataTransfer) {
      event.dataTransfer.effectAllowed = "move";
      event.dataTransfer.setData("text/plain", String(index));
    }
    syncStepRowDragState();
  });

  stepsCards.addEventListener("dragover", function (event) {
    if (!Number.isInteger(dragSourceIndex)) {
      return;
    }
    const target = event.target;
    if (!(target instanceof HTMLElement)) {
      return;
    }
    const row = target.closest(".step-row");
    if (!(row instanceof HTMLElement)) {
      return;
    }
    event.preventDefault();
    const index = Number(row.getAttribute("data-index"));
    if (!Number.isInteger(index)) {
      return;
    }
    if (dragOverIndex !== index) {
      dragOverIndex = index;
      syncStepRowDragState();
    }
  });

  stepsCards.addEventListener("drop", function (event) {
    if (!Number.isInteger(dragSourceIndex)) {
      return;
    }
    const target = event.target;
    if (!(target instanceof HTMLElement)) {
      return;
    }
    const row = target.closest(".step-row");
    if (!(row instanceof HTMLElement)) {
      return;
    }
    event.preventDefault();
    const to = Number(row.getAttribute("data-index"));
    if (!Number.isInteger(to)) {
      return;
    }
    const from = dragSourceIndex;
    dragSourceIndex = null;
    dragOverIndex = null;
    if (from !== to) {
      moveStep(from, to);
    } else {
      syncStepRowDragState();
    }
  });

  stepsCards.addEventListener("dragend", function () {
    dragSourceIndex = null;
    dragOverIndex = null;
    syncStepRowDragState();
  });

  quickOpenButtons.forEach(function (button) {
    button.addEventListener("click", async function () {
      const exampleName = button.getAttribute("data-example");
      if (!exampleName) {
        return;
      }
      if (!confirmDiscardEditorChanges()) {
        return;
      }
      await loadExample(exampleName);
    });
  });

  window.addEventListener("beforeunload", function (event) {
    if (!editorDirty) {
      return;
    }
    event.preventDefault();
    event.returnValue = "";
  });

  window.addEventListener("focus", function () {
    ensureRunPolling();
    refreshRunState({ silent: true });
  });

  updateFileMeta();
  setViewMode("blocks");
  init();
})();
