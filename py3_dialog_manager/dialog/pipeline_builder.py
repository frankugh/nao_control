# py3_nao_behavior_manager/dialog/pipeline_builder.py
from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any, Dict, Optional

from dialog.pipeline import InputLLMOutputPipeline
from dialog.interfaces import parse_cmdrec_bundle, parse_confirm_method
from dialog.command_recognizer import CmdRecRecognizer
from dialog.behavior_executor import BehaviorExecutor, ConsoleAndBehaviorExecutor, PrintBehaviorExecutor
from dialog.nao_api_router import NaoApiRouter

# input backends
from dialog.backends.input_audio import AudioInputBackend
from dialog.backends.input_console import ConsoleInputBackend

# mic/stt backends
from dialog.backends.mic_laptop import LaptopMic
from dialog.backends.mic_nao_ssh import NaoSshMic
from dialog.backends.stt_azure import AzureSTTBackend
from dialog.backends.stt_whisper import WhisperSTTBackend
from dialog.backends.stt_vosk import VoskSTTBackend

# llm backends
from dialog.backends.llm_echo import EchoLLMBackend
from dialog.backends.llm_none import NoOpLLMBackend
from dialog.backends.llm_ollama import OllamaClient, OllamaLLMBackend

# output backends
from dialog.backends.output_console import ConsoleOutputBackend
from dialog.backends.output_none import NoOpOutputBackend
from dialog.backends.output_nao import NaoTTSOutputBackend
from dialog.backends.output_router import OutputRouterBackend


JsonLike = Dict[str, Any]

_COMMAND_DESCRIPTIONS = {
    "BOX": "boks geven",
    "HIGH_FIVE": "high five geven",
    "WAVE": "zwaaien",
    "WALK_WITH_ME": "meelopen en hand vasthouden",
    "LOCOMOTION_REQUEST": "lopen of draaien (richting op basis van je zin)",
    "STOP": "stoppen",
    "SITDOWN": "gaan zitten",
    "STAND_UP": "opstaan",
    "REST": "in ruststand gaan",
    "DANCE": "een dansje uitvoeren (eventueel met naam)",
}


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


def _read_text_file(path: str, base_dir: str) -> str:
    p = path
    if not os.path.isabs(p):
        p = os.path.join(base_dir, p)
    with open(p, "r", encoding="utf-8") as f:
        return f.read()


def _extract_system_prompt(cfg: JsonLike, *, config_path: str) -> Optional[str]:
    llm_cfg = cfg.get("llm", {}) or {}
    params = (llm_cfg.get("params", {}) or {})

    sp = params.get("system_prompt", None)
    spf = params.get("system_prompt_file", None)

    if sp and spf:
        raise ValueError("Gebruik óf llm.params.system_prompt óf llm.params.system_prompt_file, niet allebei.")

    if sp:
        if not isinstance(sp, str):
            raise ValueError("llm.params.system_prompt moet een string zijn.")
        return sp

    if spf:
        if not isinstance(spf, str):
            raise ValueError("llm.params.system_prompt_file moet een string pad zijn.")
        base_dir = os.path.dirname(os.path.abspath(config_path)) if config_path and config_path != "<memory>" else os.getcwd()
        return _read_text_file(spf, base_dir)

    return None


def _extract_max_history_turns(cfg: JsonLike) -> Optional[int]:
    """
    Nieuwe plek: llm.params.context.max_history_turns
    Legacy fallback: run.max_history_turns
    """
    llm_cfg = cfg.get("llm", {}) or {}
    params = (llm_cfg.get("params", {}) or {})
    ctx = params.get("context", {}) or {}
    if not isinstance(ctx, dict):
        raise ValueError("llm.params.context moet een object/dict zijn.")

    v = ctx.get("max_history_turns", None)
    if v is None:
        # legacy fallback
        run_cfg = cfg.get("run", {}) or {}
        v = run_cfg.get("max_history_turns", None)

    if v is None:
        return None
    if not isinstance(v, int):
        raise ValueError("max_history_turns moet een int zijn (of weglaten).")
    return v


