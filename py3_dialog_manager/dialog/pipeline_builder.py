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
        if web_ui_enabled is False:
            raise ValueError(
                "confirm_method 'popup' vereist web UI; zet run.web_ui_enabled=true "
                "of kies een andere confirm_method."
            )
        # TODO: koppel popup-confirm aan web UI en check de daadwerkelijke enablement.

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
        api_router.check_primary(force=True)

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
        "has_system_prompt": bool(system_prompt),
        "max_history_turns": max_history_turns,
    }

    cmdrec_recognizer = None
    behavior_executor = None
    if cmdrec_cfg["cmdrec"] != "none":
        cmdrec_recognizer = CmdRecRecognizer(cmdrec_cfg)
        if api_router is not None:
            behavior_executor = ConsoleAndBehaviorExecutor(BehaviorExecutor(api_router=api_router))
        elif cmdrec_cfg["behavior_backend"] == "print":
            behavior_executor = PrintBehaviorExecutor()
        else:
            behavior_executor = PrintBehaviorExecutor()

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
    )


def build_pipeline_from_json(path: str) -> InputLLMOutputPipeline:
    cfg = _load_json(path)
    return build_pipeline_from_config(cfg, config_path=path)
