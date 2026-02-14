# py3_script_runner

Console script runner for workshop orchestration across one or more dialog manager (DM) instances.

## What it does (v1)

- Runs JSON scripts sequentially.
- Waits at startup until all configured DM instances are fully ready
  (DM reachable + required robot/base/behavior connections healthy).
- Supports step start transitions:
  - `manual` (press ENTER)
  - `after_prev` with `delay_s`
- Supports step action types:
  - `say`
  - `do` (`command`, `behavior_start`, `behavior_stop`, `dance`)
  - `pause`
- Uses DM wrapper endpoints:
  - `POST /api/script/say`
  - `POST /api/script/do`
  - `GET /api/script/capabilities`

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

## Script format (summary)

- `version` must be `1`
- `robots` maps `robot_id -> { dm_url }`
  - optional: `runtime_config` (same keys as DM `/api/runtime_config`)
- `defaults`:
  - `request_timeout_s` (number > 0)
  - `readiness_poll_interval_s` (number > 0, default `3`)
  - `readiness_request_timeout_s` (number > 0, default `3`)
  - `readiness_timeout_s` (number >= 0, default `0` = wait indefinitely)
  - `on_error` (`prompt|abort|continue`)
- `steps[]`:
  - `id` unique
  - `start.mode`: `manual|after_prev`
  - `start.delay_s` for `after_prev`
  - action types:
    - `say`: requires `robot_id`, `action.text`
    - `do`: requires `robot_id`, `action.mode`
    - `pause`: requires `action.seconds`

See `scripts/example_workshop.json` for a complete example.

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

## Error handling

Default `on_error` is `prompt`:

- `r` / `retry`: execute current step again
- `n` / `next`: skip step and continue
- `a` / `abort`: stop run

## Startup readiness behavior

Before executing steps, the runner enters a readiness phase and prints per-robot status lines:

- `READY`: DM and required connections are up.
- `WAIT`: still waiting; includes reason (for example `base connector down`, `behavior manager down`, `nao tcp down`, or `DM down`).

While waiting, it polls again every `readiness_poll_interval_s` (default: 3s) and prints progress.
This helps you decide whether to wait or inspect Agent config / process status in NAO Studio.

## Logs

Run logs are written to:

`py3_script_runner/logs/run_YYYYMMDD_HHMMSS.log`