def _extract_runtime_context_enabled(cfg: JsonLike) -> bool:
    llm_cfg = cfg.get("llm", {}) or {}
    params = (llm_cfg.get("params", {}) or {})
    ctx = params.get("context", {}) or {}
    if not isinstance(ctx, dict):
        raise ValueError("llm.params.context moet een object/dict zijn.")
    return bool(ctx.get("inject_runtime_context", False))


def _format_available_commands(cmdrec_recognizer: Optional[CmdRecRecognizer]) -> str:
    if cmdrec_recognizer is None:
        return ""
    labels = cmdrec_recognizer.get_labels()
    if not labels:
        return ""
    items = sorted({label for label in labels if label and label.upper() != "NONE"})
    lines = []
    for label in items:
        desc = _COMMAND_DESCRIPTIONS.get(label.upper())
        if desc:
            lines.append(f"- {label}: {desc}")
        else:
            lines.append(f"- {label}")
    return "\n".join(lines)


def _format_dance_catalog(cmdrec_recognizer: Optional[CmdRecRecognizer]) -> str:
    if cmdrec_recognizer is None:
        return ""
    dances = cmdrec_recognizer.get_dance_catalog()
    if not dances:
        return ""
    lines = []
    for dance in dances:
        key = dance.get("key")
        if not isinstance(key, str) or not key.strip():
            continue
        lines.append(f"- {key}")
    return "\n".join(lines)


def _preload_cmdrec(cmdrec_recognizer: Optional[CmdRecRecognizer]) -> None:
    if cmdrec_recognizer is None:
        return
    # Force model bundle load now to avoid first-utterance lag.
    cmdrec_recognizer.get_labels()


def _extract_cmdrec_config(cfg: JsonLike) -> JsonLike:
    cmdrec = parse_cmdrec_bundle(cfg.get("cmdrec", "none"))
    cmdrec_bundles_dir = cfg.get("cmdrec_bundles_dir", "dist")
    confirm_method = parse_confirm_method(cfg.get("confirm_method", "web"))
    confirm_timeout_s = cfg.get("confirm_timeout_s", 10.0)
    guarded_labels = cfg.get(
        "guarded_labels",
        ["DANCE", "LOCOMOTION_REQUEST", "WALK_WITH_ME", "BOX", "HIGH_FIVE"],
    )
    debug_cmdrec = cfg.get("debug_cmdrec", False)
    behavior_backend = cfg.get("behavior_backend", "nao")
    guarded_labels_override = cfg.get("guarded_labels_override", None)
    unguarded_labels_override = cfg.get("unguarded_labels_override", None)

    if not isinstance(cmdrec_bundles_dir, str):
        raise ValueError("cmdrec_bundles_dir moet een string zijn.")
    if not isinstance(confirm_timeout_s, (int, float)):
        raise ValueError("confirm_timeout_s moet een float zijn.")
    if not isinstance(guarded_labels, list) or not all(isinstance(v, str) for v in guarded_labels):
        raise ValueError("guarded_labels moet een lijst van strings zijn.")
    if not isinstance(debug_cmdrec, bool):
        raise ValueError("debug_cmdrec moet een boolean zijn.")
    if guarded_labels_override is not None:
        if not isinstance(guarded_labels_override, list) or not all(
            isinstance(v, str) for v in guarded_labels_override
        ):
            raise ValueError("guarded_labels_override moet een lijst van strings zijn.")
    if unguarded_labels_override is not None:
        if not isinstance(unguarded_labels_override, list) or not all(
            isinstance(v, str) for v in unguarded_labels_override
        ):
            raise ValueError("unguarded_labels_override moet een lijst van strings zijn.")
    if not isinstance(behavior_backend, str):
        raise ValueError("behavior_backend moet een string zijn.")
    behavior_backend = behavior_backend.strip().lower()
    if behavior_backend not in ("nao", "print"):
        raise ValueError("behavior_backend moet 'nao' of 'print' zijn.")

    run_cfg = cfg.get("run", {}) or {}
    web_ui_enabled = run_cfg.get("web_ui_enabled", None)
    if confirm_method == "popup":
        if web_ui_enabled is not True:
            raise ValueError(
                "confirm_method 'popup' vereist web UI; zet run.web_ui_enabled=true "
                "of kies een andere confirm_method."
            )

    return {
        "cmdrec": cmdrec,
        "cmdrec_bundles_dir": cmdrec_bundles_dir,
        "confirm_method": confirm_method,
        "confirm_timeout_s": float(confirm_timeout_s),
        "guarded_labels": guarded_labels,
        "debug_cmdrec": debug_cmdrec,
        "behavior_backend": behavior_backend,
        "guarded_labels_override": guarded_labels_override,
        "unguarded_labels_override": unguarded_labels_override,
    }


