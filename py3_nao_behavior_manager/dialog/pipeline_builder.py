# py3_nao_behavior_manager/dialog/pipeline_builder.py
from __future__ import annotations

import json
import os
from typing import Any, Dict

from dialog.pipeline import InputLLMOutputPipeline

# input backends
from dialog.backends.input_audio import AudioInputBackend
from dialog.backends.input_console import ConsoleInputBackend

# mic/stt backends
from dialog.backends.mic_laptop import LaptopMic
from dialog.backends.mic_nao_ssh import NaoSshMic
from dialog.backends.stt_whisper import WhisperSTTBackend

# llm backends
from dialog.backends.llm_echo import EchoLLMBackend
from dialog.backends.llm_none import NoOpLLMBackend
from dialog.backends.llm_ollama import OllamaClient, OllamaLLMBackend

# output backends
from dialog.backends.output_console import ConsoleOutputBackend
from dialog.backends.output_none import NoOpOutputBackend
from dialog.backends.output_nao import NaoTTSOutputBackend


JsonLike = Dict[str, Any]


def _load_json(path: str) -> JsonLike:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _expand_env(value: Any) -> Any:
    if isinstance(value, str):
        out = value
        for _ in range(10):
            start = out.find("${")
            if start == -1:
                break
            end = out.find("}", start + 2)
            if end == -1:
                break
            var = out[start + 2 : end]
            out = out[:start] + os.environ.get(var, "") + out[end + 1 :]
        return out
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    return value


def _req(d: JsonLike, key: str) -> Any:
    if key not in d:
        raise ValueError(f"Config mist verplicht veld: {key!r}")
    return d[key]


def _make_mic(mic_cfg: JsonLike):
    t = _req(mic_cfg, "type").lower()
    p = mic_cfg.get("params", {}) or {}
    if t == "laptop":
        return LaptopMic(**p)
    if t == "nao_ssh":
        return NaoSshMic(**p)
    raise ValueError(f"Onbekende mic.type: {t!r}")


def _make_stt(stt_cfg: JsonLike):
    t = _req(stt_cfg, "type").lower()
    p = stt_cfg.get("params", {}) or {}
    if t == "whisper":
        return WhisperSTTBackend(**p)
    if t == "vosk":
        raise NotImplementedError("Vosk nog niet toegevoegd in deze builder.")
    raise ValueError(f"Onbekende stt.type: {t!r}")


def _make_input(cfg: JsonLike):
    input_cfg = _req(cfg, "input")
    t = _req(input_cfg, "type").lower()
    p = input_cfg.get("params", {}) or {}

    if t == "console":
        return ConsoleInputBackend(**p)

    if t == "audio":
        mic = _make_mic(_req(input_cfg, "mic"))
        stt = _make_stt(_req(input_cfg, "stt"))
        return AudioInputBackend(mic=mic, stt=stt, **p)

    raise ValueError(f"Onbekende input.type: {t!r}")


def _make_llm(cfg: JsonLike):
    llm_cfg = _req(cfg, "llm")
    t = _req(llm_cfg, "type").lower()
    p = llm_cfg.get("params", {}) or {}

    if t == "echo":
        return EchoLLMBackend()

    if t == "none":
        return NoOpLLMBackend()

    # --- OLLAMA LOCAL (geen api_key verplicht) ---
    if t in ("ollama_local",):
        host = p.get("host", "http://localhost:11434")
        model = p.get("model", "llama3.2")
        # api_key optioneel; voor local meestal None/"".
        api_key = p.get("api_key", None)
        client = OllamaClient(model=model, host=host, api_key=api_key)
        return OllamaLLMBackend(client)

    # --- OLLAMA CLOUD (api_key verplicht) ---
    if t in ("ollama", "ollama_cloud"):
        api_key = p.get("api_key") or os.environ.get("OLLAMA_API_KEY")
        if not api_key:
            raise RuntimeError("OLLAMA_API_KEY ontbreekt (zet env var of llm.params.api_key).")

        host = p.get("host", os.environ.get("OLLAMA_HOST", "https://ollama.com"))
        model = p.get("model", os.environ.get("OLLAMA_MODEL", "gpt-oss:120b"))

        client = OllamaClient(model=model, host=host, api_key=api_key)
        return OllamaLLMBackend(client)

    raise ValueError(f"Onbekende llm.type: {t!r}")


def _make_output(cfg: JsonLike):
    out_cfg = _req(cfg, "output")
    t = _req(out_cfg, "type").lower()
    p = out_cfg.get("params", {}) or {}

    if t == "console":
        return ConsoleOutputBackend(**p)
    if t == "none":
        return NoOpOutputBackend()
    if t in ("nao_tts", "nao_py2", "nao"):
        return NaoTTSOutputBackend(**p)

    raise ValueError(f"Onbekende output.type: {t!r}")


def build_pipeline_from_config(cfg: JsonLike) -> InputLLMOutputPipeline:
    cfg = _expand_env(cfg)

    run_cfg = cfg.get("run", {}) or {}
    status_to_console = bool(run_cfg.get("status_to_console", True))

    input_backend = _make_input(cfg)
    llm = _make_llm(cfg)
    output = _make_output(cfg)

    return InputLLMOutputPipeline(
        input_backend=input_backend,
        llm=llm,
        output_backend=output,
        status_to_console=status_to_console,
    )


def build_pipeline_from_json(path: str) -> InputLLMOutputPipeline:
    return build_pipeline_from_config(_load_json(path))
