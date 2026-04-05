
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
  const quickOpenSelect = document.getElementById("quickOpenSelect");
  const btnCopyTemplate = document.getElementById("btnCopyTemplate");
  const btnInsertTemplate = document.getElementById("btnInsertTemplate");
  const btnDmStart = document.getElementById("btnDmStart");
  const btnRunStart = document.getElementById("btnRunStart");
  const btnRunNext = document.getElementById("btnRunNext");
  const btnRunAbort = document.getElementById("btnRunAbort");
  const btnPreloadAudio = document.getElementById("btnPreloadAudio");
  const btnPreloadPrune = document.getElementById("btnPreloadPrune");
  const summaryBanner = document.getElementById("summaryBanner");
  const btnTabJson = document.getElementById("btnTabJson");
  const btnTabConfig = document.getElementById("btnTabConfig");
  const btnTabBlocks = document.getElementById("btnTabBlocks");
  const jsonView = document.getElementById("jsonView");
  const configView = document.getElementById("configView");
  const blocksView = document.getElementById("blocksView");
  const blocksConfigSummary = document.getElementById("blocksConfigSummary");
  const blocksConfigJson = document.getElementById("blocksConfigJson");
  const btnApplyConfig = document.getElementById("btnApplyConfig");
  const btnUndoConfig = document.getElementById("btnUndoConfig");
  const btnRedoConfig = document.getElementById("btnRedoConfig");
  const blocksConfigError = document.getElementById("blocksConfigError");
  const btnApplyJson = document.getElementById("btnApplyJson");
  const jsonError = document.getElementById("jsonError");
  const btnUndoJson = document.getElementById("btnUndoJson");
  const btnRedoJson = document.getElementById("btnRedoJson");
  const editorPosition = document.getElementById("editorPosition");
  const jsonLineNumbers = document.getElementById("jsonLineNumbers");
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
  const robotStatusSection = document.getElementById("robotStatusSection");
  const robotStatusList = document.getElementById("robotStatusList");
  const templateInspector = document.getElementById("templateInspector");
  const templateStepCount = document.getElementById("templateStepCount");
  const commandLabelSuggestions = document.getElementById("commandLabelSuggestions");
  const actionDialog = document.getElementById("actionDialog");
  const actionDialogTitle = document.getElementById("actionDialogTitle");
  const actionDialogBody = document.getElementById("actionDialogBody");
  const actionDialogActions = document.getElementById("actionDialogActions");
  const actionDialogClose = document.getElementById("actionDialogClose");
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
  let runStartRequestInFlight = false;
  let preloadRequestInFlight = false;
  let lastRuntimeError = "";
  let runPollFailures = 0;
  let pollPausedByNetworkError = false;
  let dmStartResultState = [];
  let lastRunLogAutoOpenSignal = "";
  let jsonHistoryStack = [];
  let jsonHistoryIndex = -1;
  let configHistoryStack = [];
  let configHistoryIndex = -1;

  let viewMode = "blocks";
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
  let remoteCommandLabelSuggestions = [];
  let remoteCommandLabelFetchKey = "";
  let pendingCommandLabelFetchKey = "";
  let commandLabelLookup = new Map();
  let actionDialogResolve = null;
  let currentActionDialogKind = "";
  let autoRestWatchTimer = null;
  let autoRestWatchBusy = false;
  let robotStatusEntries = [];
  let connectivityDialogIssueCounts = new Map();
  let lastConnectivityDialogSignature = "";
  let dismissedConnectivityDialogSignature = "";
  let currentConnectivityDialogSource = "";
  let lastDirectConnectivitySignal = "";
  let lastSummaryAutoOpenSignal = "";
  let preflightConnectivityDialogShown = false;

  function updateJsonLineNumbers() {
    const lines = editorJson.value.split("\n");
    jsonLineNumbers.replaceChildren();
    lines.forEach((_, index) => {
      const li = document.createElement("li");
      li.textContent = String(index + 1);
      jsonLineNumbers.appendChild(li);
    });
  }

  function updateJsonEditorPosition() {
    const text = editorJson.value;
    const cursorPos = editorJson.selectionStart;
    const lines = text.substring(0, cursorPos).split("\n");
    const lineNum = lines.length;
    const colNum = lines[lines.length - 1].length + 1;
    editorPosition.textContent = `Ln ${lineNum}, Col ${colNum}`;
  }

  function pushJsonHistory() {
    jsonHistoryIndex += 1;
    if (jsonHistoryIndex < jsonHistoryStack.length) {
      jsonHistoryStack.length = jsonHistoryIndex;
    }
    jsonHistoryStack.push(editorJson.value);
    btnUndoJson.disabled = jsonHistoryIndex === 0;
    btnRedoJson.disabled = true;
  }

  function jsonUndo() {
    if (jsonHistoryIndex > 0) {
      jsonHistoryIndex -= 1;
      editorJson.value = jsonHistoryStack[jsonHistoryIndex];
      updateJsonLineNumbers();
      updateJsonEditorPosition();
      setDirty(true);
      btnUndoJson.disabled = jsonHistoryIndex === 0;
      btnRedoJson.disabled = jsonHistoryIndex >= jsonHistoryStack.length - 1;
    }
  }

  function jsonRedo() {
    if (jsonHistoryIndex < jsonHistoryStack.length - 1) {
      jsonHistoryIndex += 1;
      editorJson.value = jsonHistoryStack[jsonHistoryIndex];
      updateJsonLineNumbers();
      updateJsonEditorPosition();
      setDirty(true);
      btnUndoJson.disabled = jsonHistoryIndex === 0;
      btnRedoJson.disabled = jsonHistoryIndex >= jsonHistoryStack.length - 1;
    }
  }

  function pushConfigHistory() {
    configHistoryIndex += 1;
    if (configHistoryIndex < configHistoryStack.length) {
      configHistoryStack.length = configHistoryIndex;
    }
    configHistoryStack.push(blocksConfigJson.value);
    btnUndoConfig.disabled = configHistoryIndex === 0;
    btnRedoConfig.disabled = true;
  }

  function configUndo() {
    if (configHistoryIndex > 0) {
      configHistoryIndex -= 1;
      blocksConfigJson.value = configHistoryStack[configHistoryIndex];
      setDirty(true);
      validateConfigDraft();
      btnUndoConfig.disabled = configHistoryIndex === 0;
      btnRedoConfig.disabled = configHistoryIndex >= configHistoryStack.length - 1;
    }
  }

  function configRedo() {
    if (configHistoryIndex < configHistoryStack.length - 1) {
      configHistoryIndex += 1;
      blocksConfigJson.value = configHistoryStack[configHistoryIndex];
      setDirty(true);
      validateConfigDraft();
      btnUndoConfig.disabled = configHistoryIndex === 0;
      btnRedoConfig.disabled = configHistoryIndex >= configHistoryStack.length - 1;
    }
  }

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
    updateJsonLineNumbers();
    updateJsonEditorPosition();
    jsonHistoryStack = [text];
    jsonHistoryIndex = 0;
    btnUndoJson.disabled = true;
    btnRedoJson.disabled = true;
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
    viewMode = mode === "json" || mode === "config" || mode === "blocks" ? mode : "blocks";
    const isJson = viewMode === "json";
    const isConfig = viewMode === "config";
    const isBlocks = viewMode === "blocks";
    btnTabJson.classList.toggle("is-active", isJson);
    btnTabConfig.classList.toggle("is-active", isConfig);
    btnTabBlocks.classList.toggle("is-active", isBlocks);
    jsonView.classList.toggle("is-hidden", !isJson);
    configView.classList.toggle("is-hidden", !isConfig);
    blocksView.classList.toggle("is-hidden", !isBlocks);
  }

  function resizeBlocksConfigEditor() {
    if (!(blocksConfigJson instanceof HTMLTextAreaElement)) {
      return;
    }
    // Keep the config editor height stable in the layout and allow an internal scroll.
    blocksConfigJson.style.height = "";
    blocksConfigJson.style.overflowY = "auto";
  }

  function restoreScrollPosition(container, previousTop) {
    if (!(container instanceof HTMLElement) || !Number.isFinite(previousTop)) {
      return;
    }
    const maxScrollTop = Math.max(0, container.scrollHeight - container.clientHeight);
    if (maxScrollTop === 0) {
      container.scrollTop = Math.max(0, previousTop);
      return;
    }
    container.scrollTop = Math.min(maxScrollTop, Math.max(0, previousTop));
  }

  function generateScriptId() {
    const cryptoObj = globalThis.crypto;
    if (cryptoObj && typeof cryptoObj.randomUUID === "function") {
      return cryptoObj.randomUUID();
    }
    const stamp = Date.now().toString(16);
    const randomPart = Math.random().toString(16).slice(2, 10);
    return "script-" + stamp + "-" + randomPart;
  }

  function scriptHasSaySteps(script) {
    const steps = isObject(script) && Array.isArray(script.steps) ? script.steps : [];
    return steps.some((step) => isObject(step) && isObject(step.action) && String(step.action.type || "").trim().toLowerCase() === "say");
  }

  function syncBlocksStateFromScriptObject(script) {
    if (!blocksSessionActive) {
      return;
    }
    scriptState = { root: cloneJson(script) };
    const steps = Array.isArray(scriptState.root.steps) ? scriptState.root.steps : [];
    if (steps.length === 0) {
      selectedStepIndex = null;
    } else if (!Number.isInteger(selectedStepIndex) || selectedStepIndex < 0 || selectedStepIndex >= steps.length) {
      selectedStepIndex = 0;
    }
    blocksConfigDraft = formatJson(normalizeTopLevelConfigObject(buildConfigPaneObject(scriptState.root)));
    renderBlocks();
  }

  function persistScriptObjectToEditor(script, options) {
    if (!isObject(script)) {
      return;
    }
    editorJson.value = formatJson(script);
    if (options && options.markDirty) {
      setDirty(true);
    }
    syncBlocksStateFromScriptObject(script);
  }

  function ensureScriptIdForPreload(script) {
    if (!isObject(script) || !scriptHasSaySteps(script)) {
      return script;
    }
    const meta = isObject(script.meta) ? cloneJson(script.meta) : {};
    const existing = String(meta.script_id || "").trim();
    if (existing) {
      return script;
    }
    const next = cloneJson(script);
    next.meta = meta;
    next.meta.script_id = generateScriptId();
    persistScriptObjectToEditor(next, { markDirty: true });
    return next;
  }

  function isActionDialogOpen() {
    const hasDialogElement = typeof HTMLDialogElement !== "undefined" && actionDialog instanceof HTMLDialogElement;
    if (hasDialogElement) {
      return !!actionDialog.open;
    }
    return actionDialog instanceof HTMLElement ? actionDialog.hasAttribute("open") : false;
  }

  function populateActionDialog(options) {
    if (
      !(actionDialog instanceof HTMLElement) ||
      !(actionDialogTitle instanceof HTMLElement) ||
      !(actionDialogBody instanceof HTMLElement) ||
      !(actionDialogActions instanceof HTMLElement)
    ) {
      return false;
    }
    actionDialogTitle.textContent = options && options.title ? String(options.title) : "Actie";
    actionDialogBody.replaceChildren();
    actionDialogActions.replaceChildren();

    const introLines = Array.isArray(options && options.intro) ? options.intro : [];
    introLines.forEach((line) => {
      const p = document.createElement("p");
      p.textContent = String(line || "");
      actionDialogBody.appendChild(p);
    });

    const robots = Array.isArray(options && options.robots) ? options.robots : [];
    robots.forEach((robot) => {
      const card = document.createElement("div");
      card.className = "action-dialog-robot";

      const title = document.createElement("div");
      title.className = "action-dialog-robot-title";
      title.textContent = String(robot.title || "");
      card.appendChild(title);

      const meta = document.createElement("div");
      meta.className = "action-dialog-robot-meta";
      meta.textContent = String(robot.meta || "");
      card.appendChild(meta);

      const diff = String(robot.diff || "").trim();
      if (diff) {
        const diffEl = document.createElement("div");
        diffEl.className = "action-dialog-robot-diff";
        diffEl.textContent = diff;
        card.appendChild(diffEl);
      }

      actionDialogBody.appendChild(card);
    });

    const buttons = Array.isArray(options && options.buttons) ? options.buttons : [];
    buttons.forEach((item) => {
      const button = document.createElement("button");
      button.type = "button";
      button.textContent = String(item.label || item.id || "Kies");
      button.dataset.dialogAction = String(item.id || "");
      if (item && item.tone === "primary") {
        button.classList.add("primary");
      }
      button.addEventListener("click", function () {
        closeActionDialog(String(item.id || "cancel"));
      });
      actionDialogActions.appendChild(button);
    });
    return true;
  }

  function showActionDialogElement() {
    const hasDialogElement = typeof HTMLDialogElement !== "undefined" && actionDialog instanceof HTMLDialogElement;
    if (hasDialogElement && typeof actionDialog.showModal === "function") {
      try {
        if (!actionDialog.open) {
          actionDialog.showModal();
        }
        return;
      } catch (_err) {
        // Fall through to plain open attribute for jsdom/older browsers.
      }
    }
    if (actionDialog instanceof HTMLElement) {
      actionDialog.setAttribute("open", "");
    }
  }

  function closeActionDialog(result) {
    const hasDialogElement = typeof HTMLDialogElement !== "undefined" && actionDialog instanceof HTMLDialogElement;
    if (hasDialogElement) {
      if (typeof actionDialog.close === "function" && actionDialog.open) {
        try {
          actionDialog.close();
        } catch (_err) {
          actionDialog.removeAttribute("open");
        }
      } else {
        actionDialog.removeAttribute("open");
      }
    }
    currentActionDialogKind = "";
    if (typeof actionDialogResolve === "function") {
      const resolve = actionDialogResolve;
      actionDialogResolve = null;
      resolve(result || "cancel");
    }
  }

  function openActionDialog(options) {
    if (!populateActionDialog(options)) {
      return Promise.resolve("cancel");
    }
    return new Promise((resolve) => {
      currentActionDialogKind = options && options.kind ? String(options.kind) : "choice";
      actionDialogResolve = resolve;
      showActionDialogElement();
    });
  }

  function openConnectivityDialog(options) {
    if (typeof actionDialogResolve === "function" && currentActionDialogKind !== "connectivity") {
      return false;
    }
    if (!populateActionDialog({ ...options, kind: "connectivity" })) {
      return false;
    }
    currentActionDialogKind = "connectivity";
    showActionDialogElement();
    return true;
  }

  function dismissConnectivityDialog() {
    if (lastConnectivityDialogSignature) {
      dismissedConnectivityDialogSignature = lastConnectivityDialogSignature;
    }
    closeActionDialog("dismiss");
  }

  function currentScriptForAutoRestWatch() {
    if (blocksSessionActive && scriptState && isObject(scriptState.root) && !hasPendingBlockErrors()) {
      return cloneJson(scriptState.root);
    }
    const parsed = parseJsonText(editorJson.value, "Editor");
    if (!parsed.ok || !isObject(parsed.value)) {
      return null;
    }
    return parsed.value;
  }

  function collectScriptDmTargets(script) {
    const robots = isObject(script) && isObject(script.robots) ? script.robots : {};
    return Object.keys(robots)
      .sort((left, right) => left.localeCompare(right, "nl", { sensitivity: "base" }))
      .map((robotId) => {
        const robotCfg = robots[robotId];
        return {
          robot_id: String(robotId || "?"),
          dm_url: isObject(robotCfg) ? String(robotCfg.dm_url || "").trim() : "",
          preset: isObject(robotCfg) ? String(robotCfg.preset || "").trim() : "",
        };
      })
      .filter((target) => target.dm_url || target.robot_id);
  }

  function normalizeConnectivityIssues(rawIssues) {
    return (Array.isArray(rawIssues) ? rawIssues : [])
      .map((issue) => {
        if (!isObject(issue)) {
          return null;
        }
        const issueKey = String(issue.issue_key || "").trim();
        if (!issueKey) {
          return null;
        }
        return {
          issue_key: issueKey,
          issue_type: String(issue.issue_type || "").trim() || "dm_local",
          severity: String(issue.severity || "error").trim() || "error",
          message: String(issue.message || "").trim(),
          source: String(issue.source || "").trim(),
          first_seen_at: String(issue.first_seen_at || "").trim(),
          active: !!issue.active,
        };
      })
      .filter(Boolean);
  }

  function connectivityDialogCounterKey(entry, issue) {
    return [String(entry && entry.robot_id ? entry.robot_id : "?"), String(entry && entry.dm_url ? entry.dm_url : ""), String(issue.issue_key || "")].join("|");
  }

  function connectivityDialogRobotMeta(entry) {
    const parts = [];
    if (entry && entry.dm_url) {
      parts.push(String(entry.dm_url));
    }
    if (entry && entry.preset) {
      parts.push("preset=" + String(entry.preset));
    }
    return parts.join(" | ");
  }

  function buildConnectivityDialogRobot(entry, issues, fallbackMessage) {
    const issueLines = (Array.isArray(issues) ? issues : [])
      .map((issue) => String(issue && issue.message ? issue.message : "").trim())
      .filter((text) => text);
    const summary = issueLines.length > 0 ? issueLines.join(" | ") : String(fallbackMessage || "").trim();
    if (!summary) {
      return null;
    }
    return {
      title: String(entry && entry.robot_id ? entry.robot_id : "?"),
      meta: connectivityDialogRobotMeta(entry),
      diff: summary,
    };
  }

  function isConnectivityFailureMessage(message) {
    const text = String(message || "").trim().toLowerCase();
    if (!text) {
      return false;
    }
    return [
      "niet bereikbaar",
      "unreachable",
      "connection refused",
      "refused",
      "timed out",
      "timeout",
      "socket",
      "dns",
      "no route",
      "host unreachable",
      "base down",
      "behavior manager",
      "nao tcp",
      "verbinding",
      "connector",
    ].some((needle) => text.includes(needle));
  }

  function closeConnectivityDialog(options) {
    const preserveDismissal = !!(options && options.preserveDismissal);
    if (!preserveDismissal) {
      dismissedConnectivityDialogSignature = "";
    }
    lastConnectivityDialogSignature = "";
    currentConnectivityDialogSource = "";
    if (currentActionDialogKind === "connectivity" && isActionDialogOpen()) {
      closeActionDialog(options && options.result ? options.result : "cancel");
    }
  }

  function showConnectivityDialogState(dialogState) {
    if (!dialogState || !Array.isArray(dialogState.robots) || dialogState.robots.length === 0) {
      closeConnectivityDialog();
      return false;
    }
    const signature = String(dialogState.signature || "").trim();
    if (!signature) {
      closeConnectivityDialog();
      return false;
    }
    lastConnectivityDialogSignature = signature;
    currentConnectivityDialogSource = String(dialogState.source || "poll");
    if (dismissedConnectivityDialogSignature && dismissedConnectivityDialogSignature === signature) {
      if (currentActionDialogKind === "connectivity" && isActionDialogOpen()) {
        closeActionDialog("dismiss");
      }
      return false;
    }

    // Only show the preflight connectivity popup once per preflight session.
    if (runtimeState && String(runtimeState.status || "").toLowerCase() === "preflight") {
      if (preflightConnectivityDialogShown) {
        return false;
      }
    }

    const opened = openConnectivityDialog({
      title: dialogState.title || "Verbindingsverlies",
      intro: Array.isArray(dialogState.intro) ? dialogState.intro : [],
      robots: dialogState.robots,
      buttons: [],
    });

    if (opened && runtimeState && String(runtimeState.status || "").toLowerCase() === "preflight") {
      preflightConnectivityDialogShown = true;
    }

    return opened;
  }

  function buildPollingConnectivityDialogState(entries) {
    const status = runtimeState && runtimeState.status ? String(runtimeState.status) : "idle";
    if (!isActiveRunStatus(status)) {
      connectivityDialogIssueCounts.clear();
      if (currentConnectivityDialogSource === "poll") {
        closeConnectivityDialog();
      }
      return null;
    }
    const activeCounterKeys = new Set();
    const signatureParts = [];
    const robots = [];
    (Array.isArray(entries) ? entries : []).forEach((entry) => {
      if (entry && entry.nao_enabled === false) {
        return;
      }
      const activeIssues = normalizeConnectivityIssues(entry && entry.connectivity_issues).filter((issue) => issue.active);
      const visibleIssues = [];
      activeIssues.forEach((issue) => {
        const counterKey = connectivityDialogCounterKey(entry, issue);
        activeCounterKeys.add(counterKey);
        const current = Number(connectivityDialogIssueCounts.get(counterKey) || 0) + 1;
        connectivityDialogIssueCounts.set(counterKey, current);
        if (current >= 2) {
          visibleIssues.push(issue);
          signatureParts.push(
            [
              String(entry && entry.robot_id ? entry.robot_id : "?"),
              String(entry && entry.dm_url ? entry.dm_url : ""),
              String(issue.issue_key || ""),
              String(issue.first_seen_at || ""),
              String(issue.message || ""),
            ].join("|")
          );
        }
      });
      const robot = buildConnectivityDialogRobot(entry, visibleIssues, entry && entry.error ? entry.error : "");
      if (robot) {
        robots.push(robot);
      }
    });
    [...connectivityDialogIssueCounts.keys()].forEach((counterKey) => {
      if (!activeCounterKeys.has(counterKey)) {
        connectivityDialogIssueCounts.delete(counterKey);
      }
    });
    if (robots.length === 0) {
      if (currentConnectivityDialogSource === "poll") {
        closeConnectivityDialog();
      }
      return null;
    }
    return {
      source: "poll",
      title: "Verbindingsverlies tijdens run",
      intro: [
        status === "preflight"
          ? "Preflight ziet verbindingsproblemen bij een of meer robots."
          : "Actieve run ziet verbindingsproblemen bij een of meer robots.",
      ],
      robots: robots,
      signature: "poll|" + signatureParts.sort().join("||"),
    };
  }

  function buildDirectConnectivityDialogState(message, script) {
    const normalizedMessage = String(message || "").trim();
    if (!isConnectivityFailureMessage(normalizedMessage)) {
      return null;
    }
    const signatureParts = [];
    let robots = [];
    const activeRobotEntries = (Array.isArray(robotStatusEntries) ? robotStatusEntries : [])
      .map((entry) => {
        const issues = normalizeConnectivityIssues(entry && entry.connectivity_issues).filter((issue) => issue.active);
        return { entry: entry, issues: issues };
      })
      .filter((item) => item.issues.length > 0);
    if (activeRobotEntries.length > 0) {
      robots = activeRobotEntries
        .map((item) => {
          item.issues.forEach((issue) => {
            signatureParts.push(
              [
                String(item.entry && item.entry.robot_id ? item.entry.robot_id : "?"),
                String(item.entry && item.entry.dm_url ? item.entry.dm_url : ""),
                String(issue.issue_key || ""),
                String(issue.first_seen_at || ""),
                String(issue.message || ""),
              ].join("|")
            );
          });
          return buildConnectivityDialogRobot(item.entry, item.issues, normalizedMessage);
        })
        .filter(Boolean);
    } else {
      const targets = collectScriptDmTargets(script);
      const fallbackTargets = targets.length > 0 ? targets : [{ robot_id: "robot", dm_url: "", preset: "" }];
      robots = fallbackTargets
        .map((target) => {
          signatureParts.push([String(target.robot_id || "?"), String(target.dm_url || ""), normalizedMessage].join("|"));
          return buildConnectivityDialogRobot(target, [], normalizedMessage);
        })
        .filter(Boolean);
    }
    if (robots.length === 0) {
      return null;
    }
    return {
      source: "direct",
      title: "Verbindingsverlies",
      intro: ["Run-start faalde door een verbindings- of procesverlies."],
      robots: robots,
      signature: "direct|" + signatureParts.sort().join("||"),
    };
  }

  function syncConnectivityDialogFromWatch(entries) {
    const dialogState = buildPollingConnectivityDialogState(entries);
    if (!dialogState) {
      return;
    }
    showConnectivityDialogState(dialogState);
  }

  function maybeShowDirectConnectivityDialog(message, script, signalSeed) {
    const normalizedMessage = String(message || "").trim();
    if (!isConnectivityFailureMessage(normalizedMessage)) {
      return false;
    }
    const signal = [String(signalSeed || ""), normalizedMessage].join("|");
    if (signal && signal === lastDirectConnectivitySignal) {
      return true;
    }
    lastDirectConnectivitySignal = signal;
    const dialogState = buildDirectConnectivityDialogState(normalizedMessage, script);
    if (!dialogState) {
      return false;
    }
    return showConnectivityDialogState(dialogState);
  }

  function maybeClearDirectConnectivityDialog(state) {
    const runError = state && typeof state.last_error === "string" ? state.last_error.trim() : "";
    if (runError && isConnectivityFailureMessage(runError)) {
      return;
    }
    lastDirectConnectivitySignal = "";
    if (currentConnectivityDialogSource === "direct") {
      closeConnectivityDialog();
    }
  }

  function normalizeRobotStatusEntry(entry) {
    const awake = entry && isObject(entry.awake) ? entry.awake : {};
    const posture = entry && isObject(entry.posture) ? entry.posture : {};
    const connectivityIssues = normalizeConnectivityIssues(entry && entry.connectivity_issues);
    const naoEnabledRaw = entry && typeof entry.nao_enabled === "boolean" ? entry.nao_enabled : null;
    const naoEnabled = naoEnabledRaw === null ? true : !!naoEnabledRaw;
    const virtualRobot = !!(entry && entry.virtual_robot) || !naoEnabled;
    return {
      robot_id: String((entry && entry.robot_id) || "?"),
      dm_url: String((entry && entry.dm_url) || "").trim(),
      preset: String((entry && entry.preset) || "").trim(),
      nao_enabled: naoEnabled,
      virtual_robot: virtualRobot,
      ok: !!(entry && entry.ok),
      reachable: !!(entry && entry.reachable),
      error: naoEnabled ? String((entry && entry.error) || "").trim() : "",
      awake_value: awake && awake.ok && typeof awake.is_awake === "boolean" ? awake.is_awake : null,
      posture_value: posture && posture.ok && posture.posture ? String(posture.posture) : "",
      connectivity_issues: connectivityIssues,
    };
  }

  function formatRobotModeText(entry) {
    if (!entry || entry.nao_enabled !== false) {
      return "";
    }
    return "modus: virtuele robot";
  }

  function formatRobotMotorText(entry) {
    if (entry && entry.nao_enabled === false) {
      return "motoren: disabled";
    }
    if (!entry || entry.ok !== true) {
      return "motoren: niet beschikbaar";
    }
    if (entry.awake_value == null) {
      return "motoren: onbekend";
    }
    return entry.awake_value ? "motoren: wakker" : "motoren: rust";
  }

  function formatRobotPostureText(entry) {
    if (entry && entry.nao_enabled === false) {
      return "posture: disabled";
    }
    if (!entry || entry.ok !== true) {
      return "posture: niet beschikbaar";
    }
    if (!entry.posture_value) {
      return "posture: onbekend";
    }
    return "posture: " + entry.posture_value;
  }

  function renderRobotStatusSection(entries) {
    const status = runtimeState && runtimeState.status ? String(runtimeState.status) : "idle";
    if (!isActiveRunStatus(status)) {
      robotStatusEntries = [];
      if (robotStatusSection instanceof HTMLElement) {
        robotStatusSection.classList.add("is-hidden");
      }
      if (robotStatusList instanceof HTMLElement) {
        robotStatusList.replaceChildren();
      }
      return;
    }
    robotStatusEntries = Array.isArray(entries) ? entries.slice() : [];
    if (!(robotStatusSection instanceof HTMLElement) || !(robotStatusList instanceof HTMLElement)) {
      return;
    }
    robotStatusList.replaceChildren();
    if (robotStatusEntries.length === 0) {
      robotStatusSection.classList.add("is-hidden");
      return;
    }
    robotStatusEntries.forEach((entry) => {
      const card = document.createElement("div");
      card.className = "robot-status-card";

      const title = document.createElement("div");
      title.className = "robot-status-title";
      title.textContent = String(entry.robot_id || "?");
      card.appendChild(title);

      const metaParts = [];
      if (entry.dm_url) {
        metaParts.push(entry.dm_url);
      }
      if (entry.preset) {
        metaParts.push("preset=" + entry.preset);
      }
      if (metaParts.length > 0) {
        const meta = document.createElement("div");
        meta.className = "robot-status-meta";
        meta.textContent = metaParts.join(" | ");
        card.appendChild(meta);
      }

      const lines = document.createElement("div");
      lines.className = "robot-status-lines";
      [formatRobotModeText(entry), formatRobotMotorText(entry), formatRobotPostureText(entry)].filter(Boolean).forEach((text) => {
        const line = document.createElement("div");
        line.className = "robot-status-line";
        line.textContent = text;
        lines.appendChild(line);
      });
      card.appendChild(lines);

      if (entry.error) {
        const error = document.createElement("div");
        error.className = "robot-status-error";
        error.textContent = entry.error;
        card.appendChild(error);
      }

      robotStatusList.appendChild(card);
    });
    robotStatusSection.classList.remove("is-hidden");
  }

  async function refreshAutoRestWatch(options) {
    if (autoRestWatchBusy) {
      return;
    }
    const silent = !!(options && options.silent);
    const status = runtimeState && runtimeState.status ? String(runtimeState.status) : "idle";
    if (!isActiveRunStatus(status)) {
      renderRobotStatusSection([]);
      if (currentConnectivityDialogSource === "poll") {
        closeConnectivityDialog();
      }
      return;
    }
    const script = currentScriptForAutoRestWatch();
    if (!script) {
      renderRobotStatusSection([]);
      if (currentConnectivityDialogSource === "poll") {
        closeConnectivityDialog();
      }
      return;
    }
    autoRestWatchBusy = true;
    try {
      const payload = await fetchJson("/api/auto_rest_watch/status", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ script: script }),
      });
      const robots = Array.isArray(payload && payload.robots) ? payload.robots : [];
      const normalizedEntries = robots
        .map((entry) => normalizeRobotStatusEntry(entry))
        .filter((entry) => entry.dm_url || entry.robot_id);
      renderRobotStatusSection(normalizedEntries);
      syncConnectivityDialogFromWatch(normalizedEntries);
    } catch (err) {
      if (currentConnectivityDialogSource === "poll") {
        closeConnectivityDialog();
      }
      if (!silent) {
        const message = err && err.message ? err.message : String(err);
        setStatus("Auto-rest status ophalen mislukt: " + message, "error");
      }
    } finally {
      autoRestWatchBusy = false;
    }
  }

  function stopAutoRestWatchPolling() {
    if (autoRestWatchTimer !== null) {
      window.clearInterval(autoRestWatchTimer);
      autoRestWatchTimer = null;
    }
  }

  function ensureAutoRestWatchPolling() {
    if (autoRestWatchTimer !== null) {
      return;
    }
    autoRestWatchTimer = window.setInterval(function () {
      if (document.visibilityState === "hidden") {
        return;
      }
      refreshAutoRestWatch({ silent: true });
    }, 5000);
  }

  function syncAutoRestWatchPolling(state) {
    const status = state && state.status ? String(state.status) : "idle";
    if (!isActiveRunStatus(status)) {
      stopAutoRestWatchPolling();
      renderRobotStatusSection([]);
      return;
    }
    const shouldRefreshImmediately = autoRestWatchTimer === null;
    ensureAutoRestWatchPolling();
    if (shouldRefreshImmediately) {
      void refreshAutoRestWatch({ silent: true });
    }
  }

  function describeProfile(profile) {
    if (!isObject(profile)) {
      return "-";
    }
    const summary = String(profile.summary || "").trim();
    if (summary) {
      return summary;
    }
    return String(profile.engine || "onbekend");
  }

  function diffProfileFields(currentProfile, existingProfile) {
    if (!isObject(currentProfile) || !isObject(existingProfile)) {
      return "";
    }
    const currentDetails = isObject(currentProfile.details) ? currentProfile.details : {};
    const existingDetails = isObject(existingProfile.details) ? existingProfile.details : {};
    const keys = Array.from(new Set([...Object.keys(currentDetails), ...Object.keys(existingDetails)]));
    const diffs = [];
    keys.forEach((key) => {
      const left = currentDetails[key];
      const right = existingDetails[key];
      if (JSON.stringify(left) !== JSON.stringify(right)) {
        diffs.push(String(key));
      }
    });
    return diffs.length > 0 ? "Verschil in: " + diffs.join(", ") : "";
  }

  function normalizeCommandLabelRaw(value) {
    return String(value || "")
      .trim()
      .replace(/\\_/g, "_")
      .replace(/\s+/g, " ");
  }

  function commandLabelDisplay(value) {
    return normalizeCommandLabelRaw(value).replace(/_/g, " ");
  }

  function commandLabelMatchKey(value) {
    return commandLabelDisplay(value)
      .trim()
      .replace(/\s+/g, " ")
      .toLowerCase();
  }

  function normalizeCommandLabelList(values) {
    const seen = new Set();
    const labels = [];
    (Array.isArray(values) ? values : []).forEach((value) => {
      const label = normalizeCommandLabelRaw(value);
      if (!label) {
        return;
      }
      const key = commandLabelMatchKey(label);
      if (seen.has(key)) {
        return;
      }
      seen.add(key);
      labels.push(label);
    });
    labels.sort((left, right) => commandLabelDisplay(left).localeCompare(commandLabelDisplay(right), "nl", { sensitivity: "base" }));
    return labels;
  }

  function resolveCommandLabelValue(rawValue) {
    const label = normalizeCommandLabelRaw(rawValue);
    if (!label) {
      return "";
    }
    const key = commandLabelMatchKey(label);
    if (commandLabelLookup.has(key)) {
      return commandLabelLookup.get(key);
    }
    return label;
  }

  function collectCommandLabelsFromSteps(steps, target) {
    if (!Array.isArray(steps) || !(target instanceof Set)) {
      return;
    }
    steps.forEach((step) => {
      if (!isObject(step)) {
        return;
      }
      if (getStepActionType(step) !== "do" || getActionMode(step) !== "command") {
        return;
      }
      const label = String(step.action && step.action.label ? step.action.label : "").trim();
      if (label) {
        target.add(label);
      }
    });
  }

  function collectCommandLabelsFromCatalog(target) {
    if (!(target instanceof Set)) {
      return;
    }
    catalog.forEach((category) => {
      if (!isObject(category) || !Array.isArray(category.templates)) {
        return;
      }
      category.templates.forEach((template) => {
        if (!isObject(template)) {
          return;
        }
        const parsed = parseSnippetForInsert(template.snippet);
        if (!parsed.ok) {
          return;
        }
        collectCommandLabelsFromSteps(parsed.steps, target);
      });
    });
  }

  function renderCommandLabelSuggestionOptions() {
    if (!(commandLabelSuggestions instanceof HTMLDataListElement)) {
      return;
    }
    const labels = new Set();
    if (scriptState && isObject(scriptState.root)) {
      collectCommandLabelsFromSteps(scriptState.root.steps, labels);
    }
    collectCommandLabelsFromSteps(templateDraftSteps, labels);
    collectCommandLabelsFromCatalog(labels);
    remoteCommandLabelSuggestions.forEach((label) => {
      labels.add(label);
    });

    commandLabelLookup = new Map();
    const options = normalizeCommandLabelList(Array.from(labels)).map((label) => {
      const display = commandLabelDisplay(label);
      commandLabelLookup.set(commandLabelMatchKey(label), label);
      const option = document.createElement("option");
      option.value = display;
      return option;
    });
    commandLabelSuggestions.replaceChildren(...options);
  }

  function commandLabelFetchSourceKey() {
    if (!scriptState || !isObject(scriptState.root) || !isObject(scriptState.root.robots)) {
      return "";
    }
    const parts = [];
    Object.keys(scriptState.root.robots)
      .sort((left, right) => left.localeCompare(right, "nl", { sensitivity: "base" }))
      .forEach((robotId) => {
        const robotCfg = scriptState.root.robots[robotId];
        if (!isObject(robotCfg)) {
          return;
        }
        const dmUrl = String(robotCfg.dm_url || "").trim();
        if (dmUrl) {
          parts.push(String(robotId) + ":" + dmUrl);
        }
      });
    return parts.join("|");
  }

  async function ensureRemoteCommandLabelSuggestions() {
    const sourceKey = commandLabelFetchSourceKey();
    if (!sourceKey) {
      if (remoteCommandLabelSuggestions.length > 0 || remoteCommandLabelFetchKey) {
        remoteCommandLabelSuggestions = [];
        remoteCommandLabelFetchKey = "";
        pendingCommandLabelFetchKey = "";
        renderCommandLabelSuggestionOptions();
      }
      return;
    }
    if (sourceKey === remoteCommandLabelFetchKey || sourceKey === pendingCommandLabelFetchKey) {
      return;
    }
    pendingCommandLabelFetchKey = sourceKey;
    try {
      const payload = await fetchJson("/api/cmdrec/labels", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ script: scriptState ? scriptState.root : null }),
      });
      if (pendingCommandLabelFetchKey !== sourceKey || commandLabelFetchSourceKey() !== sourceKey) {
        return;
      }
      remoteCommandLabelSuggestions = normalizeCommandLabelList(payload.labels);
      remoteCommandLabelFetchKey = sourceKey;
    } catch (_err) {
      if (pendingCommandLabelFetchKey !== sourceKey || commandLabelFetchSourceKey() !== sourceKey) {
        return;
      }
      remoteCommandLabelSuggestions = [];
      remoteCommandLabelFetchKey = sourceKey;
    } finally {
      if (pendingCommandLabelFetchKey === sourceKey) {
        pendingCommandLabelFetchKey = "";
      }
      renderCommandLabelSuggestionOptions();
    }
  }

  function ensureCommandLabelSuggestions() {
    renderCommandLabelSuggestionOptions();
    void ensureRemoteCommandLabelSuggestions();
  }

  function isActiveRunStatus(status) {
    return status === "preflight" || status === "running" || status === "waiting";
  }

  function setPreloadRequestBusy(isBusy) {
    preloadRequestInFlight = !!isBusy;
    setRuntimeButtons(runtimeState);
  }

  function setRunStartRequestBusy(isBusy) {
    runStartRequestInFlight = !!isBusy;
    setRuntimeButtons(runtimeState);
  }

  function setRuntimeButtons(state) {
    const status = state && state.status ? String(state.status) : "idle";
    const waiting = !!(state && state.waiting_for_next);
    const summaryActive = !!(state && state.summary_active);
    const summaryWaiting = !!(state && state.summary_waiting);
    const blockedByBlocks = hasBlockingBlockErrors(blocksConfigErrorMessage, blocksStepErrors);
    const activeRun = isActiveRunStatus(status);
    btnRunStart.textContent = preloadRequestInFlight
      ? "Wacht op preload..."
      : runStartRequestInFlight
        ? "Starten..."
        : "Start";
    if (btnPreloadAudio) {
      btnPreloadAudio.textContent = preloadRequestInFlight ? "Preload bezig..." : "Preload audio...";
    }
    btnDmStart.disabled = blockedByBlocks;
    btnRunStart.disabled =
      !(
        status === "idle" ||
        status === "completed" ||
        status === "aborted" ||
        status === "failed"
      ) || blockedByBlocks || preloadRequestInFlight || runStartRequestInFlight || summaryActive;
    btnRunNext.disabled = !waiting || summaryWaiting;
    btnRunAbort.disabled = !activeRun;
    if (btnPreloadAudio) {
      btnPreloadAudio.disabled = blockedByBlocks || activeRun || preloadRequestInFlight || runStartRequestInFlight;
    }
    if (btnPreloadPrune) {
      btnPreloadPrune.disabled = activeRun || preloadRequestInFlight || runStartRequestInFlight;
    }
  }

  function renderRuntimeActionCards(cards) {
    dmStartResultState = Array.isArray(cards) ? cards.slice() : [];
    dmStartResults.replaceChildren();
    if (dmStartResultState.length === 0) {
      dmStartResults.classList.add("is-hidden");
      return;
    }
    dmStartResultState.forEach(function (item) {
      const card = document.createElement("div");
      const tone = String((item && item.tone) || "info").toLowerCase();
      card.className = "dm-start-result";
      if (tone === "ok") {
        card.classList.add("is-ok");
      } else if (tone === "warn") {
        card.classList.add("is-warn");
      } else if (tone === "error") {
        card.classList.add("is-error");
      }

      const title = document.createElement("div");
      title.className = "dm-start-result-title";
      title.textContent = item && item.title ? String(item.title) : "?";
      card.appendChild(title);

      const metaText = item && item.meta ? String(item.meta) : "";
      if (metaText) {
        const meta = document.createElement("div");
        meta.className = "dm-start-result-meta";
        meta.textContent = metaText;
        card.appendChild(meta);
      }

      const body = document.createElement("div");
      body.className = "dm-start-result-message";
      body.textContent = item && item.message ? String(item.message) : "";
      card.appendChild(body);

      dmStartResults.appendChild(card);
    });
    dmStartResults.classList.remove("is-hidden");
  }

  function renderDmStartResults(results) {
    const cards = (Array.isArray(results) ? results : []).map(function (item) {
      const started = !!(item && item.started);
      const robotId = item && item.robot_id ? String(item.robot_id) : "?";
      const dmUrl = item && item.dm_url ? String(item.dm_url) : "";
      const preset = item && item.preset ? String(item.preset) : "";
      const parts = [];
      if (dmUrl) {
        parts.push(dmUrl);
      }
      if (preset) {
        parts.push("preset=" + preset);
      }
      return {
        tone: started ? "ok" : "error",
        title: robotId + (started ? " gestart" : " niet gestart"),
        meta: parts.join(" | "),
        message: item && item.message ? String(item.message) : item && item.error ? String(item.error) : "",
      };
    });
    renderRuntimeActionCards(cards);
  }

  function renderPreloadResults(result) {
    const robots = result && Array.isArray(result.robots) ? result.robots : [];
    const robotLookup = new Map(robots.map((item) => [String(item.robot_id || ""), item]));
    const generation = result && Array.isArray(result.robots_generation) ? result.robots_generation : [];
    const cards = generation.map(function (item) {
      const robotId = String((item && item.robot_id) || "?");
      const robotStatus = robotLookup.get(robotId) || {};
      const status = String((item && item.status) || "");
      const generatedCount = Number(item && item.generated_count);
      const reusedCount = Number(item && item.reused_count);
      let title = robotId + " preload bijgewerkt";
      let tone = "ok";
      let message = "";
      if (status === "unsupported") {
        title = robotId + " preload niet beschikbaar";
        tone = "warn";
        message = String(item && item.message ? item.message : "Deze robot gebruikt live-only TTS.");
      } else {
        const parts = [];
        if (Number.isFinite(generatedCount) && generatedCount > 0) {
          parts.push(String(generatedCount) + " nieuw");
        }
        if (Number.isFinite(reusedCount) && reusedCount > 0) {
          parts.push(String(reusedCount) + " hergebruikt");
        }
        message = parts.length > 0 ? parts.join(", ") + "." : "Geen wijzigingen nodig.";
      }
      return {
        tone: tone,
        title: title,
        meta: describeProfile(robotStatus.current_profile),
        message: message,
      };
    });
    renderRuntimeActionCards(cards);
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
    const hasAutoOpenReason = status === "failed" || (status === "completed" && !!runError) || (isActiveRunStatus(status) && !!runError);
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

  function openSummaryUrl(url) {
    const targetUrl = String(url || "").trim();
    if (!targetUrl) {
      setStatus("Samenvat-URL ontbreekt.", "error");
      return false;
    }
    const win = window.open(targetUrl, "_blank", "noopener");
    if (!win) {
      setStatus("Kon summary niet in een nieuw tabblad openen. Gebruik eventueel popup-toestemming.", "warn");
      return false;
    }
    return true;
  }

  function buildSummaryBannerMessage(state) {
    const waiting = !!(state && state.summary_waiting);
    const connectionOk = !(state && state.summary_connection_ok === false);
    if (!connectionOk) {
      return "Verbinding met summary tijdelijk verloren. We proberen opnieuw.";
    }
    if (waiting) {
      return "Dit script wacht tot de samenvatting is afgerond.";
    }
    return "De samenvatting blijft actief in NAO Studio. Open de samenvatpagina om verder te gaan of annuleer de sessie.";
  }

  async function sendSummaryAbort() {
    ensureRunPolling();
    const choice = await openActionDialog({
      title: "Samenvatting annuleren",
      intro: ["Weet je zeker dat je de actieve samenvatting wilt annuleren?"],
      buttons: [
        { id: "cancel", label: "Terug" },
        { id: "confirm", label: "Samenvatting annuleren", tone: "primary" },
      ],
    });
    if (choice !== "confirm") {
      return;
    }
    try {
      const payload = await fetchJson("/api/run/summary_abort", { method: "POST" });
      renderRuntimeState(payload);
      setStatus("Samenvatting annuleren verstuurd.", "warn");
    } catch (err) {
      const message = err && err.message ? err.message : String(err);
      setStatus("Samenvatting annuleren mislukt: " + message, "error");
    }
    await refreshRunState({ silent: true });
  }

  function renderSummaryBanner(state) {
    if (!(summaryBanner instanceof HTMLElement)) {
      return;
    }
    summaryBanner.replaceChildren();
    const active = !!(state && state.summary_active);
    if (!active) {
      summaryBanner.classList.add("is-hidden");
      summaryBanner.classList.remove("is-async", "is-error");
      return;
    }

    const waiting = !!(state && state.summary_waiting);
    const connectionOk = !(state && state.summary_connection_ok === false);
    const status = state && state.summary_status ? String(state.summary_status) : "unknown";
    const lastError = state && state.summary_last_error ? String(state.summary_last_error).trim() : "";
    const url = state && state.summary_url ? String(state.summary_url) : "";

    summaryBanner.classList.remove("is-hidden", "is-async", "is-error");
    if (!waiting) {
      summaryBanner.classList.add("is-async");
    }
    if (!connectionOk) {
      summaryBanner.classList.add("is-error");
    }

    const head = document.createElement("div");
    head.className = "summary-banner-head";

    const title = document.createElement("div");
    title.className = "summary-banner-title";
    title.textContent = "Samenvatting actief.";
    head.appendChild(title);

    const statusEl = document.createElement("div");
    statusEl.className = "summary-banner-status";
    statusEl.textContent = "Status: " + status;
    head.appendChild(statusEl);
    summaryBanner.appendChild(head);

    const copy = document.createElement("div");
    copy.className = "summary-banner-copy";
    const main = document.createElement("p");
    main.textContent = buildSummaryBannerMessage(state);
    copy.appendChild(main);
    if (lastError) {
      const detail = document.createElement("p");
      detail.textContent = lastError;
      copy.appendChild(detail);
    }
    summaryBanner.appendChild(copy);

    const actions = document.createElement("div");
    actions.className = "row-actions summary-banner-actions";

    const btnOpen = document.createElement("button");
    btnOpen.type = "button";
    btnOpen.textContent = "Open samenvatting";
    btnOpen.disabled = !url;
    btnOpen.addEventListener("click", function () {
      openSummaryUrl(url);
    });
    actions.appendChild(btnOpen);

    const btnCancelSummary = document.createElement("button");
    btnCancelSummary.type = "button";
    btnCancelSummary.textContent = "Samenvatting annuleren";
    btnCancelSummary.addEventListener("click", async function () {
      await sendSummaryAbort();
    });
    actions.appendChild(btnCancelSummary);

    summaryBanner.appendChild(actions);
  }

  function maybeAutoOpenSummary(state) {
    const active = !!(state && state.summary_active);
    const url = state && state.summary_url ? String(state.summary_url) : "";
    const nonce = Number(state && state.summary_open_nonce);
    if (!active || !url || !Number.isFinite(nonce) || nonce <= 0) {
      if (!active) {
        lastSummaryAutoOpenSignal = "";
      }
      return;
    }
    const signal = [
      String((state && state.run_id) || ""),
      String((state && state.summary_session_id) || ""),
      String(nonce),
    ].join("|");
    if (!signal || signal === lastSummaryAutoOpenSignal) {
      return;
    }
    lastSummaryAutoOpenSignal = signal;
    openSummaryUrl(url);
  }

  function renderRuntimeState(state) {
    runtimeState = state || null;
    const status = state && state.status ? String(state.status) : "idle";
    if (status !== "preflight") {
      preflightConnectivityDialogShown = false;
    }
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
    renderSummaryBanner(state);
    maybeAutoOpenSummary(state);
    setRuntimeButtons(state);
    applyRunStepHighlights(runtimeState);
    syncAutoRestWatchPolling(runtimeState);
    if (!isActiveRunStatus(status)) {
      connectivityDialogIssueCounts.clear();
      if (currentConnectivityDialogSource === "poll") {
        closeConnectivityDialog();
      }
    }
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

  async function fetchTtsPreloadStatus(script) {
    return fetchJson("/api/tts_preload/status", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ script: script }),
    });
  }

  async function generateTtsPreload(script) {
    return fetchJson("/api/tts_preload/generate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ script: script }),
    });
  }

  async function pruneTtsPreload(policy) {
    return fetchJson("/api/tts_preload/prune", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ policy: policy }),
    });
  }

  function buildAutomaticCurrentPolicies(statusPayload) {
    const policies = {};
    const robots = statusPayload && Array.isArray(statusPayload.robots) ? statusPayload.robots : [];
    robots.forEach((robot) => {
      if (robot && robot.current_ready) {
        policies[String(robot.robot_id || "")] = { mode: "current" };
      }
    });
    return policies;
  }

  function summarizePreloadGeneration(result) {
    const generatedCount = result && typeof result.generated_count === "number" ? result.generated_count : 0;
    const reusedCount = result && typeof result.reused_count === "number" ? result.reused_count : 0;
    const robotsGeneration = result && Array.isArray(result.robots_generation) ? result.robots_generation : [];
    const unsupported = robotsGeneration.filter((item) => item && String(item.status || "") === "unsupported");
    let message =
      "Preload klaar: " + String(generatedCount) + " nieuw gemaakt en " + String(reusedCount) + " bestaand hergebruikt.";
    if (unsupported.length > 0) {
      const robots = unsupported.map((item) => String(item.robot_id || "?")).join(", ");
      message += " Live-only: " + robots + ".";
    }
    return {
      message: message,
      level: generatedCount > 0 || reusedCount > 0 ? "ok" : unsupported.length > 0 ? "warn" : "info",
    };
  }

  async function resolveStartPreloadPolicy(script, statusPayload) {
    let currentStatus = statusPayload;
    while (true) {
      const robots = currentStatus && Array.isArray(currentStatus.robots) ? currentStatus.robots : [];
      const problemRobots = robots.filter((robot) => {
        const status = String((robot && robot.status) || "");
        return status === "missing" || status === "mismatch_existing";
      });
      if (problemRobots.length === 0) {
        return buildAutomaticCurrentPolicies(currentStatus);
      }

      const canUseExisting = problemRobots.every(
        (robot) => String(robot.status || "") === "mismatch_existing" && !!robot.existing_ready
      );
      const buttons = [];
      if (canUseExisting) {
        buttons.push({ id: "use_existing", label: "Gebruik preload" });
      }
      buttons.push({ id: "preload_now", label: "Preload nu", tone: "primary" });
      buttons.push({ id: "live", label: "Live synthese" });
      buttons.push({ id: "cancel", label: "Annuleer" });

      const choice = await openActionDialog({
        title: "TTS preload controle",
        intro: [
          "De huidige DM TTS-profielen komen niet volledig overeen met wat al is voorgemaakt.",
          "Kies of je nu wilt preloaden, live wilt draaien of een bestaand preload-profiel wilt gebruiken.",
        ],
        robots: problemRobots.map((robot) => {
          const metaLines = ["Huidig: " + describeProfile(robot.current_profile)];
          if (robot.existing_profile) {
            metaLines.push("Bestaand preload: " + describeProfile(robot.existing_profile));
          } else if (typeof robot.current_missing_count === "number") {
            metaLines.push("Ontbrekende clips: " + String(robot.current_missing_count));
          }
          return {
            title: String(robot.robot_id || "?"),
            meta: metaLines.join(" | "),
            diff: diffProfileFields(robot.current_profile, robot.existing_profile),
          };
        }),
        buttons: buttons,
      });

      if (choice === "cancel") {
        return null;
      }
      if (choice === "live") {
        const policies = buildAutomaticCurrentPolicies(currentStatus);
        problemRobots.forEach((robot) => {
          policies[String(robot.robot_id || "")] = { mode: "live" };
        });
        return policies;
      }
      if (choice === "use_existing" && canUseExisting) {
        const policies = buildAutomaticCurrentPolicies(currentStatus);
        problemRobots.forEach((robot) => {
          const existingProfile = isObject(robot.existing_profile) ? robot.existing_profile : {};
          policies[String(robot.robot_id || "")] = {
            mode: "existing",
            profile_fingerprint: String(existingProfile.fingerprint || ""),
          };
        });
        return policies;
      }
      if (choice === "preload_now") {
        setPreloadRequestBusy(true);
        renderRuntimeActionCards([
          {
            tone: "info",
            title: "Preload bezig",
            meta: "Huidige DM TTS-profielen",
            message: "Ontbrekende audiofragmenten worden nu voorgemaakt voor de say-stappen in dit script.",
          },
        ]);
        setStatus("Run wacht op preload: audio wordt nu gerenderd voor de say-stappen.", "info");
        try {
          currentStatus = await generateTtsPreload(script);
        } finally {
          setPreloadRequestBusy(false);
        }
        renderPreloadResults(currentStatus);
        const summary = summarizePreloadGeneration(currentStatus);
        setStatus(summary.message, summary.level);
        continue;
      }
    }
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
      if (err && isConnectivityFailureMessage(err) && (status === "failed" || isActiveRunStatus(status))) {
        maybeShowDirectConnectivityDialog(err, currentScriptForAutoRestWatch(), [String(state.run_id || ""), status].join("|"));
      } else {
        maybeClearDirectConnectivityDialog(state);
      }
      if (err && err !== lastRuntimeError) {
        if (status === "failed") {
          setStatus("Run failed: " + err, "error");
        } else if (status === "completed") {
          setStatus("Run klaar met melding: " + err, "warn");
        } else {
          setStatus("Run melding: " + err, "warn");
        }
        lastRuntimeError = err;
      }
      if (!err) {
        lastRuntimeError = "";
        lastDirectConnectivitySignal = "";
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
    if (inputEl.type === "checkbox") {
      return { ok: true, value: !!raw, error: "" };
    }
    if (field === "start.delay_s" || field === "action.seconds" || field === "action.duration") {
      return coerceNumberInput(raw, false);
    }
    if (field === "action.slide") {
      return coerceNumberInput(raw, true);
    }
    const text = String(raw || "");
    if (field === "action.label") {
      return { ok: true, value: resolveCommandLabelValue(text), error: "" };
    }
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
    if (viewMode !== "blocks" && viewMode !== "config") {
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

  function applyJsonDraft() {
    const parsed = parseEditorToScriptState(editorJson.value);
    if (!parsed.ok) {
      const errorMessage = parsed.error || "JSON parsing failed";
      jsonError.textContent = errorMessage;
      jsonError.classList.remove("is-hidden");
      setStatus("JSON bevat fouten: " + errorMessage, "error");
      return false;
    }
    jsonError.classList.add("is-hidden");
    jsonError.textContent = "";
    scriptState = parsed.value;
    blocksSessionActive = true;
    selectedStepIndex =
      Array.isArray(scriptState.root.steps) && scriptState.root.steps.length > 0 ? 0 : null;
    blocksConfigDraft = formatJson(normalizeTopLevelConfigObject(buildConfigPaneObject(scriptState.root)));
    blocksConfigErrorMessage = "";
    blocksStepErrors = new Map();
    advancedDrafts = new Map();
    advancedOpen = new Set();
    setDirty(true);
    setStatus("JSON toegepast.", "ok");
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
      renderBlocks({ preserveInspectorScroll: true });
      if (switchToJsonOnError) {
        setViewMode("json");
      }
      return false;
    }
    scriptState.root.steps[index] = cloneJson(parsed.value);
    advancedDrafts.set(index, formatJson(parsed.value));
    blocksStepErrors.delete(index);
    renderBlocks({ preserveInspectorScroll: true });
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
    if (viewMode === "blocks" || viewMode === "config") {
      const ok = syncBlocksToEditor({ switchToJsonOnError: true, markDirty: true });
      if (!ok) {
        setStatus(actionLabel + " geblokkeerd: corrigeer eerst Config/Steps fouten.", "error");
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
    if (mode === "summary_start") {
      const wait = action.wait_for_complete !== false;
      const open = action.open_on_new_tab === true;
      let label = wait ? "samenvatten: start + wacht" : "samenvatten: start async";
      if (open) {
        label += " / open tab";
      }
      return label;
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

  function createCommandLabelInput(value, index, field) {
    const input = createInput("text", commandLabelDisplay(value), index, field);
    if (commandLabelSuggestions instanceof HTMLDataListElement) {
      ensureCommandLabelSuggestions();
      input.setAttribute("list", commandLabelSuggestions.id);
      input.setAttribute("autocomplete", "off");
      input.setAttribute("placeholder", "bijv. STAND UP");
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
        grid.appendChild(createField("action.label", createCommandLabelInput(step.action && step.action.label, index, "action.label"), false));
      } else if (actionMode === "behavior_start" || actionMode === "behavior_stop") {
        grid.appendChild(createField("action.behavior", createInput("text", step.action && step.action.behavior, index, "action.behavior"), false));
      } else if (actionMode === "dance") {
        grid.appendChild(createField("action.dance_key", createInput("text", step.action && step.action.dance_key, index, "action.dance_key"), false));
      } else if (actionMode === "nao_set_eye_color") {
        grid.appendChild(createField("action.color", createInput("text", step.action && step.action.color, index, "action.color"), false));
        const durationInput = createInput("number", step.action && step.action.duration, index, "action.duration");
        durationInput.step = "0.1";
        grid.appendChild(createField("action.duration", durationInput, false));
      } else if (actionMode === "summary_start") {
        grid.appendChild(
          createField(
            "action.wait_for_complete",
            createInput("checkbox", step.action && step.action.wait_for_complete !== false, index, "action.wait_for_complete"),
            false
          )
        );
        grid.appendChild(
          createField(
            "action.open_on_new_tab",
            createInput("checkbox", step.action && step.action.open_on_new_tab, index, "action.open_on_new_tab"),
            false
          )
        );
      }
    }

    return grid;
  }

  function createAdvancedView(options) {
    const advancedWrap = document.createElement("div");
    advancedWrap.className = "step-advanced-wrap";
    advancedWrap.setAttribute("data-index", String(options.index));

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
    
    const headerActions = document.createElement("div");
    headerActions.className = "step-header-controls";
    
    const isAdvancedView = advancedOpen.has(index);
    const toggleAdvancedBtn = document.createElement("button");
    toggleAdvancedBtn.type = "button";
    toggleAdvancedBtn.className = "btn-toggle-advanced";
    toggleAdvancedBtn.setAttribute("data-index", String(index));
    toggleAdvancedBtn.textContent = isAdvancedView ? "Reguliere weergave" : "Advanced JSON";
    headerActions.appendChild(toggleAdvancedBtn);
    
    const actions = createInspectorActions(index, totalSteps);
    headerActions.appendChild(actions);
    header.appendChild(headerActions);
    card.appendChild(header);

    const gridContainer = document.createElement("div");
    gridContainer.className = "step-view-container";
    gridContainer.setAttribute("data-index", String(index));
    gridContainer.classList.toggle("is-advanced-view", isAdvancedView);

    gridContainer.appendChild(createStepGrid(step, index));
    gridContainer.appendChild(
      createAdvancedView({
        index: index,
        step: step,
        drafts: advancedDrafts,
        errors: blocksStepErrors,
        textareaClass: "step-advanced",
        errorClass: "step-inline-error",
        actionAttr: "data-step-action",
      })
    );

    card.appendChild(gridContainer);
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

  function renderBlocks(options) {
    const preserveStepsRailScroll = !(options && options.preserveStepsRailScroll === false);
    const preserveInspectorScroll = !!(options && options.preserveInspectorScroll);
    const previousStepsRailScrollTop = preserveStepsRailScroll ? stepsCards.scrollTop : 0;
    const previousInspectorScrollTop = preserveInspectorScroll ? stepInspector.scrollTop : 0;
    const canRender = blocksSessionActive && scriptState && isObject(scriptState.root);
    stepsCards.replaceChildren();
    stepInspector.replaceChildren();
    if (!canRender) {
      blocksStepCount.textContent = "0 steps";
      blocksConfigSummary.textContent = "Geen configuratie geladen.";
      blocksEmpty.classList.remove("is-hidden");
      blocksConfigJson.value = "{}";
      resizeBlocksConfigEditor();
      const emptyInspector = document.createElement("div");
      emptyInspector.className = "step-inspector-empty";
      emptyInspector.textContent = "Kies een step om deze hier te bewerken.";
      stepInspector.appendChild(emptyInspector);
      renderBlocksConfigError();
      restoreScrollPosition(stepsCards, previousStepsRailScrollTop);
      restoreScrollPosition(stepInspector, previousInspectorScrollTop);
      return;
    }

    ensureSelectedStepIndex();
    blocksConfigJson.value = blocksConfigDraft;
    configHistoryStack = [blocksConfigDraft];
    configHistoryIndex = 0;
    btnUndoConfig.disabled = true;
    btnRedoConfig.disabled = true;
    resizeBlocksConfigEditor();
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
    restoreScrollPosition(stepsCards, previousStepsRailScrollTop);
    restoreScrollPosition(stepInspector, previousInspectorScrollTop);
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
    renderBlocks({ preserveInspectorScroll: true });
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
    updateJsonLineNumbers();
    updateJsonEditorPosition();
    jsonHistoryStack = [editorJson.value];
    jsonHistoryIndex = 0;
    btnUndoJson.disabled = true;
    btnRedoJson.disabled = true;
  }

  function activateConfigTab() {
    if (!ensureBlocksSessionFromEditor()) {
      return;
    }
    setViewMode("config");
    renderBlocks();
    resizeBlocksConfigEditor();
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
    if (preloadRequestInFlight) {
      setStatus("Preload audio is nog bezig. Wacht tot renderen klaar is.", "warn");
      return;
    }
    if (runStartRequestInFlight) {
      setStatus("Run wordt al voorbereid.", "warn");
      return;
    }
    if (!ensureViewSyncedForAction("Run starten")) {
      return;
    }
    setRunStartRequestBusy(true);
    let attemptedRunStart = false;
    try {
      renderDmStartResults([]);
      let script = parseEditorScriptForRun();
      if (!script) {
        return;
      }
      if (scriptHasSaySteps(script)) {
        script = ensureScriptIdForPreload(script);
      }
      let ttsPreload = undefined;
      if (scriptHasSaySteps(script)) {
        let preloadStatus;
        setStatus("Run voorbereiden: TTS preload wordt gecontroleerd.", "info");
        try {
          preloadStatus = await fetchTtsPreloadStatus(script);
        } catch (err) {
          const message = err && err.message ? err.message : String(err);
          setStatus("TTS preload status ophalen mislukt: " + message, "error");
          return;
        }
        try {
          const policyByRobot = await resolveStartPreloadPolicy(script, preloadStatus);
          if (policyByRobot === null) {
            setStatus("Run starten geannuleerd.", "warn");
            return;
          }
          if (Object.keys(policyByRobot).length > 0) {
            ttsPreload = { policy_by_robot: policyByRobot };
          }
        } catch (err) {
          const message = err && err.message ? err.message : String(err);
          renderRuntimeActionCards([
            {
              tone: "error",
              title: "TTS preloadcontrole mislukt",
              meta: "Run is niet gestart",
              message: message,
            },
          ]);
          setStatus("TTS preload verwerken mislukt: " + message, "error");
          return;
        }
      }
      setStatus("Run starten...", "info");
      attemptedRunStart = true;
      try {
        const payload = await fetchJson("/api/run/start", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ script: script, tts_preload: ttsPreload }),
        });
        renderRuntimeState(payload);
        setStatus("Run gestart.", "ok");
      } catch (err) {
        const message = err && err.message ? err.message : String(err);
        setStatus("Run starten mislukt: " + message, "error");
        maybeShowDirectConnectivityDialog(message, script, "start_run");
      }
    } finally {
      setRunStartRequestBusy(false);
      if (attemptedRunStart) {
        await refreshRunState({ silent: true });
      }
    }
  }

  async function preloadAudioFromEditor() {
    if (preloadRequestInFlight) {
      setStatus("Preload audio is al bezig.", "warn");
      return;
    }
    if (runStartRequestInFlight) {
      setStatus("Run wordt voorbereid. Wacht tot de preloadcontrole klaar is.", "warn");
      return;
    }
    if (!ensureViewSyncedForAction("Preload audio")) {
      return;
    }
    let script = parseEditorScriptForRun();
    if (!script) {
      return;
    }
    if (!scriptHasSaySteps(script)) {
      setStatus("Geen say-stappen gevonden om te preloaden.", "warn");
      return;
    }
    script = ensureScriptIdForPreload(script);
    setPreloadRequestBusy(true);
    renderRuntimeActionCards([
      {
        tone: "info",
        title: "Preload bezig",
        meta: "Huidige DM TTS-profielen",
        message: "Say-stappen worden gecontroleerd en ontbrekende audio wordt voorgemaakt op de server.",
      },
    ]);
    setStatus("Preload bezig: audio wordt nu gerenderd voor de say-stappen.", "info");
    try {
      const result = await generateTtsPreload(script);
      renderPreloadResults(result);
      const summary = summarizePreloadGeneration(result);
      setStatus(summary.message, summary.level);
    } catch (err) {
      const message = err && err.message ? err.message : String(err);
      renderRuntimeActionCards([
        {
          tone: "error",
          title: "Preload mislukt",
          meta: "Controleer TTS-instelling of modelconfiguratie",
          message: message,
        },
      ]);
      setStatus("Preload audio mislukt. Zie details in de runtimekaart.", "error");
    } finally {
      setPreloadRequestBusy(false);
    }
  }

  async function prunePreloadCacheFromUi() {
    const choice = await openActionDialog({
      title: "Opschonen preload-cache",
      intro: ["Verwijder ongebruikte clips uit de gedeelde TTS preload-cache."],
      buttons: [
        { id: "all", label: "Alles verwijderen" },
        { id: "14d", label: "Ongebruikt 14 dagen" },
        { id: "30d", label: "Ongebruikt 30 dagen" },
        { id: "cancel", label: "Annuleer" },
      ],
    });
    if (!choice || choice === "cancel") {
      return;
    }
    try {
      const result = await pruneTtsPreload(choice);
      setStatus(
        "Preload-cache opgeschoond: " + String(result.deleted_count || 0) + " clip(s) verwijderd.",
        result.deleted_count > 0 ? "ok" : "info"
      );
    } catch (err) {
      const message = err && err.message ? err.message : String(err);
      setStatus("Opschonen preload-cache mislukt: " + message, "error");
    }
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
    const hadSummaryActive = !!(runtimeState && runtimeState.summary_active);
    let summaryAction = "leave";
    if (hadSummaryActive) {
      const choice = await openActionDialog({
        title: "Run afbreken",
        intro: ["Er is nog een actieve samenvatting gekoppeld aan deze run. Kies wat je wilt afbreken."],
        buttons: [
          { id: "leave", label: "Alleen script afbreken" },
          { id: "abort", label: "Script en samenvatting afbreken", tone: "primary" },
          { id: "cancel", label: "Terug" },
        ],
      });
      if (choice === "cancel") {
        return;
      }
      summaryAction = choice === "abort" ? "abort" : "leave";
    }
    try {
      const payload = await fetchJson("/api/run/abort", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ summary_action: summaryAction }),
      });
      renderRuntimeState(payload);
      if (payload && payload.summary_abort_warning) {
        setStatus("Script afgebroken. " + String(payload.summary_abort_warning), "warn");
      } else if (hadSummaryActive && summaryAction === "abort") {
        setStatus("Abort verstuurd. Script en samenvatting worden afgebroken.", "warn");
      } else if (hadSummaryActive) {
        setStatus("Abort verstuurd. Samenvatting blijft actief.", "warn");
      } else {
        setStatus("Abort verstuurd.", "warn");
      }
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
      renderRobotStatusSection([]);
      ensureRunPolling();
      await refreshRunState({ silent: true });
      const currentStatus = runtimeState && typeof runtimeState.status === "string" ? runtimeState.status : "idle";
      const currentError = runtimeState && typeof runtimeState.last_error === "string" ? runtimeState.last_error.trim() : "";
      const keepRuntimeMessage =
        !!currentError && (isActiveRunStatus(currentStatus) || currentStatus === "completed" || currentStatus === "failed");
      if (!keepRuntimeMessage) {
        setStatus("Script Builder klaar.", "ok");
      }
    } catch (err) {
      const message = err && err.message ? err.message : String(err);
      setStatus("Initialisatie mislukt: " + message, "error");
    }
  }
  editorJson.addEventListener("input", function () {
    setDirty(true);
    updateJsonLineNumbers();
    updateJsonEditorPosition();
    pushJsonHistory();
    if (viewMode === "json") {
      resetBlocksSession();
      clearBlockValidationErrors();
    }
  });

  editorJson.addEventListener("click", updateJsonEditorPosition);
  editorJson.addEventListener("keyup", updateJsonEditorPosition);
  editorJson.addEventListener("selectionchange", updateJsonEditorPosition);

  btnUndoJson.addEventListener("click", jsonUndo);
  btnRedoJson.addEventListener("click", jsonRedo);

  blocksConfigJson.addEventListener("input", function () {
    blocksConfigDraft = blocksConfigJson.value;
    resizeBlocksConfigEditor();
    validateConfigDraft();
    pushConfigHistory();
  });

  btnUndoConfig.addEventListener("click", configUndo);
  btnRedoConfig.addEventListener("click", configRedo);

  btnApplyConfig.addEventListener("click", function () {
    if (!blocksSessionActive || !scriptState) {
      return;
    }
    snapshotBlocksDraftBuffers();
    const ok = applyConfigDraft({ switchToJsonOnError: true, markDirty: true });
    setStatus(ok ? "Config toegepast." : "Config JSON bevat fouten.", ok ? "ok" : "error");
    renderBlocks();
  });

  btnApplyJson.addEventListener("click", function () {
    applyJsonDraft();
  });

  selCategory.addEventListener("change", handleCategoryChange);
  selTemplate.addEventListener("change", handleTemplateChange);
  btnTabJson.addEventListener("click", activateJsonTab);
  btnTabConfig.addEventListener("click", activateConfigTab);
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
  if (btnPreloadAudio) {
    btnPreloadAudio.addEventListener("click", async function () {
      await preloadAudioFromEditor();
    });
  }
  if (btnPreloadPrune) {
    btnPreloadPrune.addEventListener("click", async function () {
      await prunePreloadCacheFromUi();
    });
  }
  if (actionDialogClose) {
    actionDialogClose.addEventListener("click", function () {
      if (currentActionDialogKind === "connectivity") {
        dismissConnectivityDialog();
        return;
      }
      closeActionDialog("cancel");
    });
  }
  if (typeof HTMLDialogElement !== "undefined" && actionDialog instanceof HTMLDialogElement) {
    actionDialog.addEventListener("cancel", function (event) {
      event.preventDefault();
      if (currentActionDialogKind === "connectivity") {
        dismissConnectivityDialog();
        return;
      }
      closeActionDialog("cancel");
    });
  }
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
    
    // Handle toggle advanced view button
    if (target.classList.contains("btn-toggle-advanced")) {
      const card = target.closest(".step-inspector-card");
      if (!(card instanceof HTMLElement)) {
        return;
      }
      const viewContainer = card.querySelector(".step-view-container");
      if (!(viewContainer instanceof HTMLElement)) {
        return;
      }
      const index = Number(target.getAttribute("data-index") || viewContainer.getAttribute("data-index"));
      if (!Number.isInteger(index) || index < 0) {
        return;
      }
      const isAdvanced = viewContainer.classList.toggle("is-advanced-view");
      target.textContent = isAdvanced ? "Reguliere weergave" : "Advanced JSON";
      
      if (isAdvanced) {
        advancedOpen.add(index);
      } else {
        advancedOpen.delete(index);
      }
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

  if (quickOpenSelect instanceof HTMLSelectElement) {
    quickOpenSelect.addEventListener("change", async function () {
      const exampleName = quickOpenSelect.value;
      if (!exampleName) {
        return;
      }
      if (!confirmDiscardEditorChanges()) {
        quickOpenSelect.value = "";
        return;
      }
      await loadExample(exampleName);
      quickOpenSelect.value = "";
    });
  }

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