def _extract_nao_connection(cfg: JsonLike) -> Optional[JsonLike]:
    nao_cfg = cfg.get("nao_connection", None)
    if nao_cfg is None:
        return None
    if not isinstance(nao_cfg, dict):
        raise ValueError("nao_connection moet een object/dict zijn.")

    primary = nao_cfg.get("primary", {}) or {}
    fallback = nao_cfg.get("fallback", {}) or {}
    if not isinstance(primary, dict):
        raise ValueError("nao_connection.primary moet een object/dict zijn.")
    if not isinstance(fallback, dict):
        raise ValueError("nao_connection.fallback moet een object/dict zijn.")

    primary_base_url = primary.get("base_url", "http://127.0.0.1:5001/nao")
    fallback_base_url = fallback.get("base_url", "http://127.0.0.1:5000")
    health_ttl_s = nao_cfg.get("health_ttl_s", 30.0)
    health_checks = nao_cfg.get("health_checks", ["py3_ping", "py3_nao_ping"])
    timeout_s = nao_cfg.get("timeout_s", 3.0)
    log_status = nao_cfg.get("log_status", True)

    if not isinstance(primary_base_url, str):
        raise ValueError("nao_connection.primary.base_url moet een string zijn.")
    if not isinstance(fallback_base_url, str):
        raise ValueError("nao_connection.fallback.base_url moet een string zijn.")
    if not isinstance(health_ttl_s, (int, float)):
        raise ValueError("nao_connection.health_ttl_s moet een getal zijn.")
    if not isinstance(timeout_s, (int, float)):
        raise ValueError("nao_connection.timeout_s moet een getal zijn.")
    if not isinstance(log_status, bool):
        raise ValueError("nao_connection.log_status moet een boolean zijn.")
    if not isinstance(health_checks, list) or not all(isinstance(v, str) for v in health_checks):
        raise ValueError("nao_connection.health_checks moet een lijst van strings zijn.")

    return {
        "primary_base_url": primary_base_url,
        "fallback_base_url": fallback_base_url,
        "health_ttl_s": float(health_ttl_s),
        "health_checks": health_checks,
        "timeout_s": float(timeout_s),
        "log_status": log_status,
    }


def _make_mic(mic_cfg: JsonLike):
    t = _req(mic_cfg, "type").lower()
    base = mic_cfg.get("params", {}) or {}
    ptt = mic_cfg.get("params_ptt")
    cont = mic_cfg.get("params_continuous")
    if isinstance(ptt, dict):
        p = dict(base)
        p.update(ptt)
    elif isinstance(cont, dict):
        p = dict(base)
        p.update(cont)
    else:
        p = base
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
        return VoskSTTBackend(**p)
    if t == "azure":
        return AzureSTTBackend(**p)
    raise ValueError(f"Onbekende stt.type: {t!r}")


