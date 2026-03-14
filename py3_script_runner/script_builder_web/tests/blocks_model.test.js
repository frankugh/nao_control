import { describe, expect, test } from "vitest";

import {
  applyConfigPaneObject,
  buildConfigPaneObject,
  formatJson,
  insertSnippetAfterSelection,
  moveStepInState,
  parseAdvancedStepJson,
  parseConfigPaneJson,
  parseEditorToScriptState,
  shouldWarnWithPrevReorder,
  updateObjectPath,
} from "../blocks_model.js";

function sampleScript() {
  return {
    version: 1,
    robots: { nao1: { dm_url: "http://127.0.0.1:5301" } },
    defaults: { on_error: "prompt", request_timeout_s: 12 },
    ppt: { enabled: false },
    custom_meta: { keep_me: true },
    steps: [
      {
        id: "s1",
        robot_id: "nao1",
        start: { mode: "manual" },
        action: { type: "say", text: "hallo" },
        custom_step: { keep: 1 },
      },
      {
        id: "s2",
        robot_id: "nao1",
        start: { mode: "with_prev" },
        action: { type: "do", mode: "command", label: "WAVE" },
      },
    ],
  };
}

describe("parse + roundtrip", () => {
  test("parseEditorToScriptState rejects invalid root", () => {
    const parsed = parseEditorToScriptState("[]");
    expect(parsed.ok).toBe(false);
    expect(parsed.error).toContain("Editor root");
  });

  test("roundtrip keeps unknown fields", () => {
    const script = sampleScript();
    const parsed = parseEditorToScriptState(formatJson(script));
    expect(parsed.ok).toBe(true);
    expect(parsed.value.root.custom_meta.keep_me).toBe(true);
    expect(parsed.value.root.steps[0].custom_step.keep).toBe(1);
  });
});

describe("config + advanced parsing", () => {
  test("config apply updates known keys but keeps other top-level data", () => {
    const root = sampleScript();
    const cfg = buildConfigPaneObject(root);
    cfg.defaults.on_error = "continue";
    const next = applyConfigPaneObject(root, cfg);
    expect(next.defaults.on_error).toBe("continue");
    expect(next.custom_meta.keep_me).toBe(true);
  });

  test("parseConfigPaneJson and parseAdvancedStepJson surface errors", () => {
    expect(parseConfigPaneJson("{").ok).toBe(false);
    expect(parseAdvancedStepJson("[]").ok).toBe(false);
  });
});

describe("insert + reorder", () => {
  test("insertSnippetAfterSelection inserts after selected index", () => {
    const state = { root: sampleScript() };
    const snippet = {
      id: "new_step",
      robot_id: "nao1",
      start: { mode: "manual" },
      action: { type: "pause", seconds: 1 },
    };
    const result = insertSnippetAfterSelection(state, snippet, 0);
    expect(result.ok).toBe(true);
    expect(result.state.root.steps[1].id).toBe("new_step");
    expect(result.selectedIndex).toBe(1);
  });

  test("moveStepInState reorders and with_prev warning works", () => {
    const state = { root: sampleScript() };
    const moved = moveStepInState(state, 1, 0);
    expect(moved.moved).toBe(true);
    expect(moved.state.root.steps[0].id).toBe("s2");
    expect(shouldWarnWithPrevReorder(state.root.steps, 1, 0)).toBe(true);
  });
});

describe("basic field update keeps unknown fields", () => {
  test("updateObjectPath mutates only requested path", () => {
    const step = sampleScript().steps[0];
    const next = updateObjectPath(step, "action.text", "nieuw");
    expect(next.action.text).toBe("nieuw");
    expect(next.custom_step.keep).toBe(1);
    expect(next.id).toBe("s1");
  });
});
