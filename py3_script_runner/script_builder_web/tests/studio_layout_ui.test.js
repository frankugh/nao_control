/** @vitest-environment jsdom */
import fs from "node:fs";
import path from "node:path";

import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

const INDEX_HTML = fs.readFileSync(path.join(process.cwd(), "index.html"), "utf-8");

const TEMPLATE_FIXTURE = {
  default_script: {
    version: 1,
    robots: {
      nao1: {
        dm_url: "http://127.0.0.1:5301",
      },
    },
    defaults: { request_timeout_s: 12, on_error: "prompt" },
    ppt: { enabled: true, file: "C:/slides/demo.pptx" },
    steps: [
      {
        id: "say_intro",
        robot_id: "nao1",
        start: { mode: "manual" },
        action: { type: "say", text: "Welkom bij de demo." },
      },
      {
        id: "ppt_intro",
        start: { mode: "with_prev" },
        action: { type: "ppt", mode: "next_slide" },
      },
      {
        id: "pause_short",
        start: { mode: "after_prev", delay_s: 2 },
        action: { type: "pause", seconds: 1.5 },
      },
    ],
  },
  catalog: [
    {
      category_key: "basic",
      category_label: "Basic",
      templates: [
        {
          template_key: "demo",
          template_label: "demo",
          snippet: [
            {
              id: "tmpl_say",
              robot_id: "nao1",
              start: { mode: "manual" },
              action: { type: "say", text: "Hallo allemaal." },
            },
            {
              id: "tmpl_ppt",
              start: { mode: "with_prev" },
              action: { type: "ppt", mode: "goto", slide: 3 },
            },
          ],
        },
        {
          template_key: "command_demo",
          template_label: "command",
          snippet: {
            id: "tmpl_command",
            robot_id: "nao1",
            start: { mode: "manual" },
            action: { type: "do", mode: "command", label: "STAND_UP" },
          },
        },
      ],
    },
  ],
};

function makeJsonResponse(payload, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status: status,
    async json() {
      return payload;
    },
    async text() {
      return JSON.stringify(payload);
    },
  };
}

async function flushUi() {
  await Promise.resolve();
  await Promise.resolve();
  await Promise.resolve();
}

async function waitFor(check, attempts = 20) {
  for (let index = 0; index < attempts; index += 1) {
    await flushUi();
    if (check()) {
      return;
    }
    await new Promise((resolve) => setTimeout(resolve, 0));
  }
  throw new Error("waitFor timed out");
}

function actionDialogIsOpen() {
  const dialog = document.getElementById("actionDialog");
  return !!(dialog && (dialog.open || dialog.hasAttribute("open")));
}

function dispatchDragEvent(node, type) {
  const event = new Event(type, { bubbles: true, cancelable: true });
  Object.defineProperty(event, "dataTransfer", {
    value: {
      effectAllowed: "move",
      setData() {},
    },
  });
  node.dispatchEvent(event);
}

async function loadApp(runState, options = {}) {
  document.open();
  document.write(INDEX_HTML);
  document.close();

  const fetchMock = vi.fn(async (url, fetchOptions = {}) => {
    if (typeof options.fetchImpl === "function") {
      const handled = await options.fetchImpl(url, fetchOptions);
      if (handled) {
        return handled;
      }
    }
    if (url === "./templates.json") {
      return makeJsonResponse(TEMPLATE_FIXTURE);
    }
    if (url === "/api/run/state") {
      return makeJsonResponse(
        runState || {
          ok: true,
          status: "idle",
          waiting_for_next: false,
          waiting_reason: "none",
          current_step_id: "",
          completed_steps: 0,
          total_steps: 0,
          log_tail: [],
          last_error: null,
        }
      );
    }
    if (url === "/api/cmdrec/labels") {
      return makeJsonResponse({ ok: true, labels: ["WAVE", "SIT_DOWN", "STAND\\_UP"] });
    }
    if (url === "/api/run/start") {
      return makeJsonResponse({
        ok: true,
        status: "preflight",
        waiting_for_next: false,
        waiting_reason: "none",
        current_step_id: "",
        completed_steps: 0,
        total_steps: 3,
        log_tail: [],
        last_error: null,
      });
    }
    if (url === "/api/auto_rest_watch/status") {
      return makeJsonResponse({ ok: true, robots: [] });
    }
    throw new Error("Unexpected fetch: " + String(url));
  });

  globalThis.fetch = fetchMock;
  window.fetch = fetchMock;
  vi.resetModules();
  await import("../app.js");
  await flushUi();
}

