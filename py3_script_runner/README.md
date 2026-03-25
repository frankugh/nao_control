# py3_script_runner

Script runner for workshop orchestration across one or more dialog manager (DM) instances.

The web runner is the primary path. The console runner is deprecated and kept only as a temporary legacy path.

## What it does (v1)

- Runs JSON scripts sequentially.
- Waits at startup until all configured DM instances are fully ready
  (DM reachable + required robot/base/behavior connections healthy).
- Supports step start transitions:
  - `manual` (press ENTER)
  - `after_prev` with `delay_s`
  - `with_prev` (starts in parallel with previous step)
- Supports step action types:
  - `say`
  - `do` (`command`, `behavior_start`, `behavior_stop`, `dance`, `nao_set_eye_color`, `summary_start`)
  - `pause`
  - `ppt` (`next_slide`, `previous_slide`, `goto`)
- Uses DM wrapper endpoints:
  - `POST /api/script/say`
  - `POST /api/script/do`
  - `POST /api/nao_set_eye_color`
  - `GET /api/script/capabilities`
 
## PPT capture mode (v1)

- Windows-only, via PowerPoint COM (`pywin32` + desktop PowerPoint required).
- Optional top-level `ppt` section in script:
  - `enabled` (default `false`)
  - `file` (required when enabled)
  - `fullscreen_required` (default `true`, set `false` for windowed slideshow test mode)
  - `start_capture_on_run` (default `true`)
- Capture controls:
  - `c`: toggle capture ON/OFF
  - when capture is OFF: `ENTER` = next slide, `p` = previous slide, `q` = quit prompt
- Runner logs capture and position events:
  - `[CAPTURE] ON/OFF`
  - `[PPT] slide=X build=Y`
  - `[SYNC] mismatch detected -> hard pause`
  - `[PPT] snapback to slide/build`

## Install

From repository root:

```powershell
python -m venv py3_script_runner\venv
py3_script_runner\venv\Scripts\python.exe -m pip install -r py3_script_runner\requirements.txt
```

## Legacy console runner

```powershell
py3_script_runner\venv\Scripts\python.exe -m py3_script_runner.cli --script py3_script_runner\scripts\example_workshop.json
```

Als je al in `py3_script_runner` staat:

```powershell
python .\cli.py --script .\scripts\example_workshop.json
```

## Script builder webapp

Start de losse script builder (lokale static webapp):

```powershell
py3_script_runner\venv\Scripts\python.exe -m py3_script_runner.script_runner_app
```

Default URL:

`http://127.0.0.1:8765/`

Features:

- JSON editor met `Nieuw`, `Laad`, `Opslaan`, `Opslaan als`
- `Nieuw` zet direct een valide default script
- Add-blok paneel met templates voor `NAO gedrag`, `PPTX`, `Summary`
- Bewerkbaar template preview veld met kopieerknop en auto-insert in `steps`
- `Start DM's` knop die per robot een lokale DM in een apart `cmd.exe` venster start

Frontend tests (Script Builder web):

```powershell
cd py3_script_runner\script_builder_web
npm install
npm test
```

Let op: voor echte open/save dialogs gebruikt de UI de Chromium File System Access API (Edge/Chrome).

## Script format (summary)

- `version` must be `1`
- `robots` maps `robot_id -> { dm_url }`
  - optional: `instance_id` for local DM autostart from the Script Builder webapp
- `defaults`:
  - `request_timeout_s` (number > 0)
  - `readiness_poll_interval_s` (number > 0, default `3`)
  - `readiness_request_timeout_s` (number > 0, default `3`)
  - `readiness_timeout_s` (number >= 0, default `0` = wait indefinitely)
  - `on_error` (`prompt|abort|continue`)
