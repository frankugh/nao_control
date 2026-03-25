export const START_MODES = ["manual", "after_prev", "with_prev"];
export const ACTION_TYPES = ["say", "do", "pause", "ppt"];
export const DO_MODES = [
  "command",
  "behavior_start",
  "behavior_stop",
  "dance",
  "nao_set_eye_color",
  "summary_start",
];
export const PPT_MODES = ["next_slide", "previous_slide", "goto"];
export const ON_ERROR_MODES = ["prompt", "abort", "continue"];

export function isObject(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

export function cloneJson(value) {
  return JSON.parse(JSON.stringify(value));
}

export function formatJson(value) {
  return JSON.stringify(value, null, 2);
}

export function parseJsonText(text, label) {
  try {
    return { ok: true, value: JSON.parse(text), error: "" };
  } catch (err) {
    const message = err && err.message ? err.message : "Onbekende parsefout";
    return { ok: false, value: null, error: `${label} JSON parsefout: ${message}` };
  }
}

export function parseEditorToScriptState(text) {
  const parsed = parseJsonText(text, "Editor");
  if (!parsed.ok) {
    return parsed;
  }
  if (!isObject(parsed.value)) {
    return { ok: false, value: null, error: "Editor root moet een JSON object zijn." };
  }
  const root = cloneJson(parsed.value);
  if (typeof root.steps === "undefined") {
    root.steps = [];
  }
  if (!Array.isArray(root.steps)) {
    return { ok: false, value: null, error: "Editor field 'steps' moet een array zijn." };
  }
  return { ok: true, value: { root: root }, error: "" };
}

export function scriptStateToEditorText(scriptState) {
  if (!scriptState || !isObject(scriptState.root)) {
    throw new Error("scriptState.root must be an object");
  }
  return formatJson(scriptState.root);
}

export function buildConfigPaneObject(root) {
  const cfg = {};
  cfg.robots = isObject(root.robots) ? cloneJson(root.robots) : {};
  cfg.ppt = isObject(root.ppt) ? cloneJson(root.ppt) : {};
  cfg.defaults = isObject(root.defaults) ? cloneJson(root.defaults) : {};
  return cfg;
}

export function applyConfigPaneObject(root, configObj) {
  if (!isObject(root)) {
    throw new Error("root must be an object");
  }
  if (!isObject(configObj)) {
    throw new Error("configObj must be an object");
  }
  const next = cloneJson(root);
  if ("robots" in configObj) {
    next.robots = cloneJson(configObj.robots);
  } else {
    delete next.robots;
  }
  if ("ppt" in configObj) {
    next.ppt = cloneJson(configObj.ppt);
  } else {
    delete next.ppt;
  }
  if ("defaults" in configObj) {
    next.defaults = cloneJson(configObj.defaults);
  } else {
    delete next.defaults;
  }
  if (!Array.isArray(next.steps)) {
    next.steps = [];
  }
  return next;
}

function _pathParts(path) {
  if (Array.isArray(path)) {
    return path.slice();
  }
  return String(path || "")
    .split(".")
    .map((part) => String(part || "").trim())
    .filter((part) => part.length > 0);
}

export function updateObjectPath(source, path, value) {
  if (!isObject(source)) {
    throw new Error("source must be an object");
  }
  const parts = _pathParts(path);
  if (parts.length === 0) {
    return cloneJson(source);
  }
  const next = cloneJson(source);
  let cursor = next;
  for (let i = 0; i < parts.length - 1; i += 1) {
    const key = parts[i];
    if (!isObject(cursor[key])) {
      cursor[key] = {};
    }
    cursor = cursor[key];
  }
  const lastKey = parts[parts.length - 1];
  if (typeof value === "undefined") {
    delete cursor[lastKey];
  } else {
    cursor[lastKey] = value;
  }
  return next;
}

export function parseSnippetForInsert(snippetValue) {
  if (!isObject(snippetValue) && !Array.isArray(snippetValue)) {
    return { ok: false, steps: [], error: "Preview moet een JSON object of array zijn." };
  }
  if (Array.isArray(snippetValue)) {
    const invalid = snippetValue.some((item) => !isObject(item));
    if (invalid) {
      return { ok: false, steps: [], error: "Preview array moet alleen step objecten bevatten." };
    }
    return { ok: true, steps: cloneJson(snippetValue), error: "" };
  }
  return { ok: true, steps: [cloneJson(snippetValue)], error: "" };
}

export function resolveInsertIndex(selectedIndex, stepCount) {
  const count = Math.max(0, Number(stepCount) || 0);
  if (selectedIndex === null || typeof selectedIndex === "undefined") {
    return count;
  }
  const idx = Number(selectedIndex);
  if (!Number.isInteger(idx) || idx < 0 || idx >= count) {
    return count;
  }
  return idx + 1;
}

export function insertSnippetAfterSelection(scriptState, snippetValue, selectedIndex) {
  if (!scriptState || !isObject(scriptState.root) || !Array.isArray(scriptState.root.steps)) {
    throw new Error("scriptState.root.steps must be an array");
  }
  const parsed = parseSnippetForInsert(snippetValue);
  if (!parsed.ok) {
    return { ok: false, state: scriptState, selectedIndex: selectedIndex, added: 0, error: parsed.error };
  }
  const next = { root: cloneJson(scriptState.root) };
  const insertAt = resolveInsertIndex(selectedIndex, next.root.steps.length);
  next.root.steps.splice(insertAt, 0, ...parsed.steps);
  return {
    ok: true,
    state: next,
    selectedIndex: insertAt + parsed.steps.length - 1,
    added: parsed.steps.length,
    error: "",
  };
}

export function moveStepInState(scriptState, fromIndex, toIndex) {
  if (!scriptState || !isObject(scriptState.root) || !Array.isArray(scriptState.root.steps)) {
    throw new Error("scriptState.root.steps must be an array");
  }
  const steps = cloneJson(scriptState.root.steps);
  const from = Number(fromIndex);
  const to = Number(toIndex);
  if (!Number.isInteger(from) || !Number.isInteger(to)) {
    return { state: scriptState, moved: false };
  }
  if (from < 0 || from >= steps.length || to < 0 || to >= steps.length || from === to) {
    return { state: scriptState, moved: false };
  }
  const [moved] = steps.splice(from, 1);
  steps.splice(to, 0, moved);
  const next = { root: cloneJson(scriptState.root) };
  next.root.steps = steps;
  return { state: next, moved: true };
}

export function shouldWarnWithPrevReorder(steps, fromIndex, toIndex) {
  if (!Array.isArray(steps)) {
    return false;
  }
  const from = Number(fromIndex);
  const to = Number(toIndex);
  if (!Number.isInteger(from) || !Number.isInteger(to) || from === to) {
    return false;
  }
  const inRange =
    from >= 0 && from < steps.length && to >= 0 && to < steps.length;
  if (!inRange) {
    return false;
  }
  const moved = steps[from];
  const movedMode = moved && moved.start ? String(moved.start.mode || "") : "";
  if (movedMode === "with_prev") {
    return true;
  }
  const low = Math.min(from, to);
  const high = Math.max(from, to);
  for (let i = low; i <= high; i += 1) {
    const step = steps[i];
    const mode = step && step.start ? String(step.start.mode || "") : "";
    if (mode === "with_prev") {
      return true;
    }
  }
  return false;
}

export function parseAdvancedStepJson(text) {
  const parsed = parseJsonText(text, "Advanced step");
  if (!parsed.ok) {
    return parsed;
  }
  if (!isObject(parsed.value)) {
    return { ok: false, value: null, error: "Advanced step moet een JSON object zijn." };
  }
  return parsed;
}

export function parseConfigPaneJson(text) {
  const parsed = parseJsonText(text, "Config");
  if (!parsed.ok) {
    return parsed;
  }
  if (!isObject(parsed.value)) {
    return { ok: false, value: null, error: "Config pane moet een JSON object zijn." };
  }
  return parsed;
}

export function hasBlockingBlockErrors(configError, stepErrorMap) {
  const hasConfigError = String(configError || "").trim().length > 0;
  if (hasConfigError) {
    return true;
  }
  if (!stepErrorMap) {
    return false;
  }
  if (stepErrorMap instanceof Map) {
    return stepErrorMap.size > 0;
  }
  if (Array.isArray(stepErrorMap)) {
    return stepErrorMap.length > 0;
  }
  if (isObject(stepErrorMap)) {
    return Object.keys(stepErrorMap).length > 0;
  }
  return false;
}
