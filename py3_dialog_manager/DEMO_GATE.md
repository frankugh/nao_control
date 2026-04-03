# Demo Gate

`run_demo_gate.py` is an opt-in rehearsal harness for the dialog manager, script runner and summary flow. It is not part of `run_quality_gates.py` and it is meant for demo prep, not for every commit.

## Profiles

- `offline`
  - virtual robot preset
  - continuous listening enabled
  - robot connectors disabled
  - local-safe backends for chat and summary
- `live_services`
  - virtual robot preset
  - continuous listening enabled
  - cloud-backed STT/LLM/TTS for the main runtime
  - local-safe summary fallbacks remain available for recovery
- `live_robot`
  - alex preset by default
  - continuous listening enabled
  - uses the configured robot endpoints
  - starts the base controller through the DM process API when `base_autostart=true`
  - `--nao-ip` overrides the preset value

## Scenarios

- `happy_path_dialog`
  - drives continuous listening with replayed WAV utterances
  - checks dialog replies, command routing and DM state transitions
- `summary_edit_flow`
  - runs `demo_gate_summary_single_robot.json` through the real script runner
  - captures transcript audio, edits the transcript, generates a summary and publishes it
- `service_loss_recovery`
  - runs the summary script and injects connectivity-like STT, LLM and TTS failures
  - validates recovery state, operator actions and terminal completion
- `full_demo_rehearsal`
  - runs the happy-path dialog, summary flow and `demo_gate_workshop_single_robot.json`
  - auto-advances manual script steps and completes embedded summary waits
  - adds robot connectivity, auto-rest and extra settle-time checks only for `live_robot`

## Commands

```powershell
start_demo_gate.bat
py3_dialog_manager\venv\Scripts\python.exe py3_dialog_manager\scripts\run_demo_gate.py --profile offline --scenario all
py3_dialog_manager\venv\Scripts\python.exe py3_dialog_manager\scripts\run_demo_gate.py --profile live_services --scenario all
py3_dialog_manager\venv\Scripts\python.exe py3_dialog_manager\scripts\run_demo_gate.py --profile live_robot --scenario full_demo_rehearsal --nao-ip 192.168.0.101
```

`start_demo_gate.bat` opent een korte wizard. De eerste vraag is `Run default (Y/n)`.

- `Y` start direct `offline + all`, dus alle scenario's zonder services en zonder echte robot.
- `n` vraagt daarna om scenario, live services, echte robot en optionele overrides.
- De wizard toont scenario's als `all`, `chat`, `summary`, `fallbacks` en `rehearsal`.

## Audio Fixtures

Golden replay fixtures live under [demo_gate_audio](/c:/NAOqi_fundamentals/NAO_Controller_WebAPI/py3_dialog_manager/demo_gate_audio). The harness uses those WAV files through `ReplayMic` so the DM continuous-listening path and summary capture path both go through real STT entry points.

## Notes

- The harness creates DM apps in-process with isolated runtime-state directories under `py3_dialog_manager/logs/demo_gate`.
- The console now shows operator-friendly Dutch progress lines instead of raw runner/dry-run output.
- Every run writes an `operator_trace.log` plus a `technical_trace.log` inside the artifact folder.
- `--scenario all` runs the meaningful end-to-end set for a profile without duplicating `chat` and `summary` when `rehearsal` is already included.
- `live_robot` now waits `4s` after `STAND_UP`, `5s` after `WALK_WITH_ME`, `4s` after `STOP`, and `4s` after `REST`; non-robot profiles do not wait.
- Each scenario gets a fresh in-process DM/SR harness instance so replay queues, summary sessions and output state cannot leak between scenarios.
- Artifacts are deleted on success unless `--keep-artifacts` is used.
- On failure artifacts are preserved automatically.
- The default demo-gate scripts are single-robot on purpose: `STAND_UP`, `Hallo allemaal`, `REST`, plus a separate single-robot summary script.
- `live_robot` no longer auto-selects `runtime_port_<port>.json`; it uses the chosen runtime preset directly and fails fast on multi-robot scripts, so one hidden local port preset cannot silently override the selected config.
