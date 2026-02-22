# py3_story_engine

Standalone storyteller runtime (text in, text out), zonder afhankelijkheid op `py3_dialog_manager`.

## Features v1

- Interactieve CLI (REPL) voor story turns.
- Twee LLM rollen per turn: `state_updater` + `storyteller`.
- Alleen Ollama backends: `ollama_local` en `ollama_cloud`.
- Autosave per sessie + `/save` en `/load`.
- JSONL turn logging (standaard aan).
- Debugmodus: `--debug` en runtime toggle `/debug on|off`.

## Install

```powershell
cd py3_story_engine
pip install -r requirements.txt
```

## Run

```powershell
storyteller-cli --config config.json
```

of

```powershell
python -m storyteller.cli --config config.json
```

## Voorbeeld config (`config.json`)

```json
{
  "storyteller": {
    "type": "ollama_cloud",
    "model": "gpt-oss:120b",
    "host": "https://ollama.com",
    "api_key": null,
    "temperature": 0.7,
    "top_p": 0.9,
    "top_k": 40,
    "prompt_file": "prompts/storyteller_v1.txt"
  },
  "state_updater": {
    "type": "ollama_cloud",
    "model": "gpt-oss:20b",
    "host": "https://ollama.com",
    "api_key": null,
    "temperature": 0.2,
    "top_p": 0.9,
    "top_k": 40,
    "prompt_file": "prompts/state_updater_v1.txt"
  },
  "runtime": {
    "strict_json": true,
    "required_language": "nl",
    "invalid_retry_count": 1,
    "max_history_turns": 3,
    "require_global_summary": true,
    "beat_gate_policy": "conservative_v1",
    "session_id": "local_cli",
    "debug": false,
    "log_file": "auto"
  }
}
```

Formeel schema: `config.schema.json`
Snelle starter: `config.example.json`
Alternatief (storyteller = `mistral-large-675b-cloud`): `config.storyteller_mistral.example.json`

## REPL commands

- `/help`
- `/debug on|off`
- `/state`
- `/save <name>`
- `/load <name>`
- `/saves`
- `/history [n]`
- `/quit`

## Runtime data

- Runtime root: `py3_story_engine/runtime/story`
- Autosave: per `session_id`
- Turn logs: JSONL per sessie (uitzetbaar met `--no-log` of `runtime.log_file = null`)

## Tests

```powershell
pytest
```