def make_stt_backend_from_config(cfg: JsonLike):
    """
    Factory for "just the STT backend" from a full run config.

    This avoids web UI drift: `/api/transcribe` should use the same STT parsing
    and instantiation rules as the normal `build_pipeline_from_config(...)`.
    """
    cfg = _expand_env(cfg)
    input_cfg = cfg.get("input", {}) or {}
    stt_cfg = input_cfg.get("stt", None)
    if not stt_cfg:
        raise ValueError("Config mist input.stt (nodig voor /api/transcribe).")
    if not isinstance(stt_cfg, dict):
        raise ValueError("input.stt moet een object/dict zijn.")
    return _make_stt(stt_cfg)


def _make_input(cfg: JsonLike):
    input_cfg = _req(cfg, "input")
    t = _req(input_cfg, "type").lower()
    p = input_cfg.get("params", {}) or {}

    if t == "console":
        return ConsoleInputBackend(**p)

    if t == "audio":
        mic = _make_mic(_req(input_cfg, "mic"))
        stt = _make_stt(_req(input_cfg, "stt"))
        # Wake-word settings are runtime/web-only; AudioInputBackend does not consume them.
        p_audio = dict(p)
        p_audio.pop("wake_mode", None)
        p_audio.pop("wake_timeout_s", None)
        p_audio.pop("wake_words", None)
        return AudioInputBackend(mic=mic, stt=stt, **p_audio)

    raise ValueError(f"Onbekende input.type: {t!r}")


def _make_llm(cfg: JsonLike):
    llm_cfg = _req(cfg, "llm")
    t = _req(llm_cfg, "type").lower()
    p = llm_cfg.get("params", {}) or {}

    if t == "echo":
        return EchoLLMBackend()

    if t == "none":
        return NoOpLLMBackend()

    if t in ("ollama_local",):
        host = p.get("host", "http://localhost:11434")
        model = p.get("model", "llama3.1:8b")
        api_key = p.get("api_key", None)
        client = OllamaClient(model=model, host=host, api_key=api_key)
        return OllamaLLMBackend(client)

    if t in ("ollama", "ollama_cloud"):
        api_key = p.get("api_key") or os.environ.get("OLLAMA_API_KEY")
        if not api_key:
            raise RuntimeError("OLLAMA_API_KEY ontbreekt (zet env var of llm.params.api_key).")

        host = p.get("host", os.environ.get("OLLAMA_HOST", "https://ollama.com"))
        model = p.get("model", os.environ.get("OLLAMA_MODEL", "gpt-oss:120b"))
        client = OllamaClient(model=model, host=host, api_key=api_key)
        return OllamaLLMBackend(client)

    raise ValueError(f"Onbekende llm.type: {t!r}")


def _make_output(cfg: JsonLike, *, api_router: Optional[NaoApiRouter] = None):
    out_cfg = _req(cfg, "output")
    t = _req(out_cfg, "type").lower()
    p = out_cfg.get("params", {}) or {}

    if t == "console":
        return ConsoleOutputBackend(**p)
    if t == "none":
        return NoOpOutputBackend()
    if t in ("nao_tts", "nao_py2", "nao"):
        return NaoTTSOutputBackend(api_router=api_router, **p)
    if t in ("router", "output_router"):
        return OutputRouterBackend(api_router=api_router, **p)

    raise ValueError(f"Onbekende output.type: {t!r}")


def _default_log_path(config_path: str, log_dir: str) -> str:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    cfg_name = os.path.splitext(os.path.basename(config_path))[0]
    filename = f"run_{ts}_{cfg_name}.jsonl"
    return os.path.join(log_dir, filename)


