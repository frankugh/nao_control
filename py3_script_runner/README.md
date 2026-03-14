# py3_script_runner

Console script runner for workshop orchestration across one or more dialog manager (DM) instances.

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
  - `do` (`command`, `behavior_start`, `behavior_stop`, `dance`, `nao_set_eye_color`, `summary_capture_start`, `summary_capture_stop_and_draft`, `summary_publish`, `summary_cancel`)
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

## Run

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
  - optional: `runtime_config` (same keys as DM `/api/runtime_config`)
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
      - `summary_capture_start`: optional `action.hold_until_continue` (bool, default `true`)
      - `summary_capture_stop_and_draft`: requires `action.input_prompt_template`
      - optional for `summary_capture_stop_and_draft`: `action.instruction`, `action.system_prompt`, `action.system_prompt_file`
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
- the button ignores `runtime_config`; DM runtime state stays on the DM side via `runtime_<instance>.json`

By default, the runner executes against the current runtime config already active on each DM instance.

### Optional per-robot runtime config

You can force each DM instance to a known runtime setup at preflight:

```json
{
  "robots": {
    "nao1": {
      "dm_url": "http://127.0.0.1:5301",
      "runtime_config": {
        "nao_ip": "192.168.68.101",
        "nao_base_url": "http://127.0.0.1:5101",
        "output_target": "nao",
        "tts_engine": "azure"
      }
    }
  }
}
```

The runner uses a persistent HTTP session per robot, so one stable DM `sid` is reused across all steps.

### Summary flow (3-step)

Use three `do` steps on the same robot:

1. `summary_capture_start`: starts capture and keeps capturing until operator presses ENTER to continue.
2. `summary_capture_stop_and_draft`: stops capture, returns `draft + transcript`, prints the draft in the runner log, then asks `[p]ublish / [c]ancel` (ENTER defaults to publish).
3. `summary_publish`: if step 2 chose publish, this step speaks the summary; if step 2 chose cancel, this step is skipped.

For less clipped utterance starts, set a larger continuous mic pre-roll in `robots.<id>.runtime_config.mic_params_continuous.pre_roll_ms` (typically `800-1000` ms).

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
Use per-step `request_timeout_s` for long operations such as `summary_publish`.

## Startup readiness behavior

Before executing steps, the runner enters a readiness phase and prints per-robot status lines:

- `READY`: DM and required connections are up.
- `WAIT`: still waiting; includes reason (for example `base connector down`, `behavior manager down`, `nao tcp down`, or `DM down`).

While waiting, it polls again every `readiness_poll_interval_s` (default: 3s) and prints progress.
This helps you decide whether to wait or inspect Agent config / process status in NAO Studio.

## Logs

Run logs are written to:

`py3_script_runner/logs/run_YYYYMMDD_HHMMSS.log`
