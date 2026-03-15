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
        instance_id: "alex",
      },
    },
    defaults: { request_timeout_s: 12, on_error: "prompt" },
    steps: [
      {
        id: "s1",
        robot_id: "nao1",
        start: { mode: "manual" },
        action: { type: "say", text: "hallo" },
      },
    ],
  },
  catalog: [
    {
      category_key: "basic",
      category_label: "Basic",
      templates: [
        {
          template_key: "say",
          template_label: "say",
          snippet: {
            id: "tmpl_1",
            robot_id: "nao1",
            start: { mode: "manual" },
            action: { type: "say", text: "hallo" },
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

async function loadApp(fetchMock) {
  document.open();
  document.write(INDEX_HTML);
  document.close();
  globalThis.fetch = fetchMock;
  window.fetch = fetchMock;
  vi.resetModules();
  await import("../app.js");
  await flushUi();
}

describe("dm start button", () => {
  beforeEach(() => {
    vi.spyOn(window, "setInterval").mockReturnValue(1);
    vi.spyOn(window, "clearInterval").mockImplementation(() => {});
  });

  afterEach(() => {
    vi.restoreAllMocks();
    document.body.innerHTML = "";
  });

  test("posts the current script and renders the launch result", async () => {
    const fetchMock = vi.fn(async (url, options) => {
      if (url === "./templates.json") {
        return makeJsonResponse(TEMPLATE_FIXTURE);
      }
      if (url === "/api/run/state") {
        return makeJsonResponse({
          ok: true,
          status: "idle",
          waiting_for_next: false,
          waiting_reason: "none",
          current_step_id: "",
          completed_steps: 0,
          total_steps: 0,
          log_tail: [],
        });
      }
      if (url === "/api/dm/start") {
        return makeJsonResponse({
          ok: true,
          started_count: 1,
          error_count: 0,
          message: "1 DM gestart.",
          results: [
            {
              robot_id: "nao1",
              dm_url: "http://127.0.0.1:5301",
              instance_id: "alex",
              started: true,
              message: "DM gestart in een nieuw cmd-venster.",
            },
          ],
        });
      }
      throw new Error(`Unexpected fetch: ${String(url)}`);
    });

    await loadApp(fetchMock);

    document.getElementById("btnDmStart").click();
    await flushUi();

    const dmStartCall = fetchMock.mock.calls.find((entry) => entry[0] === "/api/dm/start");
    expect(dmStartCall).toBeTruthy();
    const body = JSON.parse(dmStartCall[1].body);
    expect(body.script.robots.nao1.dm_url).toBe("http://127.0.0.1:5301");
    expect(body.script.robots.nao1.instance_id).toBe("alex");
    expect(document.getElementById("statusMessage").textContent).toContain("1 DM gestart");
    expect(document.getElementById("dmStartResults").textContent).toContain("nao1 gestart");
  });

  test("blocks the DM start request on invalid editor JSON", async () => {
    const fetchMock = vi.fn(async (url, options) => {
      if (url === "./templates.json") {
        return makeJsonResponse(TEMPLATE_FIXTURE);
      }
      if (url === "/api/run/state") {
        return makeJsonResponse({
          ok: true,
          status: "idle",
          waiting_for_next: false,
          waiting_reason: "none",
          current_step_id: "",
          completed_steps: 0,
          total_steps: 0,
          log_tail: [],
        });
      }
      if (url === "/api/dm/start") {
        return makeJsonResponse({ ok: true, results: [] });
      }
      throw new Error(`Unexpected fetch: ${String(url)}`);
    });

    await loadApp(fetchMock);

    document.getElementById("btnTabJson").click();
    const editor = document.getElementById("editorJson");
    editor.value = "{ invalid";
    editor.dispatchEvent(new Event("input", { bubbles: true }));

    document.getElementById("btnDmStart").click();
    await flushUi();

    expect(fetchMock.mock.calls.filter((entry) => entry[0] === "/api/dm/start")).toHaveLength(0);
    expect(document.getElementById("statusMessage").textContent).toContain("parsefout");
  });

  test("clears prior DM launch errors when starting a run", async () => {
    const fetchMock = vi.fn(async (url) => {
      if (url === "./templates.json") {
        return makeJsonResponse(TEMPLATE_FIXTURE);
      }
      if (url === "/api/run/state") {
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
      if (url === "/api/dm/start") {
        return makeJsonResponse({
          ok: true,
          started_count: 0,
          error_count: 1,
          message: "0 DM gestart.",
          results: [
            {
              robot_id: "nao1",
              dm_url: "http://127.0.0.1:5301",
              instance_id: "alex",
              started: false,
              message: "DM niet gestart.",
            },
          ],
        });
      }
      if (url === "/api/run/start") {
        return makeJsonResponse({
          ok: true,
          status: "preflight",
          waiting_for_next: false,
          waiting_reason: "none",
          current_step_id: "",
          completed_steps: 0,
          total_steps: 1,
          log_tail: ["[RUN] gestart"],
          last_error: null,
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
      throw new Error(`Unexpected fetch: ${String(url)}`);
    });

    await loadApp(fetchMock);

    document.getElementById("btnDmStart").click();
    await flushUi();

    expect(document.getElementById("dmStartResults").textContent).toContain("niet gestart");

    document.getElementById("btnRunStart").click();
    await waitFor(() => document.getElementById("statusMessage").textContent.includes("Run gestart"));

    expect(document.getElementById("dmStartResults").classList.contains("is-hidden")).toBe(true);
    expect(document.getElementById("statusMessage").textContent).toContain("Run gestart");
  });
});