def build_pipeline_from_config(cfg: JsonLike, *, config_path: str = "<memory>") -> InputLLMOutputPipeline:
    cfg = _expand_env(cfg)

    run_cfg = cfg.get("run", {}) or {}
    status_to_console = bool(run_cfg.get("status_to_console", True))

    # logging defaults: AAN
    log_messages = bool(run_cfg.get("log_messages", True))
    log_dir = run_cfg.get("log_dir", "logs")
    log_messages_path = run_cfg.get("log_messages_path", None)

    if log_messages:
        if not log_messages_path:
            os.makedirs(log_dir, exist_ok=True)
            log_messages_path = _default_log_path(config_path, log_dir)
        else:
            parent = os.path.dirname(log_messages_path)
            if parent:
                os.makedirs(parent, exist_ok=True)

    system_prompt = _extract_system_prompt(cfg, config_path=config_path)
    max_history_turns = _extract_max_history_turns(cfg)
    runtime_context_enabled = _extract_runtime_context_enabled(cfg)
    cmdrec_cfg = _extract_cmdrec_config(cfg)
    nao_conn = _extract_nao_connection(cfg)
    api_router = None
    if nao_conn:
        api_router = NaoApiRouter(
            primary_base_url=nao_conn["primary_base_url"],
            fallback_base_url=nao_conn["fallback_base_url"],
            health_ttl_s=nao_conn["health_ttl_s"],
            health_checks=nao_conn["health_checks"],
            timeout_s=nao_conn["timeout_s"],
            status_to_console=nao_conn["log_status"],
        )

    input_backend = _make_input(cfg)
    llm = _make_llm(cfg)
    output = _make_output(cfg, api_router=api_router)

    llm_cfg = cfg.get("llm", {}) or {}
    llm_params = (llm_cfg.get("params", {}) or {})
    log_meta = {
        "config_path": config_path,
        "llm_type": (llm_cfg.get("type") or ""),
        "llm_host": llm_params.get("host"),
        "llm_model": llm_params.get("model"),
        "has_system_prompt": bool(system_prompt or runtime_context_enabled),
        "max_history_turns": max_history_turns,
    }

    cmdrec_recognizer = None
    behavior_executor = None
    if cmdrec_cfg["cmdrec"] != "none":
        cmdrec_recognizer = CmdRecRecognizer(cmdrec_cfg)
        if api_router is not None and cmdrec_cfg["behavior_backend"] == "nao":
            custom_life_enabled = bool(cfg.get("custom_life_enabled", False))
            raw_custom_settings = cfg.get("custom_life_settings") or {}
            custom_life_settings = raw_custom_settings if isinstance(raw_custom_settings, dict) and raw_custom_settings else None
            behavior_executor = ConsoleAndBehaviorExecutor(
                BehaviorExecutor(
                    api_router=api_router,
                    custom_life_enabled=custom_life_enabled,
                    custom_life_settings=custom_life_settings,
                )
            )
        else:
            behavior_executor = PrintBehaviorExecutor()
        _preload_cmdrec(cmdrec_recognizer)

    runtime_context_static = None
    if runtime_context_enabled:
        runtime_context_static = {
            "available_commands": _format_available_commands(cmdrec_recognizer),
            "dance_catalog": _format_dance_catalog(cmdrec_recognizer),
        }

    return InputLLMOutputPipeline(
        input_backend=input_backend,
        llm=llm,
        output_backend=output,
        status_to_console=status_to_console,
        system_prompt=system_prompt,
        log_messages_path=log_messages_path if log_messages else None,
        log_meta=log_meta,
        max_history_turns=max_history_turns,
        cmdrec_recognizer=cmdrec_recognizer,
        behavior_executor=behavior_executor,
        debug_cmdrec=cmdrec_cfg["debug_cmdrec"],
        runtime_context_enabled=runtime_context_enabled,
        runtime_context_static=runtime_context_static,
    )


def build_pipeline_from_json(path: str) -> InputLLMOutputPipeline:
    cfg = _load_json(path)
    return build_pipeline_from_config(cfg, config_path=path)