describe("studio layout ui", () => {
  beforeEach(() => {
    vi.spyOn(window, "setInterval").mockReturnValue(1);
    vi.spyOn(window, "clearInterval").mockImplementation(() => {});
  });

  afterEach(() => {
    vi.restoreAllMocks();
    document.body.innerHTML = "";
  });

  test("renders steps as a compact rail and updates inspector on selection", async () => {
    await loadApp();

    const rows = document.querySelectorAll("#stepsCards .step-row");
    expect(rows).toHaveLength(3);
    expect(document.querySelector("#stepInspector .inspector-title").textContent).toContain("say_intro");

    document.querySelector('#stepsCards .step-row[data-index="1"]').click();
    await flushUi();

    expect(document.querySelector("#stepInspector .inspector-title").textContent).toContain("ppt_intro");
    expect(document.querySelectorAll('#stepInspector select[data-field="action.mode"]')).toHaveLength(1);
  });

  test("selecting a different step keeps the steps rail scroll position", async () => {
    const originalReplaceChildren = Element.prototype.replaceChildren;
    vi.spyOn(Element.prototype, "replaceChildren").mockImplementation(function (...nodes) {
      const result = originalReplaceChildren.apply(this, nodes);
      if (this instanceof HTMLElement && this.id === "stepsCards") {
        this.scrollTop = 0;
      }
      return result;
    });

    await loadApp();

    const stepsCards = document.getElementById("stepsCards");
    stepsCards.scrollTop = 180;

    document.querySelector('#stepsCards .step-row[data-index="1"]').click();
    await flushUi();

    expect(stepsCards.scrollTop).toBe(180);
  });

  test("editing in the step inspector updates the editor json and row summary", async () => {
    await loadApp();

    const textArea = document.querySelector('#stepInspector textarea[data-field="action.text"]');
    textArea.value = "Nieuwe welkomstzin voor de workshop.";
    textArea.dispatchEvent(new Event("change", { bubbles: true }));
    await flushUi();

    expect(document.getElementById("editorJson").value).toContain("Nieuwe welkomstzin voor de workshop.");
    expect(document.querySelector('#stepsCards .step-row[data-index="0"] .step-row-summary').textContent).toContain(
      "Nieuwe welkomstzin voor de workshop."
    );
  });

  test("command label uses a typeable suggestion list in template and step inspectors", async () => {
    await loadApp();

    const templateSelect = document.getElementById("selTemplate");
    templateSelect.value = "command_demo";
    templateSelect.dispatchEvent(new Event("change", { bubbles: true }));
    await flushUi();

    const templateInput = document.querySelector('#templateInspector input[data-field="action.label"]');
    expect(templateInput).not.toBeNull();
    expect(templateInput.getAttribute("list")).toBe("commandLabelSuggestions");
    expect(templateInput.value).toBe("STAND UP");

    await flushUi();

    const datalistValues = Array.from(document.querySelectorAll("#commandLabelSuggestions option")).map((option) => option.value);
    expect(datalistValues).toContain("STAND UP");
    expect(datalistValues).toContain("WAVE");
    expect(datalistValues).toContain("SIT DOWN");
    expect(datalistValues.filter((value) => value === "STAND UP")).toHaveLength(1);

    document.getElementById("btnInsertTemplate").click();
    await flushUi();

    const stepInput = document.querySelector('#stepInspector input[data-field="action.label"]');
    expect(stepInput).not.toBeNull();
    expect(stepInput.getAttribute("list")).toBe("commandLabelSuggestions");
    expect(stepInput.value).toBe("STAND UP");

    stepInput.value = "SIT DOWN";
    stepInput.dispatchEvent(new Event("change", { bubbles: true }));
    await flushUi();

    expect(document.getElementById("editorJson").value).toContain('"label": "SIT_DOWN"');
  });

  test("editing the selected step keeps the inspector scroll position", async () => {
    const originalReplaceChildren = Element.prototype.replaceChildren;
    vi.spyOn(Element.prototype, "replaceChildren").mockImplementation(function (...nodes) {
      const result = originalReplaceChildren.apply(this, nodes);
      if (this instanceof HTMLElement && this.id === "stepInspector") {
        this.scrollTop = 0;
      }
      return result;
    });

    await loadApp();

    const stepInspector = document.getElementById("stepInspector");
    stepInspector.scrollTop = 140;

    const textArea = document.querySelector('#stepInspector textarea[data-field="action.text"]');
    textArea.value = "Nog een langere gewijzigde tekst.";
    textArea.dispatchEvent(new Event("change", { bubbles: true }));
    await flushUi();

    expect(stepInspector.scrollTop).toBe(140);
  });

  test("config editor grows to fit its content in blocks mode", async () => {
    const originalScrollHeight = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "scrollHeight");
    Object.defineProperty(HTMLTextAreaElement.prototype, "scrollHeight", {
      configurable: true,
      get() {
        if (this.id === "blocksConfigJson") {
          return 420;
        }
        return 0;
      },
    });

    try {
      await loadApp();

      document.getElementById("btnTabBlocks").click();
      await flushUi();

      expect(document.getElementById("blocksConfigJson").style.height).toBe("420px");
    } finally {
      if (originalScrollHeight) {
        Object.defineProperty(HTMLTextAreaElement.prototype, "scrollHeight", originalScrollHeight);
      } else {
        delete HTMLTextAreaElement.prototype.scrollHeight;
      }
    }
  });

  test("opening config switches blocks mode into config focus layout", async () => {
    await loadApp();

    document.getElementById("btnTabBlocks").click();
    await flushUi();

    const blocksView = document.getElementById("blocksView");
    const blocksConfigSection = document.getElementById("blocksConfigSection");

    expect(blocksView.classList.contains("is-config-open")).toBe(false);

    blocksConfigSection.open = true;
    blocksConfigSection.dispatchEvent(new Event("toggle"));
    await flushUi();

    expect(blocksView.classList.contains("is-config-open")).toBe(true);

    blocksConfigSection.open = false;
    blocksConfigSection.dispatchEvent(new Event("toggle"));
    await flushUi();

    expect(blocksView.classList.contains("is-config-open")).toBe(false);
  });

  test("config summary stays populated when the section is toggled", async () => {
    await loadApp();

    document.getElementById("btnTabBlocks").click();
    await flushUi();

    const blocksConfigSection = document.getElementById("blocksConfigSection");
    const blocksConfigSummary = document.getElementById("blocksConfigSummary");
    const expectedSummary = blocksConfigSummary.textContent;

    expect(expectedSummary).toContain("1 robot");
    expect(expectedSummary).toMatch(/PPT (aan|uit)/);

    blocksConfigSection.open = true;
    blocksConfigSection.dispatchEvent(new Event("toggle"));
    await flushUi();
    expect(blocksConfigSummary.textContent).toBe(expectedSummary);

    blocksConfigSection.open = false;
    blocksConfigSection.dispatchEvent(new Event("toggle"));
    await flushUi();
    expect(blocksConfigSummary.textContent).toBe(expectedSummary);
  });

  test("step rows expose move controls directly in the rail", async () => {
    await loadApp();
    vi.spyOn(window, "confirm").mockReturnValue(true);

    const moveDown = document.querySelector('#stepsCards .step-row[data-index="0"] [data-step-action="move-down"]');
    expect(moveDown).not.toBeNull();

    moveDown.click();
    await flushUi();

    const titles = Array.from(document.querySelectorAll("#stepsCards .step-row .step-row-title")).map((el) => el.textContent);
    expect(titles).toEqual(["ppt_intro", "say_intro", "pause_short"]);
    expect(document.querySelector("#stepInspector .inspector-title").textContent).toContain("say_intro");
  });

  test("dragging a step row reorders without rerendering away the drag state", async () => {
    await loadApp();
    vi.spyOn(window, "confirm").mockReturnValue(true);

    const sourceRow = document.querySelector('#stepsCards .step-row[data-index="0"]');
    const targetRow = document.querySelector('#stepsCards .step-row[data-index="2"]');

    dispatchDragEvent(sourceRow, "dragstart");
    dispatchDragEvent(targetRow, "dragover");
    expect(targetRow.classList.contains("is-drag-over")).toBe(true);

    dispatchDragEvent(targetRow, "drop");
    await flushUi();

    const titles = Array.from(document.querySelectorAll("#stepsCards .step-row .step-row-title")).map((el) => el.textContent);
    expect(titles).toEqual(["ppt_intro", "pause_short", "say_intro"]);
    expect(document.querySelector("#stepInspector .inspector-title").textContent).toContain("say_intro");
  });

  test("template editor renders all template blocks directly in one inspector zone", async () => {
    await loadApp();

    expect(document.getElementById("templateCards")).toBeNull();
    expect(document.querySelectorAll("#templateInspector .template-inspector-card")).toHaveLength(2);
    expect(document.querySelector('#templateInspector .template-inspector-card[data-index="0"] .inspector-title').textContent).toContain(
      "tmpl_say"
    );
    expect(document.querySelector('#templateInspector .template-inspector-card[data-index="1"] .inspector-title').textContent).toContain(
      "tmpl_ppt"
    );
    expect(
      document.querySelector('#templateInspector .template-inspector-card[data-index="1"] input[data-field="action.slide"]').value
    ).toBe("3");
  });

  test("run highlights target rows and failed runs auto-open the log", async () => {
    await loadApp({
      ok: true,
      status: "failed",
      waiting_for_next: false,
      waiting_reason: "none",
      current_step_id: "ppt_intro",
      completed_steps: 1,
      total_steps: 3,
      log_tail: ["[RUN] FAILED: demo fout"],
      last_error: "demo fout",
    });

    expect(document.querySelector('#stepsCards .step-row[data-index="1"]').classList.contains("is-run-current")).toBe(
      false
    );
    expect(document.getElementById("runLogDetails").open).toBe(true);
  });

  test("run log stays closed after manual close when the same failed state is polled again", async () => {
    await loadApp({
      ok: true,
      status: "failed",
      waiting_for_next: false,
      waiting_reason: "none",
      current_step_id: "ppt_intro",
      completed_steps: 1,
      total_steps: 3,
      log_tail: ["[RUN] FAILED: demo fout"],
      last_error: "demo fout",
    });

    const details = document.getElementById("runLogDetails");
    expect(details.open).toBe(true);
    details.open = false;

    const pollCallback = window.setInterval.mock.calls[0][0];
    pollCallback();
    await flushUi();

    expect(details.open).toBe(false);
  });

  test("idle state with a stale error does not auto-open the run log", async () => {
    await loadApp({
      ok: true,
      status: "idle",
      waiting_for_next: false,
      waiting_reason: "none",
      current_step_id: "",
      completed_steps: 0,
      total_steps: 0,
      log_tail: ["[RUN] eerdere fout"],
      last_error: "eerdere fout",
    });

    expect(document.getElementById("runLogDetails").open).toBe(false);
  });

  test("completed run with an error keeps the warning visible and auto-opens the log", async () => {
    await loadApp({
      ok: true,
      status: "completed",
      waiting_for_next: false,
      waiting_reason: "none",
      current_step_id: "box_demo",
      completed_steps: 3,
      total_steps: 3,
      log_tail: ["[RUN][STEP_ERROR] [2/3] box_demo: robot meldt rust"],
      last_error: "[2/3] box_demo: robot meldt rust",
    });

    expect(document.getElementById("runLogDetails").open).toBe(true);
    expect(document.getElementById("statusMessage").textContent).toContain("Run klaar met melding");
    expect(document.getElementById("statusMessage").textContent).toContain("box_demo");
  });

  test("waiting state highlights current and next rows", async () => {
    await loadApp({
      ok: true,
      status: "waiting",
      waiting_for_next: true,
      waiting_reason: "manual_start",
      current_step_id: "ppt_intro",
      completed_steps: 1,
      total_steps: 3,
      log_tail: ["[2/3] ppt_intro: waiting for continue"],
      last_error: null,
    });

    expect(document.querySelector('#stepsCards .step-row[data-index="1"]').classList.contains("is-run-current")).toBe(
      true
    );
    expect(document.querySelector('#stepsCards .step-row[data-index="2"]').classList.contains("is-run-next")).toBe(
      true
    );
  });

  test("robotstatus section renders live DM state only during an active run", async () => {
    await loadApp(
      {
        ok: true,
        status: "running",
        waiting_for_next: false,
        waiting_reason: "none",
        current_step_id: "say_intro",
        completed_steps: 0,
        total_steps: 3,
        log_tail: [],
        last_error: null,
      },
      {
        fetchImpl: async (url) => {
          if (url === "/api/auto_rest_watch/status") {
            return makeJsonResponse({
              ok: true,
              robots: [
                {
                  robot_id: "nao1",
                  dm_url: "http://127.0.0.1:5301",
                  instance_id: "alex",
                  ok: true,
                  reachable: true,
                  awake: { ok: true, is_awake: true },
                  posture: { ok: true, posture: "Standing" },
                  auto_rest: {
                    enabled_by_config: true,
                    suspended: false,
                    timer_active: true,
                    timeout_s: 180,
                    seconds_until_rest: 177,
                  },
                },
              ],
            });
          }
          return null;
        },
      }
    );

    await waitFor(() => document.getElementById("robotStatusSection").classList.contains("is-hidden") === false);
    const statusText = document.getElementById("robotStatusList").textContent;
    expect(statusText).toContain("nao1");
    expect(statusText).toContain("motoren: wakker");
    expect(statusText).toContain("posture: Standing");
    expect(statusText).not.toContain("auto-rest");
    expect(window.setInterval.mock.calls.find((call) => call[1] === 5000)).toBeTruthy();
    expect(window.setInterval.mock.calls.find((call) => call[1] === 1000)).toBeUndefined();
  });

  test("robotstatus section stays hidden and does not poll while idle", async () => {
    let watchCalls = 0;
    await loadApp(null, {
      fetchImpl: async (url) => {
        if (url === "/api/auto_rest_watch/status") {
          watchCalls += 1;
          return makeJsonResponse({
            ok: true,
            robots: [
              {
                robot_id: "nao1",
                dm_url: "http://127.0.0.1:5301",
                instance_id: "alex",
                ok: true,
                reachable: true,
                awake: { ok: true, is_awake: true },
                posture: { ok: true, posture: "Standing" },
                auto_rest: {
                  enabled_by_config: true,
                  suspended: false,
                  timer_active: true,
                  timeout_s: 180,
                  seconds_until_rest: 177,
                },
              },
            ],
          });
        }
        return null;
      },
    });

    expect(document.getElementById("robotStatusSection").classList.contains("is-hidden")).toBe(true);
    expect(watchCalls).toBe(0);
    expect(window.setInterval.mock.calls.find((call) => call[1] === 5000)).toBeUndefined();
  });

  test("robotstatus section hides again once the run is no longer active", async () => {
    let runStateCalls = 0;
    await loadApp(null, {
      fetchImpl: async (url) => {
        if (url === "/api/run/state") {
          runStateCalls += 1;
          if (runStateCalls === 1) {
            return makeJsonResponse({
              ok: true,
              status: "running",
              waiting_for_next: false,
              waiting_reason: "none",
              current_step_id: "say_intro",
              completed_steps: 0,
              total_steps: 3,
              log_tail: [],
              last_error: null,
            });
          }
          return makeJsonResponse({
            ok: true,
            status: "completed",
            waiting_for_next: false,
            waiting_reason: "none",
            current_step_id: "",
            completed_steps: 3,
            total_steps: 3,
            log_tail: [],
            last_error: null,
          });
        }
        if (url === "/api/auto_rest_watch/status") {
          return makeJsonResponse({
            ok: true,
            robots: [
              {
                robot_id: "nao1",
                dm_url: "http://127.0.0.1:5301",
                instance_id: "alex",
                ok: false,
                  reachable: false,
                  awake: {},
                  posture: {},
                  auto_rest: {},
                  error: "base down",
                  connectivity_issues: [],
                },
              ],
            });
        }
        return null;
      },
    });

    await waitFor(() => document.getElementById("robotStatusSection").classList.contains("is-hidden") === false);
    const statusText = document.getElementById("robotStatusList").textContent;
    expect(statusText).toContain("motoren: niet beschikbaar");
    expect(statusText).toContain("posture: niet beschikbaar");
    expect(statusText).toContain("base down");

    const runPollCallback = window.setInterval.mock.calls.find((call) => call[1] === 500)?.[0];
    expect(typeof runPollCallback).toBe("function");
    await runPollCallback();
    await waitFor(() => document.getElementById("robotStatusSection").classList.contains("is-hidden"));
  });

  test("preload audio injects a script_id and posts generate for the current script", async () => {
    let generatePayload = null;
    await loadApp(null, {
      fetchImpl: async (url, fetchOptions) => {
        if (url === "/api/tts_preload/generate") {
          generatePayload = JSON.parse(fetchOptions.body);
          return makeJsonResponse({
            ok: true,
            script_id: generatePayload.script.meta.script_id,
            has_say_steps: true,
            say_count: 2,
            generated_count: 1,
            reused_count: 0,
            robots: [{ robot_id: "nao1", status: "current_ready", current_ready: true }],
            robots_generation: [{ robot_id: "nao1", status: "generated", generated_count: 1, reused_count: 0 }],
          });
        }
        return null;
      },
    });

    document.getElementById("btnPreloadAudio").click();
    await waitFor(() => document.getElementById("dmStartResults").textContent.includes("preload bijgewerkt"));

    expect(generatePayload).not.toBeNull();
    expect(generatePayload.script.meta.script_id).toBeTruthy();
    expect(document.getElementById("editorJson").value).toContain('"script_id"');
    expect(document.getElementById("saveState").textContent).toContain("Onopgeslagen");
    expect(document.getElementById("dmStartResults").textContent).toContain("preload bijgewerkt");
  });

  test("preload audio shows a busy state and disables start until rendering finishes", async () => {
    let resolveGenerate = null;
    await loadApp(null, {
      fetchImpl: async (url) => {
        if (url === "/api/tts_preload/generate") {
          return new Promise((resolve) => {
            resolveGenerate = () =>
              resolve(
                makeJsonResponse({
                  ok: true,
                  script_id: "script-1",
                  has_say_steps: true,
                  say_count: 2,
                  generated_count: 1,
                  reused_count: 0,
                  robots: [{ robot_id: "nao1", status: "current_ready", current_ready: true }],
                  robots_generation: [{ robot_id: "nao1", status: "generated", generated_count: 1, reused_count: 0 }],
                })
              );
          });
        }
        return null;
      },
    });

    const preloadButton = document.getElementById("btnPreloadAudio");
    const startButton = document.getElementById("btnRunStart");
    preloadButton.click();

    await waitFor(() => preloadButton.disabled && startButton.disabled);
    expect(preloadButton.textContent).toContain("Preload bezig");
    expect(startButton.textContent).toContain("Wacht op preload");
    expect(document.getElementById("statusMessage").textContent).toContain("Preload bezig");

    resolveGenerate();
    await waitFor(() => !preloadButton.disabled && !startButton.disabled);
    expect(preloadButton.textContent).toContain("Preload audio");
    expect(startButton.textContent).toBe("Start");
  });

  test("run start uses current preload policy when status is current_ready", async () => {
    let startPayload = null;
    await loadApp(null, {
      fetchImpl: async (url, fetchOptions) => {
        if (url === "/api/tts_preload/status") {
          return makeJsonResponse({
            ok: true,
            script_id: "script-1",
            has_say_steps: true,
            say_count: 2,
            robots: [
              {
                robot_id: "nao1",
                status: "current_ready",
                current_ready: true,
                existing_ready: false,
                current_missing_count: 0,
                current_profile: { fingerprint: "fp-current", summary: "Azure | current", details: { voice: "current" } },
                existing_profile: null,
              },
            ],
          });
        }
        if (url === "/api/run/start") {
          startPayload = JSON.parse(fetchOptions.body);
          return makeJsonResponse({
            ok: true,
            status: "preflight",
            waiting_for_next: false,
            waiting_reason: "none",
            current_step_id: "",
            completed_steps: 0,
            total_steps: 3,
            log_tail: [],
            last_error: null,
          });
        }
        return null;
      },
    });

    document.getElementById("btnRunStart").click();
    await waitFor(() => startPayload !== null);

    expect(startPayload).not.toBeNull();
    expect(startPayload.tts_preload.policy_by_robot.nao1.mode).toBe("current");
  });

  test("run start can choose an existing preload profile from the dialog", async () => {
    let startPayload = null;
    await loadApp(null, {
      fetchImpl: async (url, fetchOptions) => {
        if (url === "/api/tts_preload/status") {
          return makeJsonResponse({
            ok: true,
            script_id: "script-1",
            has_say_steps: true,
            say_count: 2,
            robots: [
              {
                robot_id: "nao1",
                status: "mismatch_existing",
                current_ready: false,
                existing_ready: true,
                current_missing_count: 2,
                current_profile: { fingerprint: "fp-current", summary: "Azure | nieuw", details: { voice: "nieuw" } },
                existing_profile: { fingerprint: "fp-old", summary: "Azure | oud", details: { voice: "oud" } },
              },
            ],
          });
        }
        if (url === "/api/run/start") {
          startPayload = JSON.parse(fetchOptions.body);
          return makeJsonResponse({
            ok: true,
            status: "preflight",
            waiting_for_next: false,
            waiting_reason: "none",
            current_step_id: "",
            completed_steps: 0,
            total_steps: 3,
            log_tail: [],
            last_error: null,
          });
        }
        return null;
      },
    });

    document.getElementById("btnRunStart").click();
    await waitFor(() => document.querySelector('#actionDialogActions [data-dialog-action="use_existing"]') !== null);

    const existingButton = document.querySelector('#actionDialogActions [data-dialog-action="use_existing"]');
    expect(existingButton).not.toBeNull();
    existingButton.click();
    await waitFor(() => startPayload !== null);

    expect(startPayload).not.toBeNull();
    expect(startPayload.tts_preload.policy_by_robot.nao1.mode).toBe("existing");
    expect(startPayload.tts_preload.policy_by_robot.nao1.profile_fingerprint).toBe("fp-old");
  });

  test("run start shows preload render progress while generating missing audio", async () => {
    let startPayload = null;
    let resolveGenerate = null;
    await loadApp(null, {
      fetchImpl: async (url, fetchOptions) => {
        if (url === "/api/tts_preload/status") {
          return makeJsonResponse({
            ok: true,
            script_id: "script-1",
            has_say_steps: true,
            say_count: 2,
            robots: [
              {
                robot_id: "nao1",
                status: "missing",
                current_ready: false,
                existing_ready: false,
                current_missing_count: 2,
                current_profile: { fingerprint: "fp-current", summary: "Azure | current", details: { voice: "current" } },
                existing_profile: null,
              },
            ],
          });
        }
        if (url === "/api/tts_preload/generate") {
          return new Promise((resolve) => {
            resolveGenerate = () =>
              resolve(
                makeJsonResponse({
                  ok: true,
                  script_id: "script-1",
                  has_say_steps: true,
                  say_count: 2,
                  generated_count: 2,
                  reused_count: 0,
                  robots: [
                    {
                      robot_id: "nao1",
                      status: "current_ready",
                      current_ready: true,
                      existing_ready: false,
                      current_missing_count: 0,
                      current_profile: {
                        fingerprint: "fp-current",
                        summary: "Azure | current",
                        details: { voice: "current" },
                      },
                      existing_profile: null,
                    },
                  ],
                  robots_generation: [{ robot_id: "nao1", status: "generated", generated_count: 2, reused_count: 0 }],
                })
              );
          });
        }
        if (url === "/api/run/start") {
          startPayload = JSON.parse(fetchOptions.body);
          return makeJsonResponse({
            ok: true,
            status: "preflight",
            waiting_for_next: false,
            waiting_reason: "none",
            current_step_id: "",
            completed_steps: 0,
            total_steps: 3,
            log_tail: [],
            last_error: null,
          });
        }
        return null;
      },
    });

    document.getElementById("btnRunStart").click();
    await waitFor(() => document.querySelector('#actionDialogActions [data-dialog-action="preload_now"]') !== null);
    document.querySelector('#actionDialogActions [data-dialog-action="preload_now"]').click();

    await waitFor(() => document.getElementById("btnRunStart").textContent.includes("Wacht op preload"));
    expect(document.getElementById("statusMessage").textContent).toContain("Run wacht op preload");

    resolveGenerate();
    await waitFor(() => startPayload !== null);

    expect(startPayload).not.toBeNull();
    expect(startPayload.tts_preload.policy_by_robot.nao1.mode).toBe("current");
  });

  test("connectivity popup opens after a second active-run poll and bundles multiple robots", async () => {
    let watchCalls = 0;
    await loadApp(
      {
        ok: true,
        status: "preflight",
        waiting_for_next: false,
        waiting_reason: "none",
        current_step_id: "",
        completed_steps: 0,
        total_steps: 3,
        log_tail: [],
        last_error: null,
      },
      {
        fetchImpl: async (url) => {
          if (url === "/api/auto_rest_watch/status") {
            watchCalls += 1;
            if (watchCalls === 1) {
              return makeJsonResponse({
                ok: true,
                robots: [
                  {
                    robot_id: "nao1",
                    dm_url: "http://127.0.0.1:5301",
                    instance_id: "alex",
                    ok: true,
                    reachable: true,
                    awake: { ok: true, is_awake: true },
                    posture: { ok: true, posture: "Standing" },
                    auto_rest: { enabled_by_config: true, suspended: false, timer_active: false, timeout_s: 180, seconds_until_rest: 180 },
                    error: "",
                    connectivity_issues: [],
                  },
                  {
                    robot_id: "nao2",
                    dm_url: "http://127.0.0.1:5302",
                    instance_id: "renee",
                    ok: true,
                    reachable: true,
                    awake: { ok: true, is_awake: true },
                    posture: { ok: true, posture: "Standing" },
                    auto_rest: { enabled_by_config: true, suspended: false, timer_active: false, timeout_s: 180, seconds_until_rest: 180 },
                    error: "",
                    connectivity_issues: [],
                  },
                ],
              });
            }
            return makeJsonResponse({
              ok: true,
              robots: [
                {
                  robot_id: "nao1",
                  dm_url: "http://127.0.0.1:5301",
                  instance_id: "alex",
                  ok: true,
                  reachable: true,
                  awake: { ok: true, is_awake: true },
                  posture: { ok: true, posture: "Standing" },
                  auto_rest: { enabled_by_config: true, suspended: false, timer_active: false, timeout_s: 180, seconds_until_rest: 180 },
                  error: "Base connector is niet bereikbaar.",
                  connectivity_issues: [
                    {
                      issue_key: "dm_local:base_ping",
                      issue_type: "dm_local",
                      severity: "error",
                      message: "Base connector is niet bereikbaar.",
                      source: "runtime_health.base_ping",
                      first_seen_at: "2026-03-20T12:00:00.000Z",
                      active: true,
                    },
                  ],
                },
                {
                  robot_id: "nao2",
                  dm_url: "http://127.0.0.1:5302",
                  instance_id: "renee",
                  ok: false,
                  reachable: false,
                  awake: {},
                  posture: {},
                  auto_rest: {},
                  error: "DM niet bereikbaar: connection refused",
                  connectivity_issues: [
                    {
                      issue_key: "sr_dm:unreachable",
                      issue_type: "sr_dm",
                      severity: "error",
                      message: "DM niet bereikbaar: connection refused",
                      source: "sr.auto_rest_watch",
                      first_seen_at: "2026-03-20T12:00:02.000Z",
                      active: true,
                    },
                  ],
                },
              ],
            });
          }
          return null;
        },
      }
    );

    expect(watchCalls).toBeGreaterThanOrEqual(1);
    expect(actionDialogIsOpen()).toBe(false);

    const pollCallback = window.setInterval.mock.calls.find((call) => call[1] === 5000)?.[0];
    expect(typeof pollCallback).toBe("function");
    await pollCallback();
    await waitFor(() => actionDialogIsOpen());

    expect(document.getElementById("actionDialogTitle").textContent).toContain("Verbindingsverlies");
    expect(document.getElementById("actionDialogBody").textContent).toContain("nao1");
    expect(document.getElementById("actionDialogBody").textContent).toContain("nao2");
    expect(document.getElementById("actionDialogBody").textContent).toContain("Base connector is niet bereikbaar.");
    expect(document.getElementById("actionDialogBody").textContent).toContain("DM niet bereikbaar: connection refused");
  });

  test("connectivity popup stays hidden while the builder is idle", async () => {
    let watchCalls = 0;
    await loadApp(null, {
      fetchImpl: async (url) => {
        if (url === "/api/auto_rest_watch/status") {
          watchCalls += 1;
          return makeJsonResponse({
            ok: true,
            robots: [
              {
                robot_id: "nao1",
                dm_url: "http://127.0.0.1:5301",
                instance_id: "alex",
                ok: true,
                reachable: true,
                awake: { ok: true, is_awake: true },
                posture: { ok: true, posture: "Standing" },
                auto_rest: { enabled_by_config: true, suspended: false, timer_active: false, timeout_s: 180, seconds_until_rest: 180 },
                error: "Base connector is niet bereikbaar.",
                connectivity_issues: [
                  {
                    issue_key: "dm_local:base_ping",
                    issue_type: "dm_local",
                    severity: "error",
                    message: "Base connector is niet bereikbaar.",
                    source: "runtime_health.base_ping",
                    first_seen_at: "2026-03-20T12:00:00.000Z",
                    active: true,
                  },
                ],
              },
            ],
          });
        }
        return null;
      },
    });

    expect(window.setInterval.mock.calls.find((call) => call[1] === 5000)).toBeUndefined();
    expect(watchCalls).toBe(0);
    expect(actionDialogIsOpen()).toBe(false);
  });

  test("connectivity popup closes again after recovery", async () => {
    let watchCalls = 0;
    await loadApp(
      {
        ok: true,
        status: "running",
        waiting_for_next: false,
        waiting_reason: "none",
        current_step_id: "say_intro",
        completed_steps: 0,
        total_steps: 3,
        log_tail: [],
        last_error: null,
      },
      {
        fetchImpl: async (url) => {
          if (url === "/api/auto_rest_watch/status") {
            watchCalls += 1;
            if (watchCalls <= 2) {
              return makeJsonResponse({
                ok: true,
                robots: [
                  {
                    robot_id: "nao1",
                    dm_url: "http://127.0.0.1:5301",
                    instance_id: "alex",
                    ok: true,
                    reachable: true,
                    awake: { ok: true, is_awake: true },
                    posture: { ok: true, posture: "Standing" },
                    auto_rest: { enabled_by_config: true, suspended: false, timer_active: false, timeout_s: 180, seconds_until_rest: 180 },
                    error: "Behavior manager is niet bereikbaar.",
                    connectivity_issues: [
                      {
                        issue_key: "dm_local:behavior_ping",
                        issue_type: "dm_local",
                        severity: "error",
                        message: "Behavior manager is niet bereikbaar.",
                        source: "runtime_health.behavior_ping",
                        first_seen_at: "2026-03-20T12:00:00.000Z",
                        active: true,
                      },
                    ],
                  },
                ],
              });
            }
            return makeJsonResponse({
              ok: true,
              robots: [
                {
                  robot_id: "nao1",
                  dm_url: "http://127.0.0.1:5301",
                  instance_id: "alex",
                  ok: true,
                  reachable: true,
                  awake: { ok: true, is_awake: true },
                  posture: { ok: true, posture: "Standing" },
                  auto_rest: { enabled_by_config: true, suspended: false, timer_active: false, timeout_s: 180, seconds_until_rest: 180 },
                  error: "",
                  connectivity_issues: [],
                },
              ],
            });
          }
          return null;
        },
      }
    );

    const pollCallback = window.setInterval.mock.calls.find((call) => call[1] === 5000)?.[0];
    expect(typeof pollCallback).toBe("function");
    await pollCallback();
    await waitFor(() => actionDialogIsOpen());

    await pollCallback();
    await waitFor(() => actionDialogIsOpen() === false);
  });

  test("run start hard connectivity failure opens the popup immediately", async () => {
    let runStateCalls = 0;
    await loadApp(null, {
      fetchImpl: async (url, fetchOptions) => {
        if (url === "/api/run/state") {
          runStateCalls += 1;
          if (runStateCalls === 1) {
            return makeJsonResponse({
              ok: true,
              status: "idle",
              waiting_for_next: false,
              waiting_reason: "none",
              current_step_id: "",
              completed_steps: 0,
              total_steps: 0,
              log_tail: [],
              last_error: null,
            });
          }
          return makeJsonResponse({
            ok: true,
            status: "failed",
            waiting_for_next: false,
            waiting_reason: "none",
            current_step_id: "",
            completed_steps: 0,
            total_steps: 3,
            log_tail: [],
            last_error: "DM niet bereikbaar: connection refused",
          });
        }
        if (url === "/api/tts_preload/status") {
          return makeJsonResponse({
            ok: true,
            script_id: "script-1",
            has_say_steps: true,
            say_count: 1,
            robots: [
              {
                robot_id: "nao1",
                status: "current_ready",
                current_ready: true,
                existing_ready: false,
                current_missing_count: 0,
                current_profile: { fingerprint: "fp-current", summary: "Azure | current", details: { voice: "current" } },
                existing_profile: null,
              },
            ],
          });
        }
        if (url === "/api/run/start") {
          const payload = JSON.parse(fetchOptions.body);
          expect(payload.tts_preload.policy_by_robot.nao1.mode).toBe("current");
          return makeJsonResponse({ ok: false, error: "DM niet bereikbaar: connection refused" }, 502);
        }
        return null;
      },
    });

    document.getElementById("btnRunStart").click();
    await waitFor(() => actionDialogIsOpen());

    expect(document.getElementById("actionDialogTitle").textContent).toContain("Verbindingsverlies");
    expect(document.getElementById("actionDialogBody").textContent).toContain("Run-start faalde");
    expect(document.getElementById("actionDialogBody").textContent).toContain("nao1");
    expect(document.getElementById("actionDialogBody").textContent).toContain("DM niet bereikbaar: connection refused");
  });
});
