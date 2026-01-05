PROJECT HANDOVER — py3_nao_behavior_manager (NAO dialog manager)

DOEL
We bouwen een modulaire dialoog-pipeline voor een NAO robot demo (en laptop testing). Architectuur:
Input -> (optioneel STT) -> LLM -> Output.
Alles configureerbaar via 1 JSON per run (configs/*). Het doel is plug-and-play wisselen tussen:
- input: console typing of audio opnemen
- stt: whisper (later eventueel vosk)
- llm: none/echo, ollama local, ollama cloud
- output: console print, NAO TTS, none

BELANGRIJKE BESTANDEN
- dialog/interfaces.py: definieert interfaces/dataclasses (ChatMessage, UserInput, LLMResult, etc.)
- dialog/pipeline.py: InputLLMOutputPipeline, doet:
  - user input ophalen
  - messages samenstellen uit history + user message
  - system prompt injecteren als eerste message (optioneel)
  - context trimming via llm.params.context.max_history_turns (optioneel)
  - logging van exacte messages die naar LLM gaan (JSONL)
  - llm.generate(messages) en output.emit(reply)
- dialog/pipeline_builder.py: build_pipeline_from_json/config
  - leest JSON schema
  - maakt input backend (console/audio)
  - maakt STT backend (whisper)
  - maakt LLM backend (none/echo/ollama local/cloud)
  - maakt output backend (console/nao/none)
  - ondersteunt system_prompt of system_prompt_file
  - ondersteunt llm.params.context.max_history_turns
- dialog/backends/stt_whisper.py:
  - faster-whisper
  - ondersteunt mode AUTO|CPU|GPU + model_cpu/model_gpu + compute_type_cpu/compute_type_gpu
  - vad_filter + min_silence_duration_ms

JSON CONFIG SCHEMA (BELANGRIJK)
Top-level keys:
{
  "run": {
    "status_to_console": bool,
    "log_messages": bool,
    "log_dir": "logs",
    "log_messages_path": "logs/whatever.jsonl"
  },
  "input": {
    "type": "console" | "audio",
    "params": { ... },
    (if audio) "mic": { "type": "laptop" | "nao_ssh", "params": {...} },
              "stt": { "type": "whisper", "params": {...} }
  },
  "llm": {
    "type": "none" | "echo" | "ollama_local" | "ollama_cloud",
    "params": {
      "host": "...",
      "model": "...",
      "api_key": "...",
      "system_prompt": "..." OR "system_prompt_file": "relative/to/config/dir.txt",
      "context": { "max_history_turns": int }
    }
  },
  "output": { "type": "console" | "nao" | "none", "params": {...} }
}

CONTEXT TRIMMING DEFINITIE
llm.params.context.max_history_turns = N betekent:
- “turns” tellen als user-messages
- trimming gebeurt zó dat de laatste N user-messages (inclusief de huidige user turn) bewaard blijven, samen met bijbehorende assistant messages erna
- system prompt blijft behouden als eerste message

SYSTEM PROMPT
We gebruiken 1 system prompt (“master prompt”), via string of file.
Doel: model consistent in rol (NAO), kort, geen meta/disclaimers, 1 vervolgvraag.
Kleine modellen (gemma:2b) zijn rommelig NL; llama3.1:8b is beter.

LOGGING (BELANGRIJK)
Per turn schrijven we JSONL met exact ‘messages’ die naar de LLM gaan, met UTC timestamp, turn_idx, meta.
Logs staan in logs/ (in .gitignore).
Logging gebeurt vlak vóór llm.generate(messages) en flushes meteen (crash-safe).

RUNNEN
- python -m scripts.run_from_json --config configs/<file>.json
- Er is een powershell launcher script om UTF-8/emoji goed te houden.
- Ollama local host: http://localhost:11434
- Models list: `ollama list`

HUIDIGE STATUS
- Console input + echo/none + console output werkt
- Audio input (laptop mic) + Whisper STT + echo werkt
- Audio input + Whisper STT + Ollama local/cloud werkt
- max_history_turns werkt en is zichtbaar in logs
- system_prompt_file werkt en is zichtbaar in logs

WAT IK VAN DE NIEUWE CHAT WIL
- Ga uit van bestaande codebase; vraag niet om alles opnieuw te ontwerpen.
- Werk incrementeel: geef complete file replacements als iets aangepast moet worden.
- Houd JSON schema consistent.
- Bij twijfel: baseer op de aangeleverde kernbestanden en logs.
- Voor nieuwe features: eerst minimale verandering, dan uitbreiden.

TYPISCHE VOLGENDE STAPPEN (VOORBEELDEN)
- betere system prompt / master prompt tuning
- context strategie uitbreiden (samenvatten, pinned facts)
- vosk backend toevoegen
- NAO-specific output tuning (korte TTS, interruption, status cues)
- UX: “listening/transcribing/thinking/speaking” cues