- `steps[]`:
  - `id` unique
  - `start.mode`: `manual|after_prev|with_prev`
  - `start.delay_s` for `after_prev`
  - action types:
    - `say`: requires `robot_id`, `action.text`
    - `do`: requires `robot_id`, `action.mode`
      - `nao_set_eye_color`: requires `action.color`, optional `action.duration` (seconds, `>= 0`)
      - `summary_start`: optional `action.wait_for_complete` (bool, default `true`)
      - `summary_start`: optional `action.open_on_new_tab` (bool, default `false`)
    - `pause`: requires `action.seconds`
    - `ppt`: `action.mode` is `next_slide|previous_slide|goto`
      - `goto`: requires `action.slide`, optional `action.click` (0 = slide start state)

- optional top-level `ppt`:
  - `enabled` (bool, default `false`)
  - `file` (required when `enabled=true`)
  - `fullscreen_required` (bool, default `true`)
  - `start_capture_on_run` (bool, default `true`)

See `scripts/example_workshop.json` for a complete DM example, `scripts/example_workshop_summary.json` for summary-only testing, and `scripts/example_workshop_ppt.json` for PPT capture usage.

### Local DM autostart from Script Builder

The `Start DM's` button in the webapp reads `robots.<id>.dm_url` and optional `robots.<id>.instance_id` from the current script.

Example:

```json
{
  "robots": {
    "nao1": {
      "dm_url": "http://127.0.0.1:5301",
      "instance_id": "alex"
    }
  }
}
```

Notes:

- only local DM targets are supported (`127.0.0.1`, `localhost`, `0.0.0.0`, `::1`)
- `dm_url` must include an explicit port
- DM runtime state stays on the DM side via `runtime_<instance>.json`

By default, the runner executes against the current runtime config already active on each DM instance.

The runner uses a persistent HTTP session per robot, so one stable DM `sid` is reused across all steps.

### Summary flow

Use one `do` step on the same robot:

1. `summary_start`: starts DM summary on `/summary`.
2. If `wait_for_complete=true`, the script waits until the summary is `completed`.
3. If `wait_for_complete=false`, the script continues immediately while the summary stays active in DM.

The Script Builder run screen shows a sticky summary banner with:

- `Open summary`
- `Samenvatting annuleren`

Summary-specific capture, prompt and publish tuning belongs in DM summary settings, not in the script file.

### Eye color example snippet

```json
{
  "id": "eye_blue",
  "robot_id": "nao1",
  "start": { "mode": "after_prev", "delay_s": 0 },
  "action": {
    "type": "do",
    "mode": "nao_set_eye_color",
    "color": "#00AEEF",
    "duration": 0.35
  }
}
```

### PPT example snippet

```json
{
  "ppt": {
    "enabled": true,
    "file": "C:/workshops/demo.pptx",
    "fullscreen_required": true,
    "start_capture_on_run": true
  },
  "steps": [
    {
      "id": "p1",
      "start": { "mode": "manual" },
      "action": { "type": "ppt", "mode": "next_slide" }
    },
    {
      "id": "p2",
      "start": { "mode": "with_prev" },
      "action": { "type": "ppt", "mode": "goto", "slide": 5, "click": 2 }
    }
  ]
}
```

## Error handling

Default `on_error` is `prompt`:

- `r` / `retry`: execute current step again
- `n` / `next`: skip step and continue
- `a` / `abort`: stop run

For timeout-like request errors while `on_error=prompt`, the runner defaults to `next` automatically.
Use per-step `request_timeout_s` for long operations such as `summary_start`.

## Startup readiness behavior

Before executing steps, the runner enters a readiness phase and prints per-robot status lines:

- `READY`: DM and required connections are up.
- `WAIT`: still waiting; includes reason (for example `base connector down`, `behavior manager down`, `nao tcp down`, or `DM down`).

While waiting, it polls again every `readiness_poll_interval_s` (default: 3s) and prints progress.
This helps you decide whether to wait or inspect Agent config / process status in NAO Studio.

## Logs

Run logs are written to:

`py3_script_runner/logs/run_YYYYMMDD_HHMMSS.log`
