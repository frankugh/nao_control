/** @vitest-environment jsdom */
import { describe, expect, test } from "vitest";

import {
  hasBlockingBlockErrors,
  insertSnippetAfterSelection,
  parseEditorToScriptState,
  resolveInsertIndex,
} from "../blocks_model.js";

function stateWithSteps(count) {
  const steps = [];
  for (let i = 0; i < count; i += 1) {
    steps.push({
      id: "s" + String(i + 1),
      start: { mode: "manual" },
      action: { type: "pause", seconds: 1 },
    });
  }
  return { root: { version: 1, robots: { nao1: { dm_url: "http://127.0.0.1:5301" } }, steps } };
}

describe("UI behavior guards", () => {
  test("tab switch to Blocks should block on invalid JSON", () => {
    const parsed = parseEditorToScriptState("{ invalid");
    expect(parsed.ok).toBe(false);
  });

  test("run/save guard blocks when config or step errors exist", () => {
    expect(hasBlockingBlockErrors("fout", new Map())).toBe(true);
    expect(hasBlockingBlockErrors("", new Map([[0, "fout"]]))).toBe(true);
    expect(hasBlockingBlockErrors("", new Map())).toBe(false);
  });

  test("insert resolves index based on current selection", () => {
    expect(resolveInsertIndex(null, 3)).toBe(3);
    expect(resolveInsertIndex(1, 3)).toBe(2);
  });
});

describe("jsdom sanity", () => {
  test("insert behavior follows selected card position", () => {
    document.body.innerHTML = "<div id='root'></div>";
    const state = stateWithSteps(2);
    const snippet = {
      id: "inserted",
      start: { mode: "manual" },
      action: { type: "pause", seconds: 1 },
    };
    const result = insertSnippetAfterSelection(state, snippet, 0);
    expect(result.ok).toBe(true);
    expect(result.state.root.steps[1].id).toBe("inserted");
  });
});
