"""Minimal intranet web UI for the existing dialog pipeline.

UX:
 - Record (start/stop) in the browser
 - On stop: upload WAV -> server runs STT (Whisper backend from your config)
 - User edits transcript in textbox
 - On send: server runs LLM + output using the edited text

Additions:
 - UI can fetch full chat history (user + assistant)
 - UI can display the master/system prompt
"""

from __future__ import annotations

import argparse
import io
import atexit
import copy
import json
import os
import re
import secrets
import threading
import time
import uuid
import shutil
import wave
import subprocess
import socket
import sys
import math
from pathlib import Path
import signal
from typing import Any, Dict, List, Optional, Tuple

from flask import Flask, jsonify, request, send_from_directory
import requests
import numpy as np

try:
    import sounddevice as sd
except Exception:
    sd = None

try:
    from dialog.pipeline import InputLLMOutputPipeline
    from dialog.pipeline_builder import build_pipeline_from_config, make_stt_backend_from_config
    from dialog.backends.input_fixed_text import FixedTextInputBackend
    from dialog.backends.output_none import NoOpOutputBackend
    from dialog.backends.mic_laptop import LaptopMic
    from dialog.backends.mic_nao_ssh import NaoSshMic
    from dialog.backends.vad_segmenter import RmsVadUtteranceCapturer, int16_to_wav_bytes
    from dialog.interfaces import UtteranceAudio, parse_confirm_method, CommandDecision, RouteDecision
except Exception as e:  # pragma: no cover
    raise RuntimeError(
        "Kan projectmodules niet importeren. Run dit script vanaf de repo root, "
        "of zorg dat PYTHONPATH de repo root bevat. Originele fout: "
        + repr(e)
    )

JsonLike = Dict[str, Any]
History = List[Dict[str, Any]]

DEFAULT_CONFIG_PATH = os.path.join("configs", "default.json")

DEFAULT_VOSK_PARAMS = {
    "model_path": "../models/vosk-model-small-nl-0.22",
    "language": "nl",
    "sample_rate": 16000,
    "max_alternatives": 0,
    "enable_words": False,
    "grammar": None,
    "log_level": -1,
}

DEFAULT_WHISPER_PARAMS = {
    "mode": "AUTO",
    "model_cpu": "small",
    "model_gpu": "medium",
    "compute_type_cpu": "int8",
    "compute_type_gpu": "float16",
    "language": "nl",
    "vad_filter": True,
    "min_silence_duration_ms": 1000,
}

DEFAULT_AZURE_PARAMS = {
    "subscription_key": None,
    "region": None,
    "language": "nl-NL",
    "output_format": "detailed",
    "profanity": None,
    "endpoint_id": None,
    "initial_silence_ms": None,
    "end_silence_ms": None,
}

_MIC_VAD_KEYS = {
    "sample_rate",
    "start_threshold_rms",
    "stop_silence_ms",
    "pre_roll_ms",
    "max_utterance_s",
    "block_ms",
}

_WAKE_MODE_VALUES = {"never", "always", "timeout"}
_DEFAULT_WAKE_WORDS = ["NAO", "Alex"]


def _clean_mic_params(raw: Any) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    out: Dict[str, Any] = {}
    for key in _MIC_VAD_KEYS:
        if key not in raw:
            continue
        value = raw.get(key)
        if value is None:
            continue
        if key == "max_utterance_s":
            try:
                out[key] = float(value)
            except Exception:
                continue
        else:
            try:
                out[key] = int(value)
            except Exception:
                continue
    return out


def _clean_wake_mode(raw: Any) -> str:
    value = str(raw or "").strip().lower()
    if value in _WAKE_MODE_VALUES:
        return value
    return "never"


def _clean_wake_timeout_s(raw: Any, *, default: int = 20) -> int:
    try:
        value = int(raw)
    except Exception:
        return int(default)
    if value < 1:
        return int(default)
    return value


def _clean_wake_words(raw: Any) -> List[str]:
    items: List[str] = []
    if isinstance(raw, list):
        source = raw
    elif isinstance(raw, str):
        source = re.split(r"[\n,;]+", raw)
    else:
        source = []
    for entry in source:
        text = str(entry or "").strip()
        if not text:
            continue
        items.append(text)
    if not items:
        return list(_DEFAULT_WAKE_WORDS)
    deduped: List[str] = []
    seen = set()
    for item in items:
        key = " ".join(item.casefold().split())
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped or list(_DEFAULT_WAKE_WORDS)


def _parse_host_port(raw: Optional[str], default_port: int) -> Tuple[Optional[str], int]:
    if not raw:
        return None, default_port
    value = raw.strip()
    if not value:
        return None, default_port
    if ":" in value:
        host, port_s = value.rsplit(":", 1)
        if port_s.isdigit():
            return host.strip(), int(port_s)
    return value, default_port


def _check_tcp(host: Optional[str], port: int) -> bool:
    if not host:
        return False
    try:
        with socket.create_connection((host, port), timeout=2.5):
            return True
    except OSError:
        return False


def _normalize_url(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    value = raw.strip()
    if not value:
        return None
    if "://" not in value:
        value = "http://" + value
    return value.rstrip("/")


def _extract_runtime_config(cfg_src: JsonLike) -> JsonLike:
    nao_conn = cfg_src.get("nao_connection", {}) or {}
    primary = nao_conn.get("primary", {}) or {}
    fallback = nao_conn.get("fallback", {}) or {}
    primary_url = (primary.get("base_url") or "").rstrip("/")
    if primary_url.endswith("/nao"):
        behavior_manager_url = primary_url[:-4]
    else:
        behavior_manager_url = primary_url

    input_cfg = cfg_src.get("input", {}) or {}
    mic_cfg = (input_cfg.get("mic", {}) or {})
    input_params = (input_cfg.get("params", {}) or {})
    mic_params = (mic_cfg.get("params", {}) or {})
    nao_ip = mic_params.get("host", "") if mic_cfg.get("type") == "nao_ssh" else ""
    if not nao_ip:
        nao_ip = cfg_src.get("nao_ip", "") or ""
    stt_cfg = (input_cfg.get("stt", {}) or {})
    stt_params = (stt_cfg.get("params", {}) or {})
    mic_ptt = _clean_mic_params((mic_cfg.get("params_ptt") or mic_cfg.get("params") or {}))
    mic_cont = _clean_mic_params((mic_cfg.get("params_continuous") or mic_cfg.get("params") or {}))

    llm_cfg = (cfg_src.get("llm", {}) or {})
    llm_params = (llm_cfg.get("params", {}) or {})
    master_prompt_file = llm_params.get("system_prompt_file", None)

    output_cfg = (cfg_src.get("output", {}) or {})
    output_type = (output_cfg.get("type") or "none").lower()
    output_params = output_cfg.get("params", {}) if isinstance(output_cfg.get("params"), dict) else {}
    output_target = output_cfg.get("target")
    if not output_target:
        if output_type == "none":
            output_target = "none"
        else:
            output_target = "nao" if output_type in ("nao_tts", "nao_py2", "nao") else "server"
    tts_engine = output_cfg.get("engine")
    if not tts_engine:
        if output_type == "none":
            tts_engine = "none"
        else:
            tts_engine = "nao_native" if output_type in ("nao_tts", "nao_py2", "nao") else "piper"

    input_device = mic_params.get("input_device", None)
    if mic_cfg.get("type") == "nao_ssh":
        input_device = "nao_ssh"

    return {
        "confirm_policy": (cfg_src.get("confirm_policy") or "when_guarded"),
        "nao_base_url": (fallback.get("base_url") or "").rstrip("/"),
        "behavior_manager_url": behavior_manager_url,
        "nao_ip": nao_ip or "",
        "nao_ip_enabled": bool(cfg_src.get("nao_ip_enabled")) if "nao_ip_enabled" in cfg_src else bool(nao_ip),
        "base_enabled": True,
        "behavior_enabled": True,
        "base_autostart": False,
        "behavior_autostart": False,
        "input_device": input_device,
        "stt_type": (stt_cfg.get("type") or "vosk"),
        "mic_params_ptt": mic_ptt,
        "mic_params_continuous": mic_cont,
        "azure_initial_silence_ms": stt_params.get("initial_silence_ms"),
        "azure_end_silence_ms": stt_params.get("end_silence_ms"),
        "wake_mode": _clean_wake_mode(input_params.get("wake_mode", "never")),
        "wake_timeout_s": _clean_wake_timeout_s(input_params.get("wake_timeout_s", 20), default=20),
        "wake_words": _clean_wake_words(input_params.get("wake_words")),
        "cmdrec": cfg_src.get("cmdrec", "latest"),
        "ptt_use_vad": bool(cfg_src.get("ptt_use_vad", False)),
        "llm_type": llm_cfg.get("type", "echo"),
        "llm_model": llm_params.get("model", ""),
        "master_prompt_file": master_prompt_file,
        "output_nao_tts": output_type in ("nao_tts", "nao_py2", "nao"),
        "output_target": output_target,
        "output_device": output_cfg.get("device", None) if output_cfg.get("device", None) is not None else output_params.get("output_device"),
        "tts_engine": tts_engine,
        "piper_model_path": output_cfg.get("piper_model_path") or output_params.get("piper_model_path"),
        "azure_tts_voice": output_cfg.get("azure_tts_voice") or output_params.get("azure_tts_voice"),
        "azure_tts_rate": output_cfg.get("azure_tts_rate") or output_params.get("azure_tts_rate"),
        "azure_tts_pitch": output_cfg.get("azure_tts_pitch") or output_params.get("azure_tts_pitch"),
        "azure_tts_volume_db": output_cfg.get("azure_tts_volume_db") or output_params.get("azure_tts_volume_db"),
        "custom_life_enabled": bool(cfg_src.get("custom_life_enabled", False)),
        "custom_life_settings": cfg_src.get("custom_life_settings", {}),
        "nao_auto_rest_after_s": cfg_src.get("nao_auto_rest_after_s", 0),
    }


def _stt_params_for_type(stt_type: str, cfg_src: JsonLike) -> JsonLike:
    stt_cfg = (cfg_src.get("input", {}) or {}).get("stt", {}) or {}
    if (stt_cfg.get("type") or "").lower() == stt_type.lower():
        return copy.deepcopy(stt_cfg.get("params", {}) or {})
    if stt_type.lower() == "vosk":
        return copy.deepcopy(DEFAULT_VOSK_PARAMS)
    if stt_type.lower() == "azure":
        return copy.deepcopy(DEFAULT_AZURE_PARAMS)
    return copy.deepcopy(DEFAULT_WHISPER_PARAMS)


def _apply_runtime_overrides(cfg_src: JsonLike, runtime_cfg: JsonLike) -> JsonLike:
    cfg_out = copy.deepcopy(cfg_src)
    input_cfg = cfg_out.setdefault("input", {})
    input_params = input_cfg.setdefault("params", {})
    mic_cfg = input_cfg.setdefault("mic", {})
    mic_params = mic_cfg.setdefault("params", {})
    stt_cfg = input_cfg.setdefault("stt", {})

    nao_base_url = _normalize_url(runtime_cfg.get("nao_base_url"))
    behavior_url = _normalize_url(runtime_cfg.get("behavior_manager_url"))
    base_enabled = bool(runtime_cfg.get("base_enabled", True))
    behavior_enabled = bool(runtime_cfg.get("behavior_enabled", True))
    nao_ip_value = (runtime_cfg.get("nao_ip") or "").strip()
    nao_ip_enabled = bool(runtime_cfg.get("nao_ip_enabled", False))

    if "nao_connection" in cfg_out:
        nao_conn = cfg_out["nao_connection"] or {}
        primary = nao_conn.get("primary", {}) or {}
        fallback = nao_conn.get("fallback", {}) or {}
        if behavior_enabled and behavior_url:
            primary["base_url"] = behavior_url + "/nao"
        elif nao_base_url:
            primary["base_url"] = nao_base_url
            nao_conn["health_checks"] = ["py3_ping"]
        if nao_base_url and base_enabled:
            fallback["base_url"] = nao_base_url
        elif "base_url" in primary:
            fallback["base_url"] = primary["base_url"]
        nao_conn["primary"] = primary
        nao_conn["fallback"] = fallback
        cfg_out["nao_connection"] = nao_conn

    input_device = runtime_cfg.get("input_device", None)
    if input_device == "nao_ssh" and nao_ip_value:
        mic_cfg["type"] = "nao_ssh"
        mic_params.pop("input_device", None)
        if nao_ip_enabled:
            mic_params["host"] = nao_ip_value
    else:
        mic_cfg["type"] = "laptop"
        if "input_device" in runtime_cfg:
            mic_params["input_device"] = input_device

    stt_type = (runtime_cfg.get("stt_type") or stt_cfg.get("type") or "vosk").lower()
    stt_cfg["type"] = stt_type
    stt_cfg["params"] = _stt_params_for_type(stt_type, cfg_src)

    mic_ptt = _clean_mic_params(runtime_cfg.get("mic_params_ptt"))
    mic_cont = _clean_mic_params(runtime_cfg.get("mic_params_continuous"))
    if mic_ptt:
        mic_cfg["params_ptt"] = mic_ptt
    if mic_cont:
        mic_cfg["params_continuous"] = mic_cont

    if stt_type == "azure":
        az_initial = runtime_cfg.get("azure_initial_silence_ms", None)
        az_end = runtime_cfg.get("azure_end_silence_ms", None)
        if az_initial is not None:
            try:
                stt_cfg["params"]["initial_silence_ms"] = int(az_initial)
            except Exception:
                pass
        if az_end is not None:
            try:
                stt_cfg["params"]["end_silence_ms"] = int(az_end)
            except Exception:
                pass

    input_params["wake_mode"] = _clean_wake_mode(runtime_cfg.get("wake_mode", input_params.get("wake_mode", "never")))
    input_params["wake_timeout_s"] = _clean_wake_timeout_s(
        runtime_cfg.get("wake_timeout_s", input_params.get("wake_timeout_s", 20)),
        default=_clean_wake_timeout_s(input_params.get("wake_timeout_s", 20), default=20),
    )
    input_params["wake_words"] = _clean_wake_words(runtime_cfg.get("wake_words", input_params.get("wake_words")))

    cmdrec_value = runtime_cfg.get("cmdrec", None)
    if cmdrec_value:
        cfg_out["cmdrec"] = cmdrec_value

    llm_type = (runtime_cfg.get("llm_type") or cfg_out.get("llm", {}).get("type") or "echo").lower()
    llm_cfg = cfg_out.get("llm", {}) or {}
    llm_params = llm_cfg.get("params", {}) or {}
    if llm_type == "echo":
        cfg_out["llm"] = {"type": "echo"}
    else:
        llm_cfg["type"] = llm_type
        model_value = runtime_cfg.get("llm_model")
        if model_value:
            llm_params["model"] = model_value
        mp_file = runtime_cfg.get("master_prompt_file", None)
        if mp_file:
            llm_params["system_prompt_file"] = mp_file
        llm_cfg["params"] = llm_params
        cfg_out["llm"] = llm_cfg

    if base_enabled:
        cfg_out["behavior_backend"] = "nao"
    else:
        cfg_out["output"] = {"type": "none", "params": {}}
        cfg_out["behavior_backend"] = "print"

    if "custom_life_enabled" in runtime_cfg:
        cfg_out["custom_life_enabled"] = bool(runtime_cfg.get("custom_life_enabled"))
    if "custom_life_settings" in runtime_cfg:
        cfg_out["custom_life_settings"] = runtime_cfg.get("custom_life_settings") or {}

    output_target = (runtime_cfg.get("output_target") or "").lower()
    tts_engine = (runtime_cfg.get("tts_engine") or "").lower()
    output_device = runtime_cfg.get("output_device", None)
    piper_model_path = runtime_cfg.get("piper_model_path")
    piper_length_scale = runtime_cfg.get("piper_length_scale")
    piper_noise_scale = runtime_cfg.get("piper_noise_scale")
    piper_noise_w_scale = runtime_cfg.get("piper_noise_w_scale")
    piper_sentence_silence = runtime_cfg.get("piper_sentence_silence")
    piper_volume = runtime_cfg.get("piper_volume")
    azure_tts_voice = runtime_cfg.get("azure_tts_voice")
    azure_tts_rate = runtime_cfg.get("azure_tts_rate")
    azure_tts_pitch = runtime_cfg.get("azure_tts_pitch")
    azure_tts_volume_db = runtime_cfg.get("azure_tts_volume_db")
    base_output_params = (cfg_out.get("output") or {}).get("params", {}) or {}
    output_timeout = base_output_params.get("timeout")
    if output_target or tts_engine or output_device is not None:
        if output_target == "none" or tts_engine == "none":
            cfg_out["output"] = {"type": "none", "params": {}}
        else:
            cfg_out["output"] = {
                "type": "router",
                "params": {
                    "target": output_target or "nao",
                    "tts_engine": tts_engine or "nao_native",
                    "output_device": output_device,
                    "piper_model_path": piper_model_path,
                    "piper_length_scale": piper_length_scale,
                    "piper_noise_scale": piper_noise_scale,
                    "piper_noise_w_scale": piper_noise_w_scale,
                    "piper_sentence_silence": piper_sentence_silence,
                    "piper_volume": piper_volume,
                    "azure_tts_voice": azure_tts_voice,
                    "azure_tts_rate": azure_tts_rate,
                    "azure_tts_pitch": azure_tts_pitch,
                    "azure_tts_volume_db": azure_tts_volume_db,
                    "timeout": output_timeout,
                },
            }

    return cfg_out


def _make_mic_from_config(cfg: JsonLike, *, mode: str = "ptt"):
    input_cfg = cfg.get("input", {}) or {}
    mic_cfg = input_cfg.get("mic", {}) or {}
    mic_type = (mic_cfg.get("type") or "").lower()
    base_params = (mic_cfg.get("params") or {})
    if mode == "continuous":
        mode_params = mic_cfg.get("params_continuous") or {}
    else:
        mode_params = mic_cfg.get("params_ptt") or {}
    if not isinstance(base_params, dict):
        base_params = {}
    if not isinstance(mode_params, dict):
        mode_params = {}
    mic_params = dict(base_params)
    mic_params.update(mode_params)
    if mic_type == "laptop":
        return LaptopMic(**mic_params)
    if mic_type == "nao_ssh":
        return NaoSshMic(**mic_params)
    raise ValueError(f"Onbekende mic.type: {mic_type!r}")


def _resolve_start_timeout(params: Dict[str, Any], *, default: float) -> float:
    value = params.get("start_timeout_s", None)
    if value is None:
        return float(default)
    try:
        value_f = float(value)
    except Exception:
        return float(default)
    if value_f <= 0:
        return float(default)
    return value_f


def _load_json(path: str) -> JsonLike:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _wav_bytes_to_int16_mono(wav_bytes: bytes) -> Tuple[np.ndarray, int]:
    with io.BytesIO(wav_bytes) as bio:
        with wave.open(bio, "rb") as wf:
            sample_rate = wf.getframerate()
            num_channels = wf.getnchannels()
            sample_width = wf.getsampwidth()
            frames = wf.readframes(wf.getnframes())

    if sample_width != 2:
        raise ValueError("Verwacht 16-bit audio (sample_width=2)")

    audio = np.frombuffer(frames, dtype=np.int16)
    if num_channels > 1:
        audio = audio.reshape(-1, num_channels).mean(axis=1).astype(np.int16)

    return audio, sample_rate


def _strip_system(history: History) -> History:
    if history and history[0].get("role") == "system":
        return history[1:]
    return history


def _append_turn(history: History, user_text: str, assistant_text: str) -> History:
    history = _strip_system(list(history))
    ts = time.time()
    history.append({"role": "user", "content": user_text, "ts": ts})
    history.append({"role": "assistant", "content": assistant_text, "ts": ts})
    return history


def create_app(*, cfg: JsonLike, config_path: str) -> Tuple[Flask, Any, InputLLMOutputPipeline]:
    app = Flask(__name__, static_folder="web", static_url_path="")

    base_cfg = copy.deepcopy(cfg)
    run_cfg = base_cfg.setdefault("run", {})
    run_cfg["web_ui_enabled"] = True
    base_config_path = config_path
    base_pipeline = build_pipeline_from_config(cfg, config_path=config_path)
    base_stt = make_stt_backend_from_config(cfg)
    pipeline_lock = threading.Lock()
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    al_dir = os.path.join(repo_root, "py3_command_recognition_train", "data", "al")
    al_lock = threading.RLock()
    retrain_lock = threading.Lock()
    retrain_state: Dict[str, Any] = {
        "running": False,
        "started_at": None,
        "finished_at": None,
        "exit_code": None,
        "last_error": None,
        "summary": None,
        "log": [],
    }
    proc_lock = threading.Lock()
    proc_logs: Dict[str, List[str]] = {"base": [], "behavior": []}
    proc_state: Dict[str, Dict[str, Any]] = {
        "base": {"proc": None, "exit_code": None},
        "behavior": {"proc": None, "exit_code": None},
    }
    max_log_lines = 400

    # In-memory session histories for intranet testing
    sessions: Dict[str, History] = {}
    pending_confirms: Dict[str, Dict[str, Any]] = {}
    cmdrec_state: Dict[str, Dict[str, Any]] = {}
    runtime_context_state: Dict[str, Dict[str, Any]] = {}
    runtime_cfg_by_sid: Dict[str, JsonLike] = {}
    pipeline_by_sid: Dict[str, InputLLMOutputPipeline] = {}
    stt_by_sid: Dict[str, Any] = {}
    continuous_state: Dict[str, Dict[str, Any]] = {}
    ptt_state: Dict[str, Dict[str, Any]] = {}
    last_activity = {"ts": time.time()}
    activity_lock = threading.Lock()

    confirm_method = parse_confirm_method(cfg.get("confirm_method", "web"))
    confirm_timeout_s = float(cfg.get("confirm_timeout_s", 10.0))

    def _get_sid() -> str:
        sid = request.cookies.get("sid")
        if not sid:
            sid = secrets.token_urlsafe(16)
        return sid

    def _get_history(sid: str) -> History:
        return sessions.setdefault(sid, [])

    def _get_cmdrec_state(sid: str) -> Dict[str, Any]:
        return cmdrec_state.setdefault(
            sid,
            {"mode": "DIALOG", "active_behavior": None, "pending_dance": None},
        )

    def _get_runtime_state(sid: str) -> Dict[str, Any]:
        return runtime_context_state.setdefault(sid, {"last_action": None})

    def _get_continuous_state(sid: str) -> Dict[str, Any]:
        return continuous_state.setdefault(
            sid,
            {
                "running": False,
                "stop": None,
                "thread": None,
                "last_error": None,
                "phase": "idle",
                "wake_open_until": 0.0,
                "wake_open_once": False,
                "wake_stt": None,
                "wake_stt_key": None,
                "last_wake_text": "",
                "eye_phase": None,
                "eye_last_try": 0.0,
            },
        )

    def _get_ptt_state(sid: str) -> Dict[str, Any]:
        return ptt_state.setdefault(
            sid,
            {"running": False, "stop": None, "thread": None, "last_error": None, "audio": None, "vad_cfg": None},
        )

    def _set_last_action(sid: str, summary: Optional[str]) -> None:
        _get_runtime_state(sid)["last_action"] = summary

    # runtime pipeline helpers
    def _get_runtime_cfg(sid: str) -> JsonLike:
        return runtime_cfg_by_sid.setdefault(sid, _extract_runtime_config(base_cfg))

    def _auto_rest_timeout_s(runtime_cfg: JsonLike) -> float:
        value = runtime_cfg.get("nao_auto_rest_after_s", base_cfg.get("nao_auto_rest_after_s", 0))
        try:
            return float(value or 0)
        except Exception:
            return 0.0

    def _touch_activity() -> None:
        with activity_lock:
            last_activity["ts"] = time.time()

    def _get_pipeline(sid: str) -> InputLLMOutputPipeline:
        return pipeline_by_sid.get(sid, base_pipeline)

    def _get_stt(sid: str):
        return stt_by_sid.get(sid, base_stt)

    def _rebuild_pipeline_for_sid(sid: str, cfg_src: JsonLike) -> None:
        with pipeline_lock:
            pipeline_by_sid[sid] = build_pipeline_from_config(cfg_src, config_path=base_config_path)
            stt_by_sid[sid] = make_stt_backend_from_config(cfg_src)

    def _pipeline_props(pipeline: InputLLMOutputPipeline) -> Tuple[bool, Any, Any]:
        dbg = bool(getattr(pipeline, "_debug_cmdrec", False))
        cmdrec = getattr(pipeline, "_cmdrec", None)
        behavior_executor = getattr(pipeline, "_behavior_executor", None)
        return dbg, cmdrec, behavior_executor

    def _format_action_summary(prefix: str, label: str, resolved: Optional[Dict[str, Any]]) -> str:
        if resolved:
            items = ", ".join([f"{k}={v}" for k, v in sorted(resolved.items())])
            return f"{prefix}: {label}; resolved: {items}"
        return f"{prefix}: {label}"

    def _normalize_text(value: str) -> str:
        return " ".join((value or "").casefold().split())

    def _wake_mode(runtime_cfg: JsonLike) -> str:
        return _clean_wake_mode(runtime_cfg.get("wake_mode", "never"))

    def _wake_timeout_s(runtime_cfg: JsonLike) -> int:
        return _clean_wake_timeout_s(runtime_cfg.get("wake_timeout_s", 20), default=20)

    def _wake_words(runtime_cfg: JsonLike) -> List[str]:
        return _clean_wake_words(runtime_cfg.get("wake_words"))

    def _text_has_wake_word(text: str, wake_words: List[str]) -> bool:
        norm = _normalize_text(text)
        if not norm:
            return False
        for wake in wake_words:
            wake_norm = _normalize_text(wake)
            if not wake_norm:
                continue
            if re.search(r"(^|\s)" + re.escape(wake_norm) + r"(?=\s|$)", norm):
                return True
        return False

    def _build_wake_stt(cfg_src: JsonLike, wake_words: List[str]):
        wake_cfg = copy.deepcopy(cfg_src)
        input_cfg = wake_cfg.setdefault("input", {})
        stt_cfg = input_cfg.setdefault("stt", {})
        stt_cfg["type"] = "vosk"
        params = _stt_params_for_type("vosk", cfg_src)
        grammar_words: List[str] = []
        for raw in wake_words:
            w = str(raw or "").strip()
            if not w:
                continue
            grammar_words.append(w)
            lower = w.casefold()
            if lower != w:
                grammar_words.append(lower)
        # Deduplicate while preserving order.
        seen = set()
        grammar_unique: List[str] = []
        for w in grammar_words:
            if w in seen:
                continue
            seen.add(w)
            grammar_unique.append(w)
        params["grammar"] = grammar_unique + ["[unk]"]
        stt_cfg["params"] = params
        return make_stt_backend_from_config(wake_cfg)

    def _dance_catalog(cmdrec_obj) -> List[Dict[str, Any]]:
        if cmdrec_obj is None:
            return []
        try:
            return cmdrec_obj.get_dance_catalog() or []
        except Exception:
            return []

    def _dance_followup_policy(cmdrec_obj) -> Dict[str, Any]:
        if cmdrec_obj is not None:
            try:
                bundle_path = getattr(cmdrec_obj, "bundle_path", None)
                if bundle_path:
                    policy_path = Path(bundle_path) / "dance_followup_policy.json"
                    if policy_path.exists():
                        return json.loads(policy_path.read_text(encoding="utf-8"))
            except Exception:
                pass
        # Fallback to data/ for local dev
        dm_root = Path(__file__).resolve().parents[1]
        repo_root = dm_root.parent
        fallback = repo_root / "py3_command_recognition_train" / "data" / "dance_followup_policy.json"
        if fallback.exists():
            return json.loads(fallback.read_text(encoding="utf-8"))
        return {}

    def _policy_list(policy: Dict[str, Any], key: str) -> List[str]:
        values = policy.get(key) or []
        return [v for v in values if isinstance(v, str) and v.strip()]

    def _policy_value(policy: Dict[str, Any], key: str, default: Any) -> Any:
        return policy.get(key, default)

    def _pick_dance_followup_prompt(policy: Dict[str, Any], hint: str) -> str:
        prompts = _policy_list(policy, "followup_prompts")
        if prompts:
            choice = secrets.choice(prompts)
            if "{hint}" in choice:
                rendered = choice.replace("{hint}", hint or "")
                if not hint:
                    rendered = re.sub(r"\(\s*\)", "", rendered)
                return re.sub(r"\s+", " ", rendered).strip()
            return choice.strip()
        if hint:
            return "Natuurlijk. Welke dans wil je? (bijv. {hint})".format(hint=hint)
        return "Natuurlijk. Welke dans wil je?"

    def _emit_followup_text(
        *,
        pipeline: InputLLMOutputPipeline,
        emit_used: str,
        runtime_cfg: JsonLike,
        text: str,
    ) -> None:
        if not text or emit_used != "pipeline":
            return
        if not _output_enabled(runtime_cfg):
            return
        try:
            pipeline.output.emit(text)
            _touch_activity()
        except Exception:
            return

    def _dance_keys(catalog: List[Dict[str, Any]]) -> List[str]:
        keys = []
        for item in catalog:
            key = item.get("key")
            if isinstance(key, str) and key.strip():
                keys.append(key.strip())
        return keys

    def _resolve_dance_from_text(text: str, catalog: List[Dict[str, Any]]) -> Tuple[Optional[str], Optional[str]]:
        cleaned = _normalize_text(text)
        for item in catalog:
            key = (item.get("key") or "").strip()
            if not key:
                continue
            aliases = item.get("aliases") or []
            if key.casefold() in cleaned:
                return key, item.get("behavior")
            for alias in aliases:
                if isinstance(alias, str) and alias.casefold() in cleaned:
                    return key, item.get("behavior")
        return None, None

    def _dance_is_cancel(text: str, policy: Dict[str, Any]) -> bool:
        cleaned = _normalize_text(text)
        return any(phrase.casefold() in cleaned for phrase in _policy_list(policy, "cancel_phrases"))

    def _dance_is_default(text: str, policy: Dict[str, Any]) -> bool:
        cleaned = _normalize_text(text)
        return any(phrase.casefold() in cleaned for phrase in _policy_list(policy, "default_phrases"))

    def _dance_is_list_request(text: str, policy: Dict[str, Any]) -> bool:
        cleaned = _normalize_text(text)
        return any(phrase.casefold() in cleaned for phrase in _policy_list(policy, "list_phrases"))

    def _dance_is_pass_through(text: str, policy: Dict[str, Any]) -> bool:
        cleaned = _normalize_text(text)
        return any(phrase.casefold() in cleaned for phrase in _policy_list(policy, "pass_through_phrases"))

    def _default_dance_resolution(cmdrec_obj) -> Dict[str, str]:
        policy = _dance_followup_policy(cmdrec_obj)
        dance_key = _policy_value(policy, "default_dance_key", "happy")
        catalog = _dance_catalog(cmdrec_obj)
        dance_behavior = None
        for item in catalog:
            if (item.get("key") or "").strip() == dance_key:
                dance_behavior = item.get("behavior")
                break
        if not dance_behavior and isinstance(dance_key, str) and dance_key.strip():
            dance_behavior = "dances/" + dance_key.strip()
        return {"dance_key": dance_key, "dance_behavior": dance_behavior}  # type: ignore[return-value]

    def _command_behavior_preview(label: str, cmdrec_obj, behavior_executor) -> Optional[str]:
        if not label:
            return None
        if label.upper() == "DANCE":
            resolved = _default_dance_resolution(cmdrec_obj)
            behavior = resolved.get("dance_behavior")
            return behavior if isinstance(behavior, str) and behavior.strip() else None
        if behavior_executor is not None and hasattr(behavior_executor, "_behavior_for_command"):
            try:
                cmd = CommandDecision(label=label, confidence=1.0, raw_text=label, resolved=None)
                if label.upper() == "DANCE":
                    cmd.resolved = _default_dance_resolution(cmdrec_obj)
                return behavior_executor._behavior_for_command(cmd)  # type: ignore[attr-defined]
            except Exception:
                return None
        return None

    def _system_prompt_for_sid(sid: str) -> str:
        pipeline = _get_pipeline(sid)
        last_action = _get_runtime_state(sid).get("last_action")
        if hasattr(pipeline, "get_system_prompt"):
            sp = pipeline.get_system_prompt(last_action=last_action)
            return (sp or "").strip()
        sp = getattr(pipeline, "system_prompt", None)
        return (sp or "").strip()

    def _append_assistant(history: History, text: str) -> None:
        history.append({"role": "assistant", "content": text, "ts": time.time()})

    def _append_user(history: History, text: str) -> None:
        history.append({"role": "user", "content": text, "ts": time.time()})

    def _append_confirm_card(history: History, payload: Dict[str, Any]) -> None:
        history.append(payload)

    def _handle_command_decision(
        *,
        sid: str,
        decision: RouteDecision,
        text: str,
        history: History,
        emit_used: str,
        debug: Optional[Dict[str, Any]],
        stt_used: Optional[bool],
        stt_edited: Optional[bool],
        cmdrec_obj,
        behavior_executor,
        runtime_cfg: JsonLike,
    ) -> Optional[Dict[str, Any]]:
        if not decision.is_command or not decision.command:
            return None

        state = _get_cmdrec_state(sid)
        cmd = decision.command

        if cmd.label == "STOP":
            _append_user(history, text)
            pending_confirms.pop(sid, None)
            if behavior_executor:
                behavior_executor.execute(cmd)
            _touch_activity()
            state["mode"] = "DIALOG"
            state["active_behavior"] = None
            _set_last_action(sid, _format_action_summary("Uitgevoerd", cmd.label, cmd.resolved))
            _append_assistant(history, "OK. Uitgevoerd: STOP")
            _al_append(
                "review_queue.jsonl",
                _al_event(
                    sid,
                    text,
                    suggested_label=cmd.label,
                    predicted_label=cmd.label,
                    confidence=cmd.confidence,
                    reason="auto_executed",
                    top3=decision.top3,
                    cmdrec_bundle=str(getattr(cmdrec_obj, "bundle_path", "") or ""),
                    stt_used=stt_used,
                    stt_edited=stt_edited,
                ),
            )
            payload = {
                "ok": True,
                "reply": "OK. Uitgevoerd: STOP",
                "history": history,
                "system_prompt": _system_prompt_for_sid(sid),
                "emit_used": emit_used,
            }
            if debug:
                payload["debug"] = debug
            return payload

        confirm_policy = (runtime_cfg.get("confirm_policy") or base_cfg.get("confirm_policy") or "when_guarded").lower()
        confirm_needed = False
        if confirm_method != "none":
            if confirm_policy == "always":
                confirm_needed = True
            else:
                confirm_needed = bool(cmdrec_obj and cmdrec_obj.is_guarded(cmd.label))

        if confirm_needed:
            _append_user(history, text)
            confirmation_id = uuid.uuid4().hex
            pending_confirms[sid] = {
                "confirmation_id": confirmation_id,
                "command": cmd,
                "created_at": time.time(),
                "timeout_s": confirm_timeout_s,
                "input_text": text,
                "input_meta": {"stt_used": stt_used, "stt_edited": stt_edited},
                "top3": decision.top3,
                "confidence": cmd.confidence,
                "label": cmd.label,
            }
            confirm_payload = {
                "type": "confirm_required",
                "role": "assistant",
                "confirmation_id": confirmation_id,
                "created_at": pending_confirms[sid]["created_at"],
                "timeout_s": pending_confirms[sid]["timeout_s"],
                "prompt": f"Bevestig uitvoeren: {cmd.label} ?",
                "command": {
                    "label": cmd.label,
                    "confidence": cmd.confidence,
                    "raw_text": cmd.raw_text,
                    "resolved": cmd.resolved,
                },
                "top3": decision.top3,
            }
            _append_confirm_card(history, confirm_payload)
            payload = {
                "ok": True,
                "reply": "",
                "history": history,
                "system_prompt": _system_prompt_for_sid(sid),
                "emit_used": emit_used,
            }
            if debug:
                payload["debug"] = debug
            return payload

        _append_user(history, text)
        if behavior_executor:
            behavior_executor.execute(cmd)
        _touch_activity()
        state["mode"] = "PERFORMING"
        state["active_behavior"] = cmd.label
        _set_last_action(sid, _format_action_summary("Uitgevoerd", cmd.label, cmd.resolved))
        _append_assistant(history, f"OK. Uitgevoerd: {cmd.label}")
        _al_append(
            "review_queue.jsonl",
            _al_event(
                sid,
                text,
                suggested_label=cmd.label,
                predicted_label=cmd.label,
                confidence=cmd.confidence,
                reason="auto_executed",
                top3=decision.top3,
                cmdrec_bundle=str(getattr(cmdrec_obj, "bundle_path", "") or ""),
                stt_used=stt_used,
                stt_edited=stt_edited,
            ),
        )
        payload = {
            "ok": True,
            "reply": f"OK. Uitgevoerd: {cmd.label}",
            "history": history,
            "system_prompt": _system_prompt_for_sid(sid),
            "emit_used": emit_used,
        }
        if debug:
            payload["debug"] = debug
        return payload

    def _append_proc_log(name: str, line: str) -> None:
        line = line.rstrip("\r\n")
        if not line:
            return
        ts = time.strftime("%H:%M:%S", time.localtime())
        entry = f"[{ts}] {line}"
        with proc_lock:
            buf = proc_logs.setdefault(name, [])
            buf.append(entry)
            if len(buf) > max_log_lines:
                del buf[: len(buf) - max_log_lines]

    def _stream_reader(name: str, stream, prefix: str) -> None:
        try:
            for raw in iter(stream.readline, ""):
                _append_proc_log(name, f"{prefix}{raw.rstrip()}")
        finally:
            try:
                stream.close()
            except Exception:
                pass

    def _proc_status(name: str) -> Dict[str, Any]:
        with proc_lock:
            entry = proc_state.get(name, {})
            proc = entry.get("proc")
            exit_code = entry.get("exit_code")
        running = bool(proc and proc.poll() is None)
        if proc and proc.poll() is not None:
            exit_code = proc.poll()
            with proc_lock:
                proc_state[name]["exit_code"] = exit_code
        return {"running": running, "pid": proc.pid if proc else None, "exit_code": exit_code}

    def _start_process(name: str, cmd: List[str], cwd: str) -> Dict[str, Any]:
        with proc_lock:
            entry = proc_state.get(name)
            proc = entry.get("proc") if entry else None
        if proc and proc.poll() is None:
            return {"ok": False, "error": f"{name} draait al."}
        try:
            env = os.environ.copy()
            env.pop("WERKZEUG_SERVER_FD", None)
            env.pop("WERKZEUG_RUN_MAIN", None)
            env.pop("FLASK_RUN_FROM_CLI", None)
            proc = subprocess.Popen(
                cmd,
                cwd=cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                env=env,
            )
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

        with proc_lock:
            proc_state[name] = {"proc": proc, "exit_code": None}
            proc_logs.setdefault(name, []).append("--- started ---")

        if proc.stdout:
            threading.Thread(target=_stream_reader, args=(name, proc.stdout, ""), daemon=True).start()
        if proc.stderr:
            threading.Thread(target=_stream_reader, args=(name, proc.stderr, "[err] "), daemon=True).start()

        return {"ok": True, "pid": proc.pid}

    def _stop_process(name: str) -> Dict[str, Any]:
        with proc_lock:
            entry = proc_state.get(name)
            proc = entry.get("proc") if entry else None
        if not proc or proc.poll() is not None:
            return {"ok": True, "stopped": True}
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        with proc_lock:
            proc_state[name]["exit_code"] = proc.poll()
            proc_logs.setdefault(name, []).append("--- stopped ---")
        return {"ok": True, "stopped": True}

    def _pick_shutdown_runtime_cfg() -> JsonLike:
        if runtime_cfg_by_sid:
            return next(iter(runtime_cfg_by_sid.values()))
        return _extract_runtime_config(base_cfg)

    def _try_nao_rest(runtime_cfg: JsonLike, *, reason: str) -> None:
        base_url = _normalize_url(runtime_cfg.get("nao_base_url"))
        behavior_url = _normalize_url(runtime_cfg.get("behavior_manager_url"))
        base_enabled = bool(runtime_cfg.get("base_enabled", True))
        behavior_enabled = bool(runtime_cfg.get("behavior_enabled", True))

        if behavior_enabled and behavior_url and _check_ping(behavior_url, "/ping"):
            try:
                requests.post(behavior_url + "/nao/rest", json={}, timeout=2.0)
                return
            except Exception:
                pass

        if base_enabled and base_url and _check_ping(base_url, "/ping"):
            try:
                requests.post(base_url + "/rest", json={}, timeout=2.0)
            except Exception:
                pass

    def _stop_all_processes() -> None:
        _try_nao_rest(_pick_shutdown_runtime_cfg(), reason="shutdown")
        for name in ("behavior", "base"):
            _stop_process(name)

    atexit.register(_stop_all_processes)

    def _shutdown_rest() -> None:
        _try_nao_rest(_pick_shutdown_runtime_cfg(), reason="signal")

    try:
        setattr(app, "_shutdown_rest", _shutdown_rest)
    except Exception:
        pass

    def _auto_rest_loop() -> None:
        while True:
            time.sleep(2.0)
            runtime_cfg = _pick_shutdown_runtime_cfg()
            timeout_s = _auto_rest_timeout_s(runtime_cfg)
            if timeout_s <= 0:
                continue
            with activity_lock:
                last_ts = last_activity["ts"]
            if time.time() - last_ts < timeout_s:
                continue
            _try_nao_rest(runtime_cfg, reason="idle")
            _touch_activity()

    threading.Thread(target=_auto_rest_loop, daemon=True).start()

    def _base_controller_cmd(nao_ip: str) -> Tuple[List[str], str, Optional[str]]:
        base_dir = os.path.join(repo_root, "py2_nao_base_controller")
        python_path = os.path.join(base_dir, "venv", "Scripts", "python.exe")
        script_path = os.path.join(base_dir, "nao_api.py")
        if not os.path.exists(python_path):
            return [], base_dir, "venv\\Scripts\\python.exe niet gevonden (py2_nao_base_controller)."
        if not os.path.exists(script_path):
            return [], base_dir, "nao_api.py niet gevonden."
        cmd = [python_path, script_path]
        if nao_ip:
            cmd += ["--nao_ip", nao_ip]
        return cmd, base_dir, None

    def _behavior_manager_cmd() -> Tuple[List[str], str, Optional[str]]:
        base_dir = os.path.join(repo_root, "py3_nao_behavior_manager")
        python_path = os.path.join(base_dir, "venv", "Scripts", "python.exe")
        script_path = os.path.join(base_dir, "py3_server.py")
        if not os.path.exists(python_path):
            return [], base_dir, "venv\\Scripts\\python.exe niet gevonden (py3_nao_behavior_manager)."
        if not os.path.exists(script_path):
            return [], base_dir, "py3_server.py niet gevonden."
        cmd = [python_path, script_path]
        return cmd, base_dir, None

    _al_key_cache: Dict[str, Dict[str, Any]] = {}

    def _al_keyset(path: str) -> set[str]:
        try:
            stat = os.stat(path)
        except OSError:
            return set()
        cached = _al_key_cache.get(path)
        if cached and cached.get("mtime") == stat.st_mtime and cached.get("size") == stat.st_size:
            return cached.get("keys", set())
        entries = _load_jsonl(path)
        keys = {
            _normalize_key(entry.get("text", ""))
            for entry in entries
            if _normalize_key(entry.get("text", ""))
        }
        _al_key_cache[path] = {"mtime": stat.st_mtime, "size": stat.st_size, "keys": keys}
        return keys

    def _al_should_skip(filename: str, payload: Dict[str, Any]) -> bool:
        if filename not in ("review_queue.jsonl", "auto_train.jsonl"):
            return False
        key = _normalize_key(payload.get("text", ""))
        if not key:
            return True
        train_base_path = os.path.join(
            repo_root, "py3_command_recognition_train", "data", "train_base.jsonl"
        )
        reviewed_path = os.path.join(al_dir, "reviewed.jsonl")
        auto_train_path = os.path.join(al_dir, "auto_train.jsonl")
        auto_train_queue_path = os.path.join(al_dir, "auto_train_queue.jsonl")
        review_queue_path = os.path.join(al_dir, "review_queue.jsonl")
        paths: List[str] = [train_base_path, reviewed_path, auto_train_path]
        if filename == "review_queue.jsonl":
            paths = [review_queue_path] + paths
        elif filename == "auto_train_queue.jsonl":
            paths = [auto_train_queue_path] + paths
        for path in paths:
            if key in _al_keyset(path):
                return True
        return False

    def _al_append(filename: str, payload: Dict[str, Any]) -> None:
        os.makedirs(al_dir, exist_ok=True)
        path = os.path.join(al_dir, filename)
        line = json.dumps(payload, ensure_ascii=False)
        with al_lock:
            if _al_should_skip(filename, payload):
                return
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(line + "\n")

    def _load_jsonl(path: str) -> List[Dict[str, Any]]:
        if not os.path.exists(path):
            return []
        entries: List[Dict[str, Any]] = []
        try:
            for line in open(path, "r", encoding="utf-8").read().splitlines():
                if not line.strip():
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        except OSError:
            return []
        return entries

    def _write_jsonl(entries: List[Dict[str, Any]], path: str) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            for entry in entries:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def _normalize_key(text: str) -> str:
        return " ".join((text or "").split()).strip().casefold()

    def _al_state_path() -> str:
        return os.path.join(al_dir, "retrain_state.json")

    def _load_retrain_state() -> Dict[str, Any]:
        path = _al_state_path()
        if not os.path.exists(path):
            return {"since_last_retrain": 0, "last_retrain_bundle": None, "last_retrain_at": None}
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return {"since_last_retrain": 0, "last_retrain_bundle": None, "last_retrain_at": None}
            data.setdefault("since_last_retrain", 0)
            data.setdefault("last_retrain_bundle", None)
            data.setdefault("last_retrain_at", None)
            return data
        except Exception:
            return {"since_last_retrain": 0, "last_retrain_bundle": None, "last_retrain_at": None}

    def _save_retrain_state(state: Dict[str, Any]) -> None:
        path = _al_state_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        Path(path).write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

    def _latest_bundle_name() -> Optional[str]:
        bundles_dir = os.path.join(repo_root, "py3_command_recognition_train", "dist")
        if not os.path.isdir(bundles_dir):
            return None
        pattern = re.compile(r"^bundle_v(\d+)(?:_(\d{8}))?$")
        bundles: List[Tuple[int, int, str]] = []
        for name in os.listdir(bundles_dir):
            path = os.path.join(bundles_dir, name)
            if not os.path.isdir(path):
                continue
            match = pattern.match(name)
            if not match:
                continue
            version = int(match.group(1))
            date_part = int(match.group(2)) if match.group(2) else 0
            bundles.append((version, date_part, name))
        if not bundles:
            return None
        bundles.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return bundles[0][2]

    def _latest_bundle_path() -> Optional[str]:
        bundles_dir = os.path.join(repo_root, "py3_command_recognition_train", "dist")
        name = _latest_bundle_name()
        if not name:
            return None
        return os.path.join(bundles_dir, name)

    def _bundle_name_from_path(value: Any) -> str:
        if not value:
            return ""
        raw = str(value)
        if not raw:
            return ""
        return os.path.basename(raw.replace("\\", "/"))

    def _resolve_train_path(path_str: Optional[str]) -> Optional[Path]:
        if not path_str:
            return None
        candidate = Path(path_str)
        if not candidate.is_absolute():
            candidate = Path(repo_root) / "py3_command_recognition_train" / candidate
        return candidate

    def _load_train_report(path_str: Optional[str]) -> Optional[Dict[str, Any]]:
        report_dir = _resolve_train_path(path_str)
        if not report_dir:
            return None
        report_path = report_dir / "train_report.json"
        if not report_path.exists():
            return None
        try:
            return json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def _constraints_ok(report: Dict[str, Any]) -> bool:
        constraints = report.get("constraints", {}) if isinstance(report, dict) else {}
        metrics = report.get("system_metrics", {}) if isinstance(report, dict) else {}
        stop_req = float(constraints.get("system_stop_recall", 0.0))
        none_req = float(constraints.get("fp_none_rate", 1.0))
        return (
            float(metrics.get("system_stop_recall", 0.0)) >= stop_req
            and float(metrics.get("fp_none_rate", 1.0)) <= none_req
        )

    def _constraints_reason(report: Dict[str, Any]) -> str:
        constraints = report.get("constraints", {}) if isinstance(report, dict) else {}
        metrics = report.get("system_metrics", {}) if isinstance(report, dict) else {}
        stop_req = float(constraints.get("system_stop_recall", 0.0))
        none_req = float(constraints.get("fp_none_rate", 1.0))
        stop_val = float(metrics.get("system_stop_recall", 0.0))
        none_val = float(metrics.get("fp_none_rate", 1.0))
        parts: List[str] = []
        if stop_val < stop_req:
            parts.append(f"system_stop_recall {stop_val:.3f} < {stop_req:.3f}")
        if none_val > none_req:
            parts.append(f"fp_none_rate {none_val:.3f} > {none_req:.3f}")
        return "; ".join(parts)

    def _append_retrain_log(line: str) -> None:
        with retrain_lock:
            log = retrain_state.get("log") or []
            log.append(line)
            if len(log) > 400:
                log = log[-400:]
            retrain_state["log"] = log

    def _summarize_retrain(
        *,
        out_path: Optional[str],
        promoted_to: Optional[str],
        baseline_path: Optional[str],
        log_reason: Optional[str],
    ) -> Dict[str, Any]:
        summary: Dict[str, Any] = {
            "promoted": bool(promoted_to),
            "promoted_to": promoted_to,
            "bundle_written": out_path,
            "baseline_bundle": _bundle_name_from_path(baseline_path),
        }
        new_report = _load_train_report(promoted_to or out_path)
        old_report = _load_train_report(baseline_path)
        if new_report:
            summary["new_score"] = float(new_report.get("best", {}).get("score", 0.0))
            summary["constraints_ok"] = _constraints_ok(new_report)
            summary["new_vectorizer"] = new_report.get("best", {}).get("vectorizer")
            summary["new_threshold"] = new_report.get("best", {}).get("threshold")
            summary["new_margin"] = new_report.get("best", {}).get("margin")
            summary["score_formula"] = new_report.get("score_formula")
            summary["score_components"] = {
                "macro_f1_others": new_report.get("system_metrics", {}).get("macro_f1_others"),
                "recall_box": new_report.get("system_metrics", {}).get("recall_box"),
                "recall_dance": new_report.get("system_metrics", {}).get("recall_dance"),
                "fp_none_rate": new_report.get("system_metrics", {}).get("fp_none_rate"),
                "system_stop_recall": new_report.get("system_metrics", {}).get("system_stop_recall"),
            }
            summary["constraints"] = {
                "system_stop_recall": new_report.get("constraints", {}).get("system_stop_recall"),
                "fp_none_rate": new_report.get("constraints", {}).get("fp_none_rate"),
            }
            summary["search_space"] = new_report.get("search_space", {})
            data_info = new_report.get("data", {})
            summary["train_total"] = data_info.get("total")
            summary["none_ratio"] = data_info.get("none_ratio")
            summary["train_label_counts"] = data_info.get("train_label_counts")
            summary["gold_label_counts"] = new_report.get("gold_label_counts")
            validation_info = new_report.get("validation", {})
            summary["gold_count"] = validation_info.get("gold_count", data_info.get("gold"))
            summary["gold_path"] = validation_info.get("gold_path")
        if old_report:
            summary["old_score"] = float(old_report.get("best", {}).get("score", 0.0))
            summary["old_vectorizer"] = old_report.get("best", {}).get("vectorizer")
            summary["old_threshold"] = old_report.get("best", {}).get("threshold")
            summary["old_margin"] = old_report.get("best", {}).get("margin")
        if summary["promoted"]:
            summary["reason"] = "score >= baseline en constraints OK"
            return summary
        reasons: List[str] = []
        if not new_report:
            reasons.append("train_report ontbreekt")
        if not old_report:
            reasons.append("baseline report ontbreekt")
        if new_report and old_report:
            new_score = float(new_report.get("best", {}).get("score", 0.0))
            old_score = float(old_report.get("best", {}).get("score", 0.0))
            if new_score < old_score:
                reasons.append(f"score lager ({new_score:.3f} < {old_score:.3f})")
            if not _constraints_ok(new_report):
                details = _constraints_reason(new_report)
                reasons.append(details or "constraints niet gehaald")
        if not reasons and log_reason:
            reasons.append(log_reason)
        summary["reason"] = "; ".join(reasons) if reasons else "onbekend"
        return summary

    def _cmdrec_train_python() -> str:
        candidate = os.path.join(
            repo_root, "py3_command_recognition_train", "venv", "Scripts", "python.exe"
        )
        if os.path.exists(candidate):
            return candidate
        return sys.executable

    def _reviewed_count() -> int:
        reviewed_path = os.path.join(al_dir, "reviewed.jsonl")
        with al_lock:
            entries = _load_jsonl(reviewed_path)
        return len(entries)

    def _review_queue_stale(latest: Optional[str]) -> Tuple[bool, int, int]:
        queue_path = os.path.join(al_dir, "review_queue.jsonl")
        entries = _load_jsonl(queue_path)
        total = len(entries)
        if not latest or not entries:
            return False, total, 0
        stale = 0
        for entry in entries:
            bundle = _bundle_name_from_path(entry.get("cmdrec_bundle"))
            if not bundle or bundle != latest:
                stale += 1
        return stale > 0, total, stale

    def _sync_retrain_state(state: Dict[str, Any]) -> Tuple[Dict[str, Any], Optional[str]]:
        latest = _latest_bundle_name()
        if latest and latest != state.get("last_retrain_bundle"):
            state["last_retrain_bundle"] = latest
            state["since_last_retrain"] = 0
            state["last_retrain_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            _save_retrain_state(state)
        return state, latest

    def _paginate(entries: List[Dict[str, Any]], offset: int, limit: int) -> Tuple[List[Dict[str, Any]], int]:
        total = len(entries)
        if offset < 0:
            offset = 0
        if limit <= 0:
            limit = 20
        return entries[offset : offset + limit], total

    def _al_event(
        sid: str,
        text: str,
        *,
        suggested_label: str,
        predicted_label: Optional[str] = None,
        confidence: Optional[float] = None,
        reason: Optional[str] = None,
        top3: Optional[List[Any]] = None,
        cmdrec_bundle: Optional[str] = None,
        stt_used: Optional[bool] = None,
        stt_edited: Optional[bool] = None,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "id": uuid.uuid4().hex,
            "session_id": sid,
            "text": text,
            "suggested_label": suggested_label,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        if stt_used is not None:
            payload["stt_used"] = bool(stt_used)
        if stt_edited is not None:
            payload["stt_edited"] = bool(stt_edited)
        if predicted_label:
            payload["predicted_label"] = predicted_label
        if confidence is not None:
            payload["confidence"] = confidence
        if reason:
            payload["reason"] = reason
        if top3:
            payload["top3"] = top3
        if cmdrec_bundle:
            payload["cmdrec_bundle"] = cmdrec_bundle
        return payload

    def _make_debug(decision, debug_enabled: bool) -> Optional[Dict[str, Any]]:
        if not debug_enabled:
            return None
        return {
            "routed_as": "command" if decision.is_command else "dialog",
            "reason": decision.reason or None,
            "label": decision.command.label if decision.command else None,
            "confidence": decision.command.confidence if decision.command else None,
            "top3": decision.top3,
        }

    def _expire_pending_if_needed(sid: str, history: History) -> None:
        pending = pending_confirms.get(sid)
        if not pending:
            return
        if time.time() - pending["created_at"] <= pending["timeout_s"]:
            return
        pending_confirms.pop(sid, None)
        cmd = pending.get("command")
        if cmd is not None:
            _set_last_action(sid, _format_action_summary("Verlopen", cmd.label, cmd.resolved))
        _append_assistant(history, "⏳ Bevestiging verlopen.")

    def _extract_stt_meta(input_meta: Optional[Dict[str, Any]]) -> Tuple[Optional[bool], Optional[bool]]:
        if not input_meta:
            return None, None
        stt_used = input_meta.get("stt_used") if "stt_used" in input_meta else None
        stt_edited = input_meta.get("stt_edited") if "stt_edited" in input_meta else None
        if stt_used is not None:
            stt_used = bool(stt_used)
        if stt_edited is not None:
            stt_edited = bool(stt_edited)
        return stt_used, stt_edited

    @app.get("/")
    def index():
        return send_from_directory("web", "index.html")

    @app.get("/server")
    def server_ui():
        resp = send_from_directory("web", "index.html")
        resp.set_cookie("ui_mode", "server", max_age=60 * 60 * 24 * 30, samesite="Lax")
        return resp

    @app.get("/client")
    def client_ui():
        resp = send_from_directory("web", "index.html")
        resp.set_cookie("ui_mode", "client", max_age=60 * 60 * 24 * 30, samesite="Lax")
        return resp

    @app.get("/active_learning")
    def active_learning_ui():
        return send_from_directory("web", "index.html")

    @app.get("/health")
    def health():
        return jsonify({"ok": True})

    @app.get("/api/state")
    def api_state():
        sid = _get_sid()
        history = _get_history(sid)
        resp = jsonify({"ok": True, "history": history, "system_prompt": _system_prompt_for_sid(sid)})
        resp.set_cookie("sid", sid, max_age=60 * 60 * 24 * 7, samesite="Lax")
        return resp

    @app.post("/api/transcribe")
    def api_transcribe():
        sid = _get_sid()
        if "audio" in request.files:
            wav_bytes = request.files["audio"].read()
        else:
            wav_bytes = request.get_data()

        if not wav_bytes:
            return jsonify({"ok": False, "error": "Geen audio ontvangen."}), 400

        audio = UtteranceAudio(pcm=wav_bytes, sample_rate=16000, channels=1, sample_width=2)
        res = _get_stt(sid).transcribe(audio)
        return jsonify(
            {
                "ok": True,
                "transcript": res.text,
                "language": getattr(res, "language", ""),
                "confidence": getattr(res, "confidence", None),
            }
        )

    @app.post("/api/transcribe_mic")
    def api_transcribe_mic():
        sid = _get_sid()
        runtime_cfg = _get_runtime_cfg(sid)
        merged = _apply_runtime_overrides(base_cfg, runtime_cfg)
        input_cfg = merged.get("input", {}) or {}
        if (input_cfg.get("type") or "audio").lower() != "audio":
            return jsonify({"ok": False, "error": "Input type is geen audio."}), 400

        mic = _make_mic_from_config(merged, mode="ptt")
        timeout_s = _resolve_start_timeout((input_cfg.get("params") or {}), default=10.0)
        audio = mic.capture_utterance(timeout_s=timeout_s)
        res = _get_stt(sid).transcribe(audio)
        return jsonify(
            {
                "ok": True,
                "transcript": res.text,
                "language": getattr(res, "language", ""),
                "confidence": getattr(res, "confidence", None),
            }
        )

    @app.post("/api/ptt_start")
    def api_ptt_start():
        sid = _get_sid()
        state = _get_ptt_state(sid)
        if state.get("running"):
            return jsonify({"ok": True, "running": True})

        runtime_cfg = _get_runtime_cfg(sid)
        merged = _apply_runtime_overrides(base_cfg, runtime_cfg)
        input_cfg = merged.get("input", {}) or {}
        if (input_cfg.get("type") or "audio").lower() != "audio":
            return jsonify({"ok": False, "error": "Input type is geen audio."}), 400

        try:
            mic = _make_mic_from_config(merged, mode="ptt")
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

        stop_event = threading.Event()
        state.update(
            {
                "running": True,
                "stop": stop_event,
                "last_error": None,
                "audio": None,
                "vad_cfg": getattr(mic, "cfg", None),
            }
        )

        def _worker():
            try:
                max_duration_s = getattr(getattr(mic, "cfg", None), "max_utterance_s", None)
                audio = mic.record_until(stop_event, max_duration_s=max_duration_s)
                state["audio"] = audio
            except Exception as exc:
                state["last_error"] = str(exc)
            finally:
                state["running"] = False

        t = threading.Thread(target=_worker, daemon=True)
        state["thread"] = t
        t.start()
        return jsonify({"ok": True, "running": True})

    @app.post("/api/ptt_stop")
    def api_ptt_stop():
        sid = _get_sid()
        state = _get_ptt_state(sid)
        stop_event = state.get("stop")
        if stop_event is not None:
            stop_event.set()
            state["stop"] = None
        t = state.get("thread")
        if t is not None and t.is_alive():
            t.join(timeout=2.0)

        audio = state.get("audio")
        if audio is None:
            err = state.get("last_error") or "Geen audio opgenomen."
            return jsonify({"ok": False, "error": err}), 400

        runtime_cfg = _get_runtime_cfg(sid)
        use_vad = bool(runtime_cfg.get("ptt_use_vad", False))
        if use_vad and state.get("vad_cfg") is not None:
            try:
                int16_audio, sample_rate = _wav_bytes_to_int16_mono(audio.pcm)
                capturer = RmsVadUtteranceCapturer(state["vad_cfg"])
                trimmed = capturer.capture_from_buffer(int16_audio)
                wav_bytes = int16_to_wav_bytes(trimmed, sample_rate)
                audio = UtteranceAudio(pcm=wav_bytes, sample_rate=sample_rate, channels=1, sample_width=2)
            except Exception:
                pass

        res = _get_stt(sid).transcribe(audio)
        state["audio"] = None
        return jsonify(
            {
                "ok": True,
                "transcript": res.text,
                "language": getattr(res, "language", ""),
                "confidence": getattr(res, "confidence", None),
            }
        )


    def _process_text(
        sid: str,
        text: str,
        *,
        emit_req: str = "pipeline",
        reset: bool = False,
        input_meta: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        emit_used = "pipeline" if str(emit_req).lower() == "pipeline" else "none"
        stt_used, stt_edited = _extract_stt_meta(input_meta)
        _touch_activity()

        pipeline = _get_pipeline(sid)
        debug_cmdrec, cmdrec, behavior_executor = _pipeline_props(pipeline)
        runtime_cfg = _get_runtime_cfg(sid)
        if reset:
            sessions[sid] = []
            _set_last_action(sid, None)

        history = _get_history(sid)
        _expire_pending_if_needed(sid, history)

        pending = pending_confirms.get(sid)
        if pending and cmdrec is not None:
            state = _get_cmdrec_state(sid)
            decision = cmdrec.route(text, state["mode"], state["active_behavior"])
            debug = _make_debug(decision, debug_cmdrec)

            if decision.is_command and decision.command and decision.command.label == "STOP":
                _append_user(history, text)
                pending_confirms.pop(sid, None)
                if behavior_executor:
                    behavior_executor.execute(decision.command)
                state["mode"] = "DIALOG"
                state["active_behavior"] = None
                _set_last_action(
                    sid,
                    _format_action_summary(
                        "Uitgevoerd",
                        decision.command.label,
                        decision.command.resolved,
                    ),
                )
                _append_assistant(history, "OK. Uitgevoerd: STOP")
                _al_append(
                    "review_queue.jsonl",
                    _al_event(
                        sid,
                        text,
                        suggested_label=decision.command.label,
                        predicted_label=decision.command.label,
                        confidence=decision.command.confidence,
                        reason="auto_executed",
                        top3=decision.top3,
                        cmdrec_bundle=str(getattr(cmdrec, "bundle_path", "") or ""),
                        stt_used=stt_used,
                        stt_edited=stt_edited,
                    ),
                )
                payload = {
                    "ok": True,
                    "reply": "OK. Uitgevoerd: STOP",
                    "history": history,
                    "system_prompt": _system_prompt_for_sid(sid),
                    "emit_used": emit_used,
                }
                if debug:
                    payload["debug"] = debug
                return payload

            _append_user(history, text)
            _append_assistant(history, "Bevestig of annuleer eerst.")
            payload = {
                "ok": True,
                "reply": "Bevestig of annuleer eerst.",
                "history": history,
                "system_prompt": _system_prompt_for_sid(sid),
                "emit_used": emit_used,
            }
            if debug:
                payload["debug"] = debug
            return payload

        debug_for_response = None

        # If we are waiting for a dance choice, try to resolve before normal routing.
        state = _get_cmdrec_state(sid)
        if state.get("pending_dance") and cmdrec is not None:
            catalog = _dance_catalog(cmdrec)
            policy = _dance_followup_policy(cmdrec)
            if _dance_is_pass_through(text, policy):
                state["pending_dance"] = None
            else:
                if _dance_is_cancel(text, policy):
                    state["pending_dance"] = None
                    _append_user(history, text)
                    _append_assistant(history, "OK, geen dans.")
                    _emit_followup_text(
                        pipeline=pipeline,
                        emit_used=emit_used,
                        runtime_cfg=runtime_cfg,
                        text="OK, geen dans.",
                    )
                    return {
                        "ok": True,
                        "reply": "OK, geen dans.",
                        "history": history,
                        "system_prompt": _system_prompt_for_sid(sid),
                        "emit_used": emit_used,
                    }

                if _dance_is_list_request(text, policy):
                    keys = _dance_keys(catalog)
                    listing = ", ".join(keys) if keys else "(geen lijst beschikbaar)"
                    _append_user(history, text)
                    reply_text = "Ik kan: " + listing
                    _append_assistant(history, reply_text)
                    _emit_followup_text(
                        pipeline=pipeline,
                        emit_used=emit_used,
                        runtime_cfg=runtime_cfg,
                        text=reply_text,
                    )
                    return {
                        "ok": True,
                        "reply": "",
                        "history": history,
                        "system_prompt": _system_prompt_for_sid(sid),
                        "emit_used": emit_used,
                    }

                dance_key, dance_behavior = _resolve_dance_from_text(text, catalog)
                if dance_key is None and _dance_is_default(text, policy):
                    dance_key = _policy_value(policy, "default_dance_key", "happy")
                    # fill behavior from catalog if available
                    for item in catalog:
                        if (item.get("key") or "").strip() == dance_key:
                            dance_behavior = item.get("behavior")
                            break

                if dance_key is None:
                    keys = _dance_keys(catalog)
                    limit = int(_policy_value(policy, "prompt_examples_limit", 6))
                    hint = ", ".join(keys[:limit]) if keys else ""
                    _append_user(history, text)
                    reply_text = _pick_dance_followup_prompt(policy, hint)
                    _append_assistant(history, reply_text)
                    _emit_followup_text(
                        pipeline=pipeline,
                        emit_used=emit_used,
                        runtime_cfg=runtime_cfg,
                        text=reply_text,
                    )
                    return {
                        "ok": True,
                        "reply": "",
                        "history": history,
                        "system_prompt": _system_prompt_for_sid(sid),
                        "emit_used": emit_used,
                    }

                state["pending_dance"] = None
                resolved = {"dance_key": dance_key}
                if isinstance(dance_behavior, str) and dance_behavior.strip():
                    resolved["dance_behavior"] = dance_behavior
                decision = RouteDecision(
                    is_command=True,
                    command=CommandDecision(
                        label="DANCE",
                        confidence=1.0,
                        raw_text=text,
                        resolved=resolved,
                    ),
                    reason="dance_resolved",
                    top3=None,
                )
                debug = _make_debug(decision, debug_cmdrec)
                debug_for_response = debug
                result = _handle_command_decision(
                    sid=sid,
                    decision=decision,
                    text=text,
                    history=history,
                    emit_used=emit_used,
                    debug=debug,
                    stt_used=stt_used,
                    stt_edited=stt_edited,
                    cmdrec_obj=cmdrec,
                    behavior_executor=behavior_executor,
                    runtime_cfg=runtime_cfg,
                )
                if result is not None:
                    return result
        if cmdrec is not None:
            state = _get_cmdrec_state(sid)
            decision = cmdrec.route(text, state["mode"], state["active_behavior"])
            debug = _make_debug(decision, debug_cmdrec)
            debug_for_response = debug

            # DANCE resolved? If not, ask follow-up and mark pending.
            if decision.is_command and decision.command and decision.command.label == "DANCE":
                resolved = decision.command.resolved or {}
                dance_key = resolved.get("dance_key")
                if not dance_key or str(dance_key).strip().lower() == "unknown":
                    state["pending_dance"] = True
                    catalog = _dance_catalog(cmdrec)
                    policy = _dance_followup_policy(cmdrec)
                    keys = _dance_keys(catalog)
                    limit = int(_policy_value(policy, "prompt_examples_limit", 6))
                    hint = ", ".join(keys[:limit]) if keys else ""
                    _append_user(history, text)
                    reply_text = _pick_dance_followup_prompt(policy, hint)
                    _append_assistant(history, reply_text)
                    _emit_followup_text(
                        pipeline=pipeline,
                        emit_used=emit_used,
                        runtime_cfg=runtime_cfg,
                        text=reply_text,
                    )
                    payload = {
                        "ok": True,
                        "reply": "",
                        "history": history,
                        "system_prompt": _system_prompt_for_sid(sid),
                        "emit_used": emit_used,
                    }
                    if debug:
                        payload["debug"] = debug
                    return payload

            result = _handle_command_decision(
                sid=sid,
                decision=decision,
                text=text,
                history=history,
                emit_used=emit_used,
                debug=debug,
                stt_used=stt_used,
                stt_edited=stt_edited,
                cmdrec_obj=cmdrec,
                behavior_executor=behavior_executor,
                runtime_cfg=runtime_cfg,
            )
            if result is not None:
                return result

            if not decision.is_command and decision.reason != "disabled":
                _al_append(
                    "review_queue.jsonl",
                    _al_event(
                        sid,
                        text,
                        suggested_label="NONE",
                        predicted_label=None,
                        confidence=None,
                        reason=decision.reason or "none",
                        top3=decision.top3,
                        cmdrec_bundle=str(getattr(cmdrec, "bundle_path", "") or ""),
                        stt_used=stt_used,
                        stt_edited=stt_edited,
                    ),
                )

        output_enabled = _output_enabled(runtime_cfg)
        if not output_enabled:
            emit_used = "none"
        output_backend = pipeline.output if emit_used == "pipeline" else NoOpOutputBackend()
        status_to_console = pipeline.status_to_console if emit_used == "pipeline" else False

        base_prompt = getattr(pipeline, "system_prompt_base", None)
        if base_prompt is None:
            base_prompt = getattr(pipeline, "system_prompt", None)
        runtime_context_enabled = bool(
            getattr(pipeline, "runtime_context_enabled", False)
            or getattr(pipeline, "_runtime_context_enabled", False)
        )
        runtime_context_static = getattr(pipeline, "runtime_context_static", None)
        if runtime_context_static is None:
            runtime_context_static = getattr(pipeline, "_runtime_context_static", None)
        last_action = _get_runtime_state(sid).get("last_action")

        pipeline = InputLLMOutputPipeline(
            input_backend=FixedTextInputBackend(text),
            llm=pipeline.llm,
            output_backend=output_backend,
            status_to_console=status_to_console,
            system_prompt=base_prompt,
            log_messages_path=pipeline.log_messages_path,
            log_meta=pipeline.log_meta,
            max_history_turns=pipeline.max_history_turns,
            cmdrec_recognizer=None,
            behavior_executor=None,
            debug_cmdrec=False,
            runtime_context_enabled=runtime_context_enabled,
            runtime_context_static=runtime_context_static,
            runtime_last_action=last_action,
        )

        turn = pipeline.run_once(history=history)
        reply = (turn.llm.reply or "").strip()
        if reply and emit_used == "pipeline" and output_enabled:
            _touch_activity()

        sessions[sid] = _append_turn(history, text, reply)

        payload = {
            "ok": True,
            "reply": reply,
            "history": sessions[sid],
            "system_prompt": _system_prompt_for_sid(sid),
            "emit_used": emit_used,
        }
        if debug_for_response:
            payload["debug"] = debug_for_response
        return payload

    @app.post("/api/send")
    def api_send():
        payload = request.get_json(force=True, silent=True) or {}
        text = (payload.get("text") or "").strip()
        if not text:
            return jsonify({"ok": False, "error": "Lege tekst."}), 400

        emit_req = (payload.get("emit") or "pipeline")
        sid = _get_sid()
        result = _process_text(
            sid,
            text,
            emit_req=emit_req,
            reset=payload.get("reset") is True,
            input_meta=payload.get("input_meta"),
        )
        resp = jsonify(result)
        resp.set_cookie("sid", sid, max_age=60 * 60 * 24 * 7, samesite="Lax")
        return resp

    def _continuous_eye_color_for_phase(phase: str) -> str:
        phase_norm = (phase or "").strip().lower()
        if phase_norm in ("wake_listening", "idle"):
            return "#1E4DFF"  # idle/waiting for wake word
        if phase_norm == "listening":
            return "#00C853"  # actively listening
        if phase_norm in ("thinking", "transcribing"):
            return "#FFA000"  # processing / not listening
        return "#1E4DFF"

    def _set_continuous_eye_phase(runtime_cfg: JsonLike, state: Dict[str, Any], phase: str) -> None:
        # Avoid spamming NAO with repeated color updates for the same phase.
        if state.get("eye_phase") == phase:
            return
        now = time.time()
        last_try = float(state.get("eye_last_try") or 0.0)
        if (now - last_try) < 0.15:
            return
        state["eye_last_try"] = now

        base_url, mode = _resolve_nao_audio_url(runtime_cfg)
        if not base_url:
            return

        color = _continuous_eye_color_for_phase(phase)
        url = base_url + "/nao/set_eye_color" if mode == "behavior" else base_url + "/set_eye_color"
        try:
            requests.post(url, json={"color": color, "duration": 0.2}, timeout=1.2)
            state["eye_phase"] = phase
        except Exception:
            pass

    def _set_continuous_eye_color(runtime_cfg: JsonLike, state: Dict[str, Any], color: str, *, duration: float = 0.2) -> None:
        base_url, mode = _resolve_nao_audio_url(runtime_cfg)
        if not base_url:
            return
        url = base_url + "/nao/set_eye_color" if mode == "behavior" else base_url + "/set_eye_color"
        try:
            requests.post(url, json={"color": color, "duration": float(duration)}, timeout=1.2)
            state["eye_phase"] = "custom"
            state["eye_last_try"] = time.time()
        except Exception:
            pass

    @app.post("/api/continuous_start")
    def api_continuous_start():
        sid = _get_sid()
        state = _get_continuous_state(sid)
        if state.get("running"):
            return jsonify({"ok": True, "running": True, "error": state.get("last_error")})

        stop_event = threading.Event()
        state["stop"] = stop_event
        state["running"] = True
        state["last_error"] = None
        state["eye_phase"] = None
        state["eye_last_try"] = 0.0

        def _set_phase(phase: str, runtime_cfg_local: Optional[JsonLike] = None) -> None:
            state["phase"] = phase
            cfg_local = runtime_cfg_local if runtime_cfg_local is not None else _get_runtime_cfg(sid)
            _set_continuous_eye_phase(cfg_local, state, phase)

        _set_phase("listening")

        def _worker():
            try:
                while not stop_event.is_set():
                    runtime_cfg = _get_runtime_cfg(sid)
                    merged = _apply_runtime_overrides(base_cfg, runtime_cfg)
                    wake_mode = _wake_mode(runtime_cfg)
                    wake_timeout_s = _wake_timeout_s(runtime_cfg)
                    wake_words = _wake_words(runtime_cfg)
                    if wake_mode == "always":
                        _set_phase("listening" if state.get("wake_open_once") else "wake_listening", runtime_cfg)
                    elif wake_mode == "timeout":
                        _set_phase(
                            "listening" if (time.time() <= float(state.get("wake_open_until", 0.0) or 0.0)) else "wake_listening",
                            runtime_cfg,
                        )
                    else:
                        _set_phase("listening", runtime_cfg)
                        state["wake_open_once"] = False
                        state["wake_open_until"] = 0.0
                    try:
                        mic = _make_mic_from_config(merged, mode="continuous")
                    except Exception as exc:
                        state["last_error"] = str(exc)
                        _set_phase("idle", runtime_cfg)
                        time.sleep(1.0)
                        continue

                    timeout_s = _resolve_start_timeout(
                        (merged.get("input", {}).get("params") or {}),
                        default=10**9,
                    )
                    try:
                        audio = mic.capture_utterance(timeout_s=timeout_s)
                    except TimeoutError:
                        _set_phase("listening", runtime_cfg)
                        continue
                    except Exception as exc:
                        state["last_error"] = str(exc)
                        _set_phase("idle", runtime_cfg)
                        time.sleep(0.5)
                        continue

                    if stop_event.is_set():
                        break

                    use_wake_gate = wake_mode != "never"
                    if use_wake_gate:
                        now_s = time.time()
                        if wake_mode == "always":
                            gate_open = bool(state.get("wake_open_once"))
                        else:
                            gate_open = now_s <= float(state.get("wake_open_until", 0.0) or 0.0)
                    else:
                        gate_open = True

                    if not gate_open:
                        _set_phase("wake_listening", runtime_cfg)
                        wake_key = (tuple([_normalize_text(w) for w in wake_words]),)
                        if state.get("wake_stt_key") != wake_key or state.get("wake_stt") is None:
                            try:
                                state["wake_stt"] = _build_wake_stt(merged, wake_words)
                                state["wake_stt_key"] = wake_key
                            except Exception as exc:
                                state["last_error"] = "wake-stt: " + str(exc)
                                _set_phase("idle", runtime_cfg)
                                time.sleep(0.5)
                                continue
                        try:
                            wake_res = state["wake_stt"].transcribe(audio)
                            wake_text = (wake_res.text or "").strip()
                            state["last_wake_text"] = wake_text
                        except Exception as exc:
                            state["last_error"] = "wake-stt: " + str(exc)
                            _set_phase("idle", runtime_cfg)
                            continue
                        if not _text_has_wake_word(wake_text, wake_words):
                            _set_phase("wake_listening", runtime_cfg)
                            continue
                        state["last_error"] = None
                        if wake_mode == "always":
                            state["wake_open_once"] = True
                        else:
                            state["wake_open_until"] = time.time() + float(wake_timeout_s)
                        _set_phase("listening", runtime_cfg)
                        continue

                    _set_phase("transcribing", runtime_cfg)
                    try:
                        res = _get_stt(sid).transcribe(audio)
                    except Exception as exc:
                        state["last_error"] = str(exc)
                        _set_phase("idle", runtime_cfg)
                        continue

                    text_in = (res.text or "").strip()
                    _set_phase("thinking", runtime_cfg)
                    if not text_in:
                        if wake_mode == "timeout" and float(state.get("wake_open_until", 0.0) or 0.0) < time.time():
                            _set_phase("wake_listening", runtime_cfg)
                        continue

                    try:
                        with pipeline_lock:
                            _process_text(
                                sid,
                                text_in,
                                emit_req="pipeline",
                                reset=False,
                                input_meta={"stt_used": True, "stt_edited": False},
                            )
                        if wake_mode == "always":
                            state["wake_open_once"] = False
                            _set_phase("wake_listening", runtime_cfg)
                        elif wake_mode == "timeout":
                            state["wake_open_until"] = time.time() + float(wake_timeout_s)
                            _set_phase("listening", runtime_cfg)
                        else:
                            _set_phase("listening", runtime_cfg)
                        state["last_error"] = None
                    except Exception as exc:
                        state["last_error"] = str(exc)
                        _set_phase("idle", runtime_cfg)
                        continue
            finally:
                state["running"] = False
                state["wake_open_once"] = False
                state["wake_open_until"] = 0.0
                state["last_wake_text"] = ""
                if state.get("phase") != "stopped":
                    _set_phase("idle")

        t = threading.Thread(target=_worker, daemon=True)
        state["thread"] = t
        t.start()
        return jsonify({"ok": True, "running": True})

    @app.post("/api/continuous_stop")
    def api_continuous_stop():
        sid = _get_sid()
        state = _get_continuous_state(sid)
        runtime_cfg = _get_runtime_cfg(sid)
        stop_event = state.get("stop")
        if stop_event is not None:
            stop_event.set()
        state["running"] = False
        state["wake_open_once"] = False
        state["wake_open_until"] = 0.0
        state["last_wake_text"] = ""
        state["phase"] = "stopped"
        _set_continuous_eye_color(runtime_cfg, state, "#D50000", duration=0.25)
        return jsonify({"ok": True, "running": False})

    @app.get("/api/continuous_state")
    def api_continuous_state():
        sid = _get_sid()
        state = _get_continuous_state(sid)
        return jsonify(
            {
                "ok": True,
                "running": bool(state.get("running")),
                "error": state.get("last_error"),
                "phase": state.get("phase"),
                "last_wake_text": state.get("last_wake_text", ""),
            }
        )

    @app.get("/api/al_retrain_state")
    def api_al_retrain_state():
        state = _load_retrain_state()
        state, latest = _sync_retrain_state(state)
        stale, total, stale_count = _review_queue_stale(latest)
        return jsonify(
            {
                "ok": True,
                "state": state,
                "latest_bundle": latest,
                "review_queue_stale": stale,
                "review_queue_count": total,
                "review_queue_stale_count": stale_count,
            }
        )

    def _retrain_status_snapshot() -> Dict[str, Any]:
        with retrain_lock:
            return {
                "running": bool(retrain_state.get("running")),
                "started_at": retrain_state.get("started_at"),
                "finished_at": retrain_state.get("finished_at"),
                "exit_code": retrain_state.get("exit_code"),
                "last_error": retrain_state.get("last_error"),
                "summary": retrain_state.get("summary"),
                "log": list(retrain_state.get("log") or []),
            }

    def _run_retrain_job() -> None:
        reviewed_path = os.path.join(al_dir, "reviewed.jsonl")
        auto_train_path = os.path.join(al_dir, "auto_train.jsonl")
        auto_queue_path = os.path.join(al_dir, "auto_train_queue.jsonl")
        with al_lock:
            reviewed_count = len(_load_jsonl(reviewed_path))
            auto_count = len(_load_jsonl(auto_train_path))
            auto_queue_count = len(_load_jsonl(auto_queue_path))
        auto_sample = int(math.floor(0.5 * reviewed_count))
        state = _load_retrain_state()
        state, _ = _sync_retrain_state(state)
        since = int(state.get("since_last_retrain", 0))
        _append_retrain_log(
            f"INFO: AL counts: reviewed={reviewed_count} auto_train={auto_count} auto_train_queue={auto_queue_count} new_since_last_retrain={since}"
        )
        _append_retrain_log(f"INFO: Auto-train sample cap: {auto_sample} (50% van reviewed)")
        cmd = [
            _cmdrec_train_python(),
            "tools/retrain_cmdrec.py",
            "--out",
            "auto",
            "--promote-if-better",
            "--auto-train-sample",
            str(auto_sample),
            "--auto-train-queue",
            "data/al/auto_train_queue.jsonl",
            "--auto-train-conf-max",
            "0.75",
        ]
        workdir = os.path.join(repo_root, "py3_command_recognition_train")
        env = os.environ.copy()
        src_path = os.path.join(repo_root, "py3_command_recognition_train", "src")
        env["PYTHONPATH"] = src_path + os.pathsep + env.get("PYTHONPATH", "")
        baseline_path = _latest_bundle_path()
        promoted_to = None
        bundle_written = None
        log_reason = None
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=workdir,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                env=env,
            )
            assert proc.stdout is not None
            for raw_line in proc.stdout:
                line = raw_line.rstrip()
                _append_retrain_log(line)
                if "Promoted bundle to " in line:
                    promoted_to = line.split("Promoted bundle to ", 1)[1].strip()
                elif "Not promoted" in line:
                    log_reason = line.strip()
                elif "Bundle written to " in line:
                    bundle_written = line.split("Bundle written to ", 1)[1].strip()
            exit_code = proc.wait()
            summary = _summarize_retrain(
                out_path=bundle_written,
                promoted_to=promoted_to,
                baseline_path=baseline_path,
                log_reason=log_reason,
            )
            with retrain_lock:
                retrain_state["running"] = False
                retrain_state["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                retrain_state["exit_code"] = exit_code
                retrain_state["last_error"] = None if exit_code == 0 else "retrain failed"
                retrain_state["summary"] = summary
        except Exception as exc:
            with retrain_lock:
                retrain_state["running"] = False
                retrain_state["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                retrain_state["exit_code"] = -1
                retrain_state["last_error"] = str(exc)
                retrain_state["summary"] = None

    @app.get("/api/retrain_status")
    def api_retrain_status():
        return jsonify({"ok": True, **_retrain_status_snapshot()})

    @app.post("/api/retrain_start")
    def api_retrain_start():
        with retrain_lock:
            if retrain_state.get("running"):
                return jsonify({"ok": False, "error": "Retrain loopt al."}), 409
            retrain_state["running"] = True
            retrain_state["started_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            retrain_state["finished_at"] = None
            retrain_state["exit_code"] = None
            retrain_state["last_error"] = None
            retrain_state["summary"] = None
            retrain_state["log"] = []
        thread = threading.Thread(target=_run_retrain_job, daemon=True)
        thread.start()
        return jsonify({"ok": True})

    @app.get("/api/review_queue")
    def api_review_queue():
        try:
            offset = int(request.args.get("offset", 0))
            limit = int(request.args.get("limit", 20))
        except Exception:
            offset, limit = 0, 20
        sort_mode = (request.args.get("sort") or "").strip().lower()
        queue_path = os.path.join(al_dir, "review_queue.jsonl")
        entries = _load_jsonl(queue_path)
        if sort_mode in ("confidence", "confidence_asc", "conf"):
            def _conf_value(entry: Dict[str, Any]) -> Optional[float]:
                conf = entry.get("confidence")
                if conf is not None:
                    try:
                        return float(conf)
                    except Exception:
                        return None
                top3 = entry.get("top3")
                if isinstance(top3, list) and top3:
                    try:
                        return float(top3[0][1])
                    except Exception:
                        return None
                return None

            def _conf_key(entry: Dict[str, Any]) -> Tuple[int, float]:
                conf_val = _conf_value(entry)
                if conf_val is None:
                    return (1, 0.0)
                return (0, conf_val)
            entries = sorted(entries, key=_conf_key)
        elif sort_mode in ("oldest", "asc"):
            pass
        else:
            entries = list(reversed(entries))
        page, total = _paginate(entries, offset, limit)
        return jsonify({"ok": True, "items": page, "total": total, "offset": offset, "limit": limit})

    @app.get("/api/reviewed")
    def api_reviewed():
        try:
            offset = int(request.args.get("offset", 0))
            limit = int(request.args.get("limit", 20))
        except Exception:
            offset, limit = 0, 20
        reviewed_path = os.path.join(al_dir, "reviewed.jsonl")
        entries = _load_jsonl(reviewed_path)
        entries = list(reversed(entries))
        page, total = _paginate(entries, offset, limit)
        return jsonify({"ok": True, "items": page, "total": total, "offset": offset, "limit": limit})

    def _sort_al_entries(entries: List[Dict[str, Any]], sort_mode: str) -> List[Dict[str, Any]]:
        if sort_mode in ("confidence", "confidence_asc", "conf"):
            def _conf_value(entry: Dict[str, Any]) -> Optional[float]:
                conf = entry.get("confidence")
                if conf is not None:
                    try:
                        return float(conf)
                    except Exception:
                        return None
                top3 = entry.get("top3")
                if isinstance(top3, list) and top3:
                    try:
                        return float(top3[0][1])
                    except Exception:
                        return None
                return None

            def _conf_key(entry: Dict[str, Any]) -> Tuple[int, float]:
                conf_val = _conf_value(entry)
                if conf_val is None:
                    return (1, 0.0)
                return (0, conf_val)

            return sorted(entries, key=_conf_key)
        if sort_mode in ("oldest", "asc"):
            return entries
        return list(reversed(entries))

    @app.get("/api/auto_train")
    def api_auto_train():
        try:
            offset = int(request.args.get("offset", 0))
            limit = int(request.args.get("limit", 20))
        except Exception:
            offset, limit = 0, 20
        sort_mode = (request.args.get("sort") or "").strip().lower()
        auto_path = os.path.join(al_dir, "auto_train.jsonl")
        entries = _load_jsonl(auto_path)
        entries = _sort_al_entries(entries, sort_mode)
        page, total = _paginate(entries, offset, limit)
        return jsonify({"ok": True, "items": page, "total": total, "offset": offset, "limit": limit})

    @app.get("/api/auto_train_queue")
    def api_auto_train_queue():
        try:
            offset = int(request.args.get("offset", 0))
            limit = int(request.args.get("limit", 20))
        except Exception:
            offset, limit = 0, 20
        sort_mode = (request.args.get("sort") or "").strip().lower()
        auto_path = os.path.join(al_dir, "auto_train_queue.jsonl")
        entries = _load_jsonl(auto_path)
        entries = _sort_al_entries(entries, sort_mode)
        page, total = _paginate(entries, offset, limit)
        return jsonify({"ok": True, "items": page, "total": total, "offset": offset, "limit": limit})

    @app.post("/api/review_requeue")
    def api_review_requeue():
        payload = request.get_json(force=True, silent=True) or {}
        source = (payload.get("source") or "").strip().lower()
        entry_id = str(payload.get("id") or "").strip()
        text = (payload.get("text") or "").strip()
        label = (payload.get("label") or "").strip().upper()
        if source not in {"reviewed", "auto_train", "auto_train_queue"}:
            return jsonify({"ok": False, "error": "Invalid source."}), 400
        if not entry_id and not text:
            return jsonify({"ok": False, "error": "id/text ontbreekt."}), 400
        source_path = os.path.join(al_dir, f"{source}.jsonl")
        queue_path = os.path.join(al_dir, "review_queue.jsonl")
        sid = _get_sid()
        pipeline = _get_pipeline(sid)
        _, cmdrec, _ = _pipeline_props(pipeline)
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with al_lock:
            source_entries = _load_jsonl(source_path)
            base_entry = None
            if entry_id:
                for entry in source_entries:
                    if str(entry.get("id") or "") == entry_id:
                        base_entry = entry
                        break
            if base_entry is None and text:
                key = _normalize_key(text)
                for entry in source_entries:
                    if _normalize_key(entry.get("text", "")) == key:
                        base_entry = entry
                        break
            if base_entry is None:
                return jsonify({"ok": False, "error": "Item niet gevonden."}), 404

            text = (text or base_entry.get("text") or "").strip()
            if not text:
                return jsonify({"ok": False, "error": "Text ontbreekt."}), 400

            suggested = (
                label
                or base_entry.get("label")
                or base_entry.get("suggested_label")
                or base_entry.get("predicted_label")
                or "NONE"
            )
            suggested = str(suggested).strip().upper() or "NONE"

            queue_entries = _load_jsonl(queue_path)
            queue_keys = {_normalize_key(entry.get("text", "")) for entry in queue_entries}
            key = _normalize_key(text)

            # Remove from source
            pruned = [
                entry
                for entry in source_entries
                if _normalize_key(entry.get("text", "")) != key
            ]
            _write_jsonl(pruned, source_path)

            if key in queue_keys:
                return jsonify({"ok": True, "added": False, "note": "bestond al in review_queue"})

            predicted_label = base_entry.get("predicted_label") or suggested
            confidence = base_entry.get("confidence")
            top3 = base_entry.get("top3")
            cmdrec_bundle = base_entry.get("cmdrec_bundle")
            if cmdrec is not None:
                decision = cmdrec.route(text, "DIALOG", None)
                if decision.is_command and decision.command:
                    predicted_label = decision.command.label
                    confidence = decision.command.confidence
                else:
                    if decision.top3:
                        try:
                            confidence = float(decision.top3[0][1])
                        except Exception:
                            confidence = None
                top3 = decision.top3
                if getattr(cmdrec, "bundle_path", None):
                    cmdrec_bundle = str(cmdrec.bundle_path)

            review_entry = _al_event(
                sid,
                text,
                suggested_label=suggested,
                predicted_label=predicted_label,
                confidence=confidence,
                reason="manual_requeue",
                top3=top3,
                cmdrec_bundle=cmdrec_bundle,
                stt_used=base_entry.get("stt_used"),
                stt_edited=base_entry.get("stt_edited"),
            )
            review_entry["requeued_from"] = source
            review_entry["requeued_at"] = now
            queue_entries.append(review_entry)
            _write_jsonl(queue_entries, queue_path)
        return jsonify({"ok": True, "added": True})

    @app.post("/api/al_review")
    def api_al_review():
        payload = request.get_json(force=True, silent=True) or {}
        text = (payload.get("text") or "").strip()
        label = (payload.get("label") or "").strip().upper()
        if not text or not label:
            return jsonify({"ok": False, "error": "text/label ontbreekt."}), 400

        gold_candidate = bool(payload.get("gold_candidate", False))
        notes = (payload.get("notes") or "").strip()
        suggested = (payload.get("suggested_label") or label or "NONE").strip().upper()
        stt_used = payload.get("stt_used")
        stt_edited = payload.get("stt_edited")

        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        entry_id = uuid.uuid4().hex
        record = {
            "id": entry_id,
            "text": text,
            "suggested_label": suggested,
            "confidence": payload.get("confidence"),
            "reason": payload.get("reason") or "ui_review",
            "stt_used": stt_used,
            "stt_edited": stt_edited,
            "reviewed_label": label,
            "keep": 1,
            "gold_candidate": 1 if gold_candidate else 0,
            "notes": notes,
            "timestamp": now,
            "reviewed_at": now,
        }

        reviewed_path = os.path.join(al_dir, "reviewed.jsonl")
        review_log_path = os.path.join(al_dir, "review_log.jsonl")
        gold_candidates_path = os.path.join(al_dir, "gold_candidates.jsonl")
        queue_path = os.path.join(al_dir, "review_queue.jsonl")
        auto_train_path = os.path.join(al_dir, "auto_train.jsonl")

        with al_lock:
            reviewed_entries = _load_jsonl(reviewed_path)
            reviewed_keys = {_normalize_key(entry.get("text", "")) for entry in reviewed_entries}
            key = _normalize_key(text)
            added_reviewed = False
            if key and key not in reviewed_keys:
                reviewed_entries.append({"text": text, "label": label, "source": "ui_review"})
                _write_jsonl(reviewed_entries, reviewed_path)
                added_reviewed = True

            _al_append("review_log.jsonl", record)

            if os.path.exists(auto_train_path):
                auto_entries = _load_jsonl(auto_train_path)
                auto_entries = [entry for entry in auto_entries if _normalize_key(entry.get("text", "")) != key]
                _write_jsonl(auto_entries, auto_train_path)

            if gold_candidate:
                gold_entries = _load_jsonl(gold_candidates_path)
                gold_keys = {_normalize_key(entry.get("text", "")) for entry in gold_entries}
                if key and key not in gold_keys:
                    gold_entries.append({"text": text, "label": label, "source": "al_gold_candidate"})
                    _write_jsonl(gold_entries, gold_candidates_path)

            if os.path.exists(queue_path):
                queue_entries = _load_jsonl(queue_path)
                pruned = [entry for entry in queue_entries if _normalize_key(entry.get("text", "")) != key]
                _write_jsonl(pruned, queue_path)

        state = _load_retrain_state()
        state, latest = _sync_retrain_state(state)
        if added_reviewed:
            state["since_last_retrain"] = int(state.get("since_last_retrain", 0)) + 1
            _save_retrain_state(state)

        return jsonify({"ok": True, "added": added_reviewed, "state": state, "latest_bundle": latest})

    @app.post("/api/review_apply")
    def api_review_apply():
        payload = request.get_json(force=True, silent=True) or {}
        rid = str(payload.get("id") or "").strip()
        text = (payload.get("text") or "").strip()
        keep = bool(payload.get("keep", True))
        reviewed_label = (payload.get("reviewed_label") or "").strip().upper()
        gold_candidate = bool(payload.get("gold_candidate", False))
        notes = (payload.get("notes") or "").strip()

        if keep and not reviewed_label:
            return jsonify({"ok": False, "error": "reviewed_label ontbreekt."}), 400

        queue_path = os.path.join(al_dir, "review_queue.jsonl")
        review_log_path = os.path.join(al_dir, "review_log.jsonl")
        reviewed_path = os.path.join(al_dir, "reviewed.jsonl")
        gold_candidates_path = os.path.join(al_dir, "gold_candidates.jsonl")
        auto_train_path = os.path.join(al_dir, "auto_train.jsonl")

        with al_lock:
            queue_entries = _load_jsonl(queue_path)
            base_entry = None
            if rid:
                for entry in queue_entries:
                    if str(entry.get("id") or "") == rid:
                        base_entry = entry
                        break
            if base_entry is None and text:
                key = _normalize_key(text)
                for entry in queue_entries:
                    if _normalize_key(entry.get("text", "")) == key:
                        base_entry = entry
                        break
            if base_entry is None:
                return jsonify({"ok": False, "error": "Item niet gevonden in queue."}), 404

            text = (text or base_entry.get("text") or "").strip()
            suggested = (base_entry.get("suggested_label") or base_entry.get("predicted_label") or "NONE").strip().upper()
            record = {
                "id": base_entry.get("id") or uuid.uuid4().hex,
                "text": text,
                "suggested_label": suggested,
                "confidence": base_entry.get("confidence"),
                "reason": base_entry.get("reason") or "ui_review",
                "stt_used": base_entry.get("stt_used"),
                "stt_edited": base_entry.get("stt_edited"),
                "reviewed_label": reviewed_label or suggested,
                "keep": 1 if keep else 0,
                "gold_candidate": 1 if gold_candidate else 0,
                "notes": notes,
                "timestamp": base_entry.get("timestamp") or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "reviewed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
            _al_append("review_log.jsonl", record)

            added_reviewed = False
            if keep:
                reviewed_entries = _load_jsonl(reviewed_path)
                reviewed_keys = {_normalize_key(entry.get("text", "")) for entry in reviewed_entries}
                key = _normalize_key(text)
                if key and key not in reviewed_keys:
                    reviewed_entries.append({"text": text, "label": reviewed_label, "source": "al_reviewed"})
                    _write_jsonl(reviewed_entries, reviewed_path)
                    added_reviewed = True

                if os.path.exists(auto_train_path):
                    auto_entries = _load_jsonl(auto_train_path)
                    auto_entries = [entry for entry in auto_entries if _normalize_key(entry.get("text", "")) != key]
                    _write_jsonl(auto_entries, auto_train_path)

                if gold_candidate:
                    gold_entries = _load_jsonl(gold_candidates_path)
                    gold_keys = {_normalize_key(entry.get("text", "")) for entry in gold_entries}
                    if key and key not in gold_keys:
                        gold_entries.append({"text": text, "label": reviewed_label, "source": "al_gold_candidate"})
                        _write_jsonl(gold_entries, gold_candidates_path)

            # Remove from queue (prefer id match, fallback to normalized text)
            base_id = str(base_entry.get("id") or "")
            base_key = _normalize_key(base_entry.get("text", ""))
            if base_id:
                pruned = [entry for entry in queue_entries if str(entry.get("id") or "") != base_id]
            else:
                pruned = [entry for entry in queue_entries if _normalize_key(entry.get("text", "")) != base_key]
            _write_jsonl(pruned, queue_path)

        state = _load_retrain_state()
        state, latest = _sync_retrain_state(state)
        if added_reviewed:
            state["since_last_retrain"] = int(state.get("since_last_retrain", 0)) + 1
            _save_retrain_state(state)

        return jsonify({"ok": True, "added": added_reviewed, "state": state, "latest_bundle": latest})

    @app.post("/api/review_queue_rescore")
    def api_review_queue_rescore():
        sid = _get_sid()
        pipeline = _get_pipeline(sid)
        _, cmdrec, _ = _pipeline_props(pipeline)
        if cmdrec is None:
            return jsonify({"ok": False, "error": "CmdRec niet beschikbaar."}), 400
        queue_path = os.path.join(al_dir, "review_queue.jsonl")
        auto_path = os.path.join(al_dir, "auto_train.jsonl")
        auto_queue_path = os.path.join(al_dir, "auto_train_queue.jsonl")
        latest = _latest_bundle_name()
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        updated = 0
        auto_updated = 0
        auto_queue_updated = 0
        with al_lock:
            entries = _load_jsonl(queue_path)
            for entry in entries:
                text = (entry.get("text") or "").strip()
                if not text:
                    continue
                decision = cmdrec.route(text, "DIALOG", None)
                predicted_label = "NONE"
                confidence = None
                if decision.is_command and decision.command:
                    predicted_label = decision.command.label
                    confidence = decision.command.confidence
                else:
                    if decision.top3:
                        try:
                            confidence = float(decision.top3[0][1])
                        except Exception:
                            confidence = None
                entry["predicted_label"] = predicted_label
                entry["confidence"] = confidence
                entry["top3"] = decision.top3
                if getattr(cmdrec, "bundle_path", None):
                    entry["cmdrec_bundle"] = str(cmdrec.bundle_path)
                if latest:
                    entry["rescored_bundle"] = latest
                entry["rescored_at"] = now
                updated += 1
            auto_entries = _load_jsonl(auto_path)
            for entry in auto_entries:
                text = (entry.get("text") or "").strip()
                if not text:
                    continue
                decision = cmdrec.route(text, "DIALOG", None)
                predicted_label = "NONE"
                confidence = None
                if decision.is_command and decision.command:
                    predicted_label = decision.command.label
                    confidence = decision.command.confidence
                else:
                    if decision.top3:
                        try:
                            confidence = float(decision.top3[0][1])
                        except Exception:
                            confidence = None
                entry["predicted_label"] = predicted_label
                entry["confidence"] = confidence
                entry["top3"] = decision.top3
                if getattr(cmdrec, "bundle_path", None):
                    entry["cmdrec_bundle"] = str(cmdrec.bundle_path)
                if latest:
                    entry["rescored_bundle"] = latest
                entry["rescored_at"] = now
                auto_updated += 1
            if auto_entries:
                _write_jsonl(auto_entries, auto_path)
            auto_queue_entries = _load_jsonl(auto_queue_path)
            for entry in auto_queue_entries:
                text = (entry.get("text") or "").strip()
                if not text:
                    continue
                decision = cmdrec.route(text, "DIALOG", None)
                predicted_label = "NONE"
                confidence = None
                if decision.is_command and decision.command:
                    predicted_label = decision.command.label
                    confidence = decision.command.confidence
                else:
                    if decision.top3:
                        try:
                            confidence = float(decision.top3[0][1])
                        except Exception:
                            confidence = None
                entry["predicted_label"] = predicted_label
                entry["confidence"] = confidence
                entry["top3"] = decision.top3
                if getattr(cmdrec, "bundle_path", None):
                    entry["cmdrec_bundle"] = str(cmdrec.bundle_path)
                if latest:
                    entry["rescored_bundle"] = latest
                entry["rescored_at"] = now
                auto_queue_updated += 1
            if auto_queue_entries:
                _write_jsonl(auto_queue_entries, auto_queue_path)
            # Deduplicate by text (keep lowest confidence; tie => newest timestamp)
            best: Dict[str, Tuple[float, str, int, Dict[str, Any]]] = {}
            for idx, entry in enumerate(entries):
                key = _normalize_key(entry.get("text", ""))
                if not key:
                    continue
                conf_raw = entry.get("confidence")
                try:
                    conf_val = float(conf_raw) if conf_raw is not None else 1e9
                except Exception:
                    conf_val = 1e9
                ts = str(entry.get("timestamp") or "")
                if key not in best:
                    best[key] = (conf_val, ts, idx, entry)
                    continue
                prev_conf, prev_ts, prev_idx, _ = best[key]
                if conf_val < prev_conf or (conf_val == prev_conf and ts > prev_ts):
                    best[key] = (conf_val, ts, idx, entry)
            deduped = [item[3] for item in sorted(best.values(), key=lambda item: item[2])]
            _write_jsonl(deduped, queue_path)
        return jsonify(
            {
                "ok": True,
                "updated": updated,
                "auto_updated": auto_updated,
                "auto_queue_updated": auto_queue_updated,
                "total": len(entries),
                "deduped": len(deduped),
                "latest_bundle": latest,
            }
        )

    @app.post("/api/confirm")
    def api_confirm():
        payload = request.get_json(force=True, silent=True) or {}
        confirmation_id = (payload.get("confirmation_id") or "").strip()
        confirmed = bool(payload.get("confirmed"))

        if not confirmation_id:
            return jsonify({"ok": False, "error": "Missing confirmation_id."}), 400

        sid = _get_sid()
        history = _get_history(sid)
        _expire_pending_if_needed(sid, history)
        pending = pending_confirms.get(sid)
        if not pending or pending.get("confirmation_id") != confirmation_id:
            return jsonify({"ok": False, "error": "Confirmation verlopen of onbekend.", "history": history}), 400

        pending_confirms.pop(sid, None)
        cmd = pending["command"]
        pipeline = _get_pipeline(sid)
        _, _, behavior_executor = _pipeline_props(pipeline)
        stt_used, stt_edited = _extract_stt_meta(pending.get("input_meta"))

        if confirmed:
            if behavior_executor:
                behavior_executor.execute(cmd)
            _set_last_action(sid, _format_action_summary("Uitgevoerd", cmd.label, cmd.resolved))
            _append_assistant(history, f"✅ Uitgevoerd: {cmd.label}")
            summary = _format_action_summary("Behavior", cmd.label, cmd.resolved)
            if not behavior_executor:
                summary += " (geen behavior uitgevoerd)"
            _append_assistant(history, summary)
            _al_append(
                "auto_train_queue.jsonl",
                _al_event(
                    sid,
                    str(pending.get("input_text") or cmd.raw_text or ""),
                    suggested_label=cmd.label,
                    predicted_label=cmd.label,
                    confidence=pending.get("confidence") or cmd.confidence,
                    reason="approved",
                    top3=pending.get("top3"),
                    cmdrec_bundle=str(getattr(_get_pipeline(sid), "_cmdrec", None).bundle_path)
                    if getattr(_get_pipeline(sid), "_cmdrec", None)
                    else None,
                    stt_used=stt_used,
                    stt_edited=stt_edited,
                ),
            )
        else:
            _set_last_action(sid, _format_action_summary("Geannuleerd", cmd.label, cmd.resolved))
            _append_assistant(history, "❌ Geannuleerd")
            _al_append(
                "review_queue.jsonl",
                _al_event(
                    sid,
                    str(pending.get("input_text") or cmd.raw_text or ""),
                    suggested_label=cmd.label,
                    predicted_label=cmd.label,
                    confidence=pending.get("confidence") or cmd.confidence,
                    reason="declined",
                    top3=pending.get("top3"),
                    cmdrec_bundle=str(getattr(_get_pipeline(sid), "_cmdrec", None).bundle_path)
                    if getattr(_get_pipeline(sid), "_cmdrec", None)
                    else None,
                    stt_used=stt_used,
                    stt_edited=stt_edited,
                ),
            )

        resp = jsonify(
            {
                "ok": True,
                "history": history,
                "system_prompt": _system_prompt_for_sid(sid),
            }
        )
        resp.set_cookie("sid", sid, max_age=60 * 60 * 24 * 7, samesite="Lax")
        return resp

    @app.post("/api/reset")
    def api_reset():
        sid = _get_sid()
        sessions[sid] = []
        _set_last_action(sid, None)
        resp = jsonify({"ok": True, "history": [], "system_prompt": _system_prompt_for_sid(sid)})
        resp.set_cookie("sid", sid, max_age=60 * 60 * 24 * 7, samesite="Lax")
        return resp

    def _list_audio_devices() -> Dict[str, Any]:
        if sd is None:
            return {"devices": [], "default_input": None}
        devices = []
        try:
            raw = sd.query_devices()
            hostapis = sd.query_hostapis()
            default_hostapi = sd.default.hostapi
            default_in = sd.default.device[0] if sd.default.device else None
            for idx, d in enumerate(raw):
                if d.get("max_input_channels", 0) <= 0:
                    continue
                name = d.get("name", "")
                if "microsoft sound mapper" in name.lower():
                    continue
                if default_hostapi is not None and d.get("hostapi") != default_hostapi:
                    continue
                devices.append({"index": idx, "name": name, "default": idx == default_in})
            if not devices:
                for idx, d in enumerate(raw):
                    if d.get("max_input_channels", 0) <= 0:
                        continue
                    name = d.get("name", "")
                    if "microsoft sound mapper" in name.lower():
                        continue
                    devices.append({"index": idx, "name": name, "default": idx == default_in})
            return {"devices": devices, "default_input": default_in}
        except Exception:
            return {"devices": [], "default_input": None}

    def _list_audio_output_devices() -> Dict[str, Any]:
        if sd is None:
            return {"devices": [], "default_output": None}
        devices = []
        try:
            raw = sd.query_devices()
            default_hostapi = sd.default.hostapi
            default_out = sd.default.device[1] if sd.default.device else None
            for idx, d in enumerate(raw):
                if d.get("max_output_channels", 0) <= 0:
                    continue
                name = d.get("name", "")
                if "microsoft sound mapper" in name.lower():
                    continue
                if default_hostapi is not None and d.get("hostapi") != default_hostapi:
                    continue
                devices.append({"index": idx, "name": name, "default": idx == default_out})
            if not devices:
                for idx, d in enumerate(raw):
                    if d.get("max_output_channels", 0) <= 0:
                        continue
                    name = d.get("name", "")
                    if "microsoft sound mapper" in name.lower():
                        continue
                    devices.append({"index": idx, "name": name, "default": idx == default_out})
            return {"devices": devices, "default_output": default_out}
        except Exception:
            return {"devices": [], "default_output": None}

    def _piper_models_root() -> Path:
        env_root = os.environ.get("PIPER_MODELS_DIR")
        if env_root:
            return Path(env_root)
        return Path(repo_root) / "piper_tts_models"

    def _list_piper_models() -> Tuple[List[Dict[str, Any]], Optional[str]]:
        root = _piper_models_root()
        if not root.exists():
            return [], None
        models: List[Dict[str, Any]] = []
        for onnx_path in sorted(root.rglob("*.onnx")):
            config_path = Path(str(onnx_path) + ".json")
            if not config_path.exists():
                continue
            try:
                cfg = json.loads(config_path.read_text(encoding="utf-8"))
            except Exception:
                cfg = {}
            sample_rate = (cfg.get("audio") or {}).get("sample_rate")
            inference = cfg.get("inference") or {}
            def _safe_float(value: Any, fallback: float) -> float:
                try:
                    return float(value)
                except Exception:
                    return float(fallback)
            defaults = {
                "length_scale": _safe_float(inference.get("length_scale", 1.0), 1.0),
                "noise_scale": _safe_float(inference.get("noise_scale", 0.667), 0.667),
                "noise_w_scale": _safe_float(inference.get("noise_w", 0.8), 0.8),
                "sentence_silence": 0.0,
                "volume": 1.0,
            }
            rel = onnx_path.relative_to(root)
            parts = rel.parts
            label = "/".join(parts[-4:-1]) if len(parts) >= 4 else "/".join(parts[:-1])
            models.append(
                {
                    "path": str(onnx_path),
                    "label": label or onnx_path.stem,
                    "sample_rate": sample_rate,
                    "defaults": defaults,
                }
            )
        default_model = None
        for m in models:
            if "nathalie" in m["label"].lower() and "medium" in m["label"].lower():
                default_model = m["path"]
                break
        if default_model is None and models:
            default_model = models[0]["path"]
        return models, default_model

    def _azure_env() -> Tuple[Optional[str], Optional[str]]:
        key = os.environ.get("AZURE_SPEECH_KEY") or os.environ.get("AZURE_TTS_KEY")
        region = os.environ.get("AZURE_SPEECH_REGION") or os.environ.get("AZURE_TTS_REGION")
        return key, region

    def _list_azure_voices() -> Tuple[List[Dict[str, Any]], Optional[str], Optional[str]]:
        key, region = _azure_env()
        if not key or not region:
            return [], None, "AZURE_SPEECH_KEY of AZURE_SPEECH_REGION ontbreekt."
        url = f"https://{region}.tts.speech.microsoft.com/cognitiveservices/voices/list"
        try:
            resp = requests.get(url, headers={"Ocp-Apim-Subscription-Key": key}, timeout=5)
        except requests.RequestException as exc:
            return [], None, f"Azure voices fetch failed: {exc}"
        if resp.status_code >= 400:
            return [], None, f"Azure voices fetch failed ({resp.status_code})."
        try:
            payload = resp.json()
        except ValueError:
            return [], None, "Azure voices response is not JSON."
        voices_raw: List[Dict[str, Any]] = []
        for item in payload if isinstance(payload, list) else []:
            short_name = item.get("ShortName")
            if not short_name:
                continue
            voices_raw.append(
                {
                    "short_name": short_name,
                    "locale": item.get("Locale"),
                    "gender": item.get("Gender"),
                    "voice_type": item.get("VoiceType"),
                }
            )
        voices = [v for v in voices_raw if str(v.get("locale") or "").lower().startswith("nl-")]
        if not voices:
            voices = voices_raw
        default_voice = None
        for loc in ("nl-NL", "nl-BE"):
            for v in voices:
                if v.get("locale") == loc and str(v.get("voice_type") or "").lower() == "neural":
                    default_voice = v["short_name"]
                    break
            if default_voice:
                break
        if default_voice is None and voices:
            default_voice = voices[0]["short_name"]
        return voices, default_voice, None

    def _check_ping(url: Optional[str], path: str) -> bool:
        if not url:
            return False
        try:
            resp = requests.get(url + path, timeout=2.5)
        except requests.RequestException:
            return False
        if resp.status_code >= 400:
            return False
        try:
            data = resp.json()
        except ValueError:
            return True
        return data.get("status") == "ok"

    def _output_enabled(runtime_cfg: JsonLike) -> bool:
        target = (runtime_cfg.get("output_target") or "").lower()
        if target == "none":
            return False
        if target == "server":
            return True
        base_enabled = bool(runtime_cfg.get("base_enabled", True))
        if not base_enabled:
            return False
        base_url = _normalize_url(runtime_cfg.get("nao_base_url"))
        if not _check_ping(base_url, "/ping"):
            return False
        return True

    @app.get("/api/audio_devices")
    def api_audio_devices():
        return jsonify({"ok": True, **_list_audio_devices()})

    @app.get("/api/audio_output_devices")
    def api_audio_output_devices():
        return jsonify({"ok": True, **_list_audio_output_devices()})

    @app.get("/api/piper_models")
    def api_piper_models():
        models, default_model = _list_piper_models()
        return jsonify({"ok": True, "models": models, "default_model": default_model})

    @app.get("/api/azure_voices")
    def api_azure_voices():
        voices, default_voice, err = _list_azure_voices()
        if err:
            return jsonify({"ok": False, "error": err, "voices": []})
        return jsonify({"ok": True, "voices": voices, "default_voice": default_voice})

    @app.get("/api/cmdrec_bundles")
    def api_cmdrec_bundles():
        bundles_dir = base_cfg.get("cmdrec_bundles_dir", "dist")
        items = []
        try:
            if not os.path.isabs(bundles_dir):
                repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                bundles_dir = os.path.join(repo_root, bundles_dir)
            if not os.path.isdir(bundles_dir):
                repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                alt = os.path.join(repo_root, "py3_command_recognition_train", "dist")
                if os.path.isdir(alt):
                    bundles_dir = alt
            for name in sorted(os.listdir(bundles_dir)):
                if name.startswith(".") or name.lower() == "backup":
                    continue
                match = re.match(r"bundle_v(\d+)(?:_(\d{8}))?$", name, re.IGNORECASE)
                if match:
                    version = int(match.group(1))
                    date_part = match.group(2)
                    if date_part:
                        items.append(f"v{version}_{date_part}")
                    else:
                        items.append(f"v{version}")
                    continue
                match = re.match(r"v(\d+)(?:_(\d{8}))?$", name, re.IGNORECASE)
                if match:
                    version = int(match.group(1))
                    date_part = match.group(2)
                    if date_part:
                        items.append(f"v{version}_{date_part}")
                    else:
                        items.append(f"v{version}")
        except OSError:
            pass
        def _sort_key(value: str) -> tuple[int, int]:
            match = re.match(r"v(\d+)(?:_(\d{8}))?$", value, re.IGNORECASE)
            if not match:
                return (0, 0)
            version = int(match.group(1))
            date_part = int(match.group(2)) if match.group(2) else 0
            return (version, date_part)

        items = sorted({i for i in items}, key=_sort_key, reverse=True)
        return jsonify({"ok": True, "bundles": items})

    @app.get("/api/master_prompts")
    def api_master_prompts():
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        prompts_dir = os.path.join(repo_root, "py3_dialog_manager", "configs", "master_prompts")
        items = []
        try:
            for name in sorted(os.listdir(prompts_dir)):
                if name.startswith(".") or not name.lower().endswith(".txt"):
                    continue
                items.append("master_prompts/" + name)
        except OSError:
            pass
        return jsonify({"ok": True, "prompts": items})

    @app.get("/api/agent_presets")
    def api_agent_presets():
        presets = _load_agent_presets()
        return jsonify({"ok": True, "presets": presets})

    @app.post("/api/agent_presets")
    def api_agent_presets_upsert():
        payload = request.get_json(force=True, silent=True) or {}
        config = payload.get("config")
        if not isinstance(config, dict):
            return jsonify({"ok": False, "error": "config ontbreekt"}), 400
        name = (payload.get("name") or "").strip() or "Preset"
        mode = (payload.get("mode") or "overwrite").lower()
        preset_id = (payload.get("id") or "").strip() or None

        presets = _load_agent_presets()
        by_id = {p.get("id"): p for p in presets if isinstance(p, dict) and p.get("id")}

        if mode == "create" or preset_id is None:
            new_id = _unique_preset_id(_slugify(name), presets)
            preset = {
                "id": new_id,
                "name": name,
                "locked": False,
                "config": config,
            }
            presets.append(preset)
            _save_agent_presets(presets)
            return jsonify({"ok": True, "preset": preset, "presets": presets})

        existing = by_id.get(preset_id)
        if existing is None:
            return jsonify({"ok": False, "error": "preset bestaat niet"}), 404
        if existing.get("locked"):
            return jsonify({"ok": False, "error": "default preset kan niet worden overschreven"}), 400

        existing["name"] = name or existing.get("name") or preset_id
        existing["config"] = config
        _save_agent_presets(presets)
        return jsonify({"ok": True, "preset": existing, "presets": presets})

    @app.post("/api/agent_presets_delete")
    def api_agent_presets_delete():
        payload = request.get_json(force=True, silent=True) or {}
        preset_id = (payload.get("id") or "").strip()
        if not preset_id:
            return jsonify({"ok": False, "error": "id ontbreekt"}), 400
        presets = _load_agent_presets()
        for idx, preset in enumerate(presets):
            if preset.get("id") != preset_id:
                continue
            if preset.get("locked"):
                return jsonify({"ok": False, "error": "default preset kan niet worden verwijderd"}), 400
            presets.pop(idx)
            _save_agent_presets(presets)
            return jsonify({"ok": True, "presets": presets})
        return jsonify({"ok": False, "error": "preset bestaat niet"}), 404

    @app.get("/api/ollama_models_local")
    def api_ollama_models_local():
        if not shutil.which("ollama"):
            return jsonify({"ok": True, "models": [], "error": "ollama cli niet gevonden"})
        try:
            proc = subprocess.run(
                ["ollama", "list"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            lines = (proc.stdout or "").splitlines()
            models = []
            for line in lines[1:]:
                parts = line.split()
                if parts:
                    models.append(parts[0])
            return jsonify({"ok": True, "models": models})
        except Exception as exc:
            return jsonify({"ok": True, "models": [], "error": str(exc)})

    @app.get("/api/ollama_models_cloud")
    def api_ollama_models_cloud():
        return jsonify({"ok": True, "models": ["gpt-oss:120b", "gpt-oss:20b"]})

    @app.get("/api/runtime_config")
    def api_runtime_config():
        sid = _get_sid()
        cfg_out = _get_runtime_cfg(sid)
        resp = jsonify({"ok": True, "config": cfg_out, "system_prompt": _system_prompt_for_sid(sid)})
        resp.set_cookie("sid", sid, max_age=60 * 60 * 24 * 7, samesite="Lax")
        return resp

    @app.get("/api/runtime_effective")
    def api_runtime_effective():
        sid = _get_sid()
        runtime_cfg = _get_runtime_cfg(sid)
        merged = _apply_runtime_overrides(base_cfg, runtime_cfg)
        output_enabled = _output_enabled(runtime_cfg)
        resp = jsonify(
            {
                "ok": True,
                "runtime_config": runtime_cfg,
                "effective_config": merged,
                "output_enabled": output_enabled,
            }
        )
        resp.set_cookie("sid", sid, max_age=60 * 60 * 24 * 7, samesite="Lax")
        return resp

    @app.post("/api/runtime_config")
    def api_runtime_config_set():
        payload = request.get_json(force=True, silent=True) or {}
        sid = _get_sid()
        if payload.get("reset"):
            runtime_cfg_by_sid.pop(sid, None)
            pipeline_by_sid.pop(sid, None)
            stt_by_sid.pop(sid, None)
            cfg_out = _get_runtime_cfg(sid)
            resp = jsonify({"ok": True, "config": cfg_out, "system_prompt": _system_prompt_for_sid(sid)})
            resp.set_cookie("sid", sid, max_age=60 * 60 * 24 * 7, samesite="Lax")
            return resp

        runtime_cfg = _get_runtime_cfg(sid)
        runtime_cfg.update(payload.get("config", payload))
        runtime_cfg_by_sid[sid] = runtime_cfg

        if runtime_cfg.get("base_enabled", True):
            base_url = _normalize_url(runtime_cfg.get("nao_base_url"))
            llm_type = (runtime_cfg.get("llm_type") or "").lower()
            llm_model = (runtime_cfg.get("llm_model") or "").strip()
            if llm_type != "echo" and not llm_model:
                return jsonify({"ok": False, "error": "LLM model ontbreekt."}), 400

        merged = _apply_runtime_overrides(base_cfg, runtime_cfg)
        _rebuild_pipeline_for_sid(sid, merged)
        _apply_custom_life(sid, runtime_cfg)
        resp = jsonify(
            {
                "ok": True,
                "config": runtime_cfg,
                "system_prompt": _system_prompt_for_sid(sid),
                "effective_config": merged,
                "output_enabled": bool(runtime_cfg.get("base_enabled", True)),
            }
        )
        resp.set_cookie("sid", sid, max_age=60 * 60 * 24 * 7, samesite="Lax")
        return resp

    @app.post("/api/runtime_health")
    def api_runtime_health():
        payload = request.get_json(force=True, silent=True) or {}
        base_url = _normalize_url(payload.get("nao_base_url"))
        behavior_url = _normalize_url(payload.get("behavior_manager_url"))
        base_enabled = bool(payload.get("base_enabled", True))
        behavior_enabled = bool(payload.get("behavior_enabled", True))
        nao_ip_enabled = bool(payload.get("nao_ip_enabled", False))
        nao_host, nao_port = _parse_host_port(payload.get("nao_ip"), 9559)
        base_ping = _check_ping(base_url, "/ping") if base_enabled else False
        base_nao_ping = _check_ping(base_url, "/is_awake") if base_enabled else False
        behavior_ping = _check_ping(behavior_url, "/ping") if behavior_enabled else False
        behavior_nao_ping = _check_ping(behavior_url + "/nao" if behavior_url else None, "/is_awake") if behavior_enabled else False
        nao_ping = _check_tcp(nao_host, nao_port) if nao_ip_enabled else False
        return jsonify(
            {
                "ok": True,
                "base": {"ping": base_ping, "nao_ping": base_nao_ping},
                "behavior": {"ping": behavior_ping, "nao_ping": behavior_nao_ping},
                "nao": {"ping": nao_ping},
            }
        )

    def _resolve_nao_audio_url(runtime_cfg: JsonLike) -> Tuple[Optional[str], str]:
        """
        Kies de juiste base URL voor NAO audio (volume/tts_speed).
        Returns (base_url, mode) where mode is "behavior" or "base".
        """
        base_url = _normalize_url(runtime_cfg.get("nao_base_url"))
        behavior_url = _normalize_url(runtime_cfg.get("behavior_manager_url"))
        base_enabled = bool(runtime_cfg.get("base_enabled", True))
        behavior_enabled = bool(runtime_cfg.get("behavior_enabled", True))

        if behavior_enabled and behavior_url:
            return behavior_url, "behavior"
        if base_enabled and base_url:
            return base_url, "base"
        return None, "none"

    def _apply_custom_life(sid: str, runtime_cfg: JsonLike) -> None:
        enabled = bool(runtime_cfg.get("custom_life_enabled", False))
        settings = runtime_cfg.get("custom_life_settings") or {}
        base_url, mode = _resolve_nao_audio_url(runtime_cfg)
        if not base_url:
            return
        if mode == "behavior":
            apply_url = base_url + "/nao/custom_life_apply"
            restore_url = base_url + "/nao/custom_life_restore"
        else:
            apply_url = base_url + "/custom_life_apply"
            restore_url = base_url + "/custom_life_restore"
        state = _get_runtime_state(sid)
        prev_state = state.get("custom_life_prev_state")
        try:
            if enabled:
                resp = requests.post(apply_url, json={"settings": settings}, timeout=3.0)
                data = resp.json() if resp.ok else {}
                payload = data.get("data") if isinstance(data, dict) else None
                if isinstance(payload, dict) and payload.get("prev_state") is not None:
                    state["custom_life_prev_state"] = payload.get("prev_state")
            else:
                if prev_state:
                    requests.post(restore_url, json={"state": prev_state}, timeout=3.0)
                state.pop("custom_life_prev_state", None)
        except Exception:
            pass

    def _get_nao_audio_state(runtime_cfg: JsonLike) -> Dict[str, Any]:
        base_url, mode = _resolve_nao_audio_url(runtime_cfg)
        if not base_url:
            return {"ok": False, "error": "NAO audio endpoint ontbreekt."}

        try:
            if mode == "behavior":
                volume_url = base_url + "/nao/volume"
                speed_url = base_url + "/nao/tts_speed"
            else:
                volume_url = base_url + "/volume"
                speed_url = base_url + "/tts_speed"
            volume_resp = requests.get(volume_url, timeout=2.5)
            speed_resp = requests.get(speed_url, timeout=2.5)
            volume_data = volume_resp.json() if volume_resp.ok else {}
            speed_data = speed_resp.json() if speed_resp.ok else {}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

        volume = (volume_data.get("data") or {}).get("volume")
        speed = (speed_data.get("data") or {}).get("speed")
        return {"ok": True, "volume": volume, "speed": speed, "mode": mode}

    def _get_nao_autonomous_life_state(runtime_cfg: JsonLike) -> Dict[str, Any]:
        base_url, mode = _resolve_nao_audio_url(runtime_cfg)
        if not base_url:
            return {"ok": False, "error": "NAO audio endpoint ontbreekt."}
        try:
            url = base_url + "/nao/autonomous_life" if mode == "behavior" else base_url + "/autonomous_life"
            resp = requests.get(url, timeout=3.0)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

        payload = data.get("data") if isinstance(data, dict) else None
        if isinstance(payload, dict):
            state = payload.get("state")
            enabled = payload.get("enabled")
        else:
            state = data.get("state") if isinstance(data, dict) else None
            enabled = data.get("enabled") if isinstance(data, dict) else None
        if enabled is None and state is not None:
            enabled = str(state).lower() != "disabled"
        return {"ok": True, "state": state, "enabled": enabled}

    def _get_nao_custom_life_state(runtime_cfg: JsonLike) -> Dict[str, Any]:
        base_url, mode = _resolve_nao_audio_url(runtime_cfg)
        if not base_url:
            return {"ok": False, "error": "NAO audio endpoint ontbreekt."}
        try:
            url = base_url + "/nao/custom_life_state" if mode == "behavior" else base_url + "/custom_life_state"
            resp = requests.get(url, timeout=3.0)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

        payload = data.get("data") if isinstance(data, dict) else None
        if not isinstance(payload, dict):
            return {"ok": False, "error": "invalid response"}
        return {"ok": True, "state": payload}

    def _agent_presets_path() -> str:
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        return os.path.join(repo_root, "py3_dialog_manager", "configs", "agent_presets.json")

    def _default_agent_presets() -> List[Dict[str, Any]]:
        return [
            {
                "id": "echo",
                "name": "Echo",
                "locked": True,
                "config": {
                    "stt_type": "vosk",
                    "output_target": "none",
                    "tts_engine": "nao_native",
                    "cmdrec": "latest",
                    "confirm_policy": "when_guarded",
                    "llm_type": "echo",
                    "llm_model": "",
                    "master_prompt_file": None,
                },
            },
            {
                "id": "local",
                "name": "Local",
                "locked": True,
                "config": {
                    "stt_type": "vosk",
                    "output_target": "nao",
                    "tts_engine": "piper",
                    "cmdrec": "latest",
                    "confirm_policy": "when_guarded",
                    "llm_type": "ollama_local",
                    "llm_model": "gemma:2b",
                    "master_prompt_file": "master_prompts/short.txt",
                },
            },
            {
                "id": "cloud",
                "name": "Cloud",
                "locked": True,
                "config": {
                    "stt_type": "azure",
                    "output_target": "nao",
                    "tts_engine": "azure",
                    "cmdrec": "latest",
                    "confirm_policy": "when_guarded",
                    "llm_type": "ollama_cloud",
                    "llm_model": "gpt-oss:120b",
                    "master_prompt_file": "master_prompts/atlantis.txt",
                },
            },
        ]


    def _load_agent_presets() -> List[Dict[str, Any]]:
        path = _agent_presets_path()
        defaults = _default_agent_presets()
        presets: List[Dict[str, Any]] = []
        try:
            with open(path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
            if isinstance(raw, dict):
                presets = raw.get("presets") or []
            elif isinstance(raw, list):
                presets = raw
        except Exception:
            presets = []
        if not isinstance(presets, list):
            presets = []

        by_id = {p.get("id"): p for p in presets if isinstance(p, dict) and p.get("id")}
        changed = False
        for default in defaults:
            existing = by_id.get(default["id"])
            if existing is None:
                presets.append(default)
                changed = True
            else:
                if existing.get("locked") is not True:
                    existing["locked"] = True
                    changed = True
                if not existing.get("name"):
                    existing["name"] = default["name"]
                    changed = True

        if not os.path.isfile(path):
            changed = True
        if changed:
            _save_agent_presets(presets)
        return presets

    def _save_agent_presets(presets: List[Dict[str, Any]]) -> None:
        path = _agent_presets_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"presets": presets}, fh, ensure_ascii=False, indent=2)

    def _slugify(value: str) -> str:
        slug = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
        return slug or "preset"

    def _unique_preset_id(slug: str, presets: List[Dict[str, Any]]) -> str:
        ids = {p.get("id") for p in presets if isinstance(p, dict)}
        if slug not in ids:
            return slug
        for idx in range(2, 1000):
            cand = f"{slug}_{idx}"
            if cand not in ids:
                return cand
        return slug + "_" + uuid.uuid4().hex[:4]

    def _flatten_behaviors(payload: Any) -> List[str]:
        if isinstance(payload, list):
            return [str(item) for item in payload if item]
        if isinstance(payload, dict):
            out: List[str] = []
            for folder, items in payload.items():
                if not isinstance(items, list):
                    continue
                for name in items:
                    if not name:
                        continue
                    if folder:
                        out.append(str(folder).rstrip("/") + "/" + str(name))
                    else:
                        out.append(str(name))
            return out
        return []

    @app.get("/api/nao_audio_state")
    def api_nao_audio_state():
        sid = _get_sid()
        runtime_cfg = _get_runtime_cfg(sid)
        return jsonify(_get_nao_audio_state(runtime_cfg))

    @app.post("/api/nao_set_volume")
    def api_nao_set_volume():
        payload = request.get_json(force=True, silent=True) or {}
        volume = payload.get("volume", None)
        try:
            volume = int(volume)
        except Exception:
            return jsonify({"ok": False, "error": "Ongeldige volume waarde."}), 400
        volume = max(0, min(100, volume))

        sid = _get_sid()
        runtime_cfg = _get_runtime_cfg(sid)
        base_url, mode = _resolve_nao_audio_url(runtime_cfg)
        if not base_url:
            return jsonify({"ok": False, "error": "NAO audio endpoint ontbreekt."}), 400

        try:
            if mode == "behavior":
                url = base_url + "/nao/set_volume"
            else:
                url = base_url + "/set_volume"
            resp = requests.post(url, json={"volume": volume}, timeout=3.0)
            data = resp.json() if resp.ok else {}
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

        return jsonify({"ok": True, "volume": (data.get("data") or {}).get("volume", volume)})

    @app.post("/api/nao_set_tts_speed")
    def api_nao_set_tts_speed():
        payload = request.get_json(force=True, silent=True) or {}
        speed = payload.get("speed", None)
        try:
            speed = int(speed)
        except Exception:
            return jsonify({"ok": False, "error": "Ongeldige speed waarde."}), 400

        sid = _get_sid()
        runtime_cfg = _get_runtime_cfg(sid)
        base_url, mode = _resolve_nao_audio_url(runtime_cfg)
        if not base_url:
            return jsonify({"ok": False, "error": "NAO audio endpoint ontbreekt."}), 400

        try:
            if mode == "behavior":
                url = base_url + "/nao/tts_speed"
            else:
                url = base_url + "/tts_speed"
            resp = requests.post(url, json={"speed": speed}, timeout=3.0)
            data = resp.json() if resp.ok else {}
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

        return jsonify({"ok": True, "speed": (data.get("data") or {}).get("speed", speed)})

    @app.post("/api/nao_set_eye_color")
    def api_nao_set_eye_color():
        payload = request.get_json(force=True, silent=True) or {}
        color = (payload.get("color") or "").strip()
        duration = payload.get("duration", None)
        if not color:
            return jsonify({"ok": False, "error": "Missing 'color'."}), 400
        try:
            duration_f = float(duration) if duration is not None else 0.5
        except Exception:
            duration_f = 0.5

        runtime_cfg = _get_runtime_cfg(_get_sid())
        base_url, mode = _resolve_nao_audio_url(runtime_cfg)
        if not base_url:
            return jsonify({"ok": False, "error": "NAO audio endpoint ontbreekt."}), 400

        try:
            url = base_url + "/nao/set_eye_color" if mode == "behavior" else base_url + "/set_eye_color"
            resp = requests.post(url, json={"color": color, "duration": duration_f}, timeout=3.0)
            data = resp.json() if resp.ok else {}
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

        if isinstance(data, dict) and data.get("status") not in (None, "ok"):
            return jsonify({"ok": False, "error": data.get("error") or "set_eye_color failed"}), 400
        return jsonify({"ok": True})

    @app.get("/api/nao_autonomous_life_state")
    def api_nao_autonomous_life_state():
        runtime_cfg = _get_runtime_cfg(_get_sid())
        return jsonify(_get_nao_autonomous_life_state(runtime_cfg))

    @app.get("/api/nao_custom_life_state")
    def api_nao_custom_life_state():
        runtime_cfg = _get_runtime_cfg(_get_sid())
        return jsonify(_get_nao_custom_life_state(runtime_cfg))

    @app.post("/api/nao_autonomous_life_set")
    def api_nao_autonomous_life_set():
        payload = request.get_json(force=True, silent=True) or {}
        enabled = payload.get("enabled", None)
        state = payload.get("state", None)
        runtime_cfg = _get_runtime_cfg(_get_sid())
        base_url, mode = _resolve_nao_audio_url(runtime_cfg)
        if not base_url:
            return jsonify({"ok": False, "error": "NAO audio endpoint ontbreekt."}), 400
        try:
            url = base_url + "/nao/autonomous_life" if mode == "behavior" else base_url + "/autonomous_life"
            resp = requests.post(url, json={"enabled": enabled, "state": state}, timeout=3.0)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

        payload = data.get("data") if isinstance(data, dict) else None
        if isinstance(payload, dict):
            return jsonify({"ok": True, "state": payload.get("state"), "enabled": payload.get("enabled")})
        return jsonify({"ok": True, "state": data.get("state"), "enabled": data.get("enabled")})

    @app.get("/api/command_catalog")
    def api_command_catalog():
        sid = _get_sid()
        pipeline = _get_pipeline(sid)
        _, cmdrec, behavior_executor = _pipeline_props(pipeline)
        if cmdrec is None:
            return jsonify({"ok": False, "error": "cmdrec niet beschikbaar."}), 400
        try:
            labels = cmdrec.get_labels() or []
            exclude = {"LOCOMOTION_REQUEST", "WALK_WITH_ME"}
            commands = []
            for label in sorted(labels):
                label_upper = label.upper()
                if label_upper in exclude or "LOCOMOTION" in label_upper:
                    continue
                behavior = _command_behavior_preview(label, cmdrec, behavior_executor)
                commands.append({"label": label, "behavior": behavior})
            return jsonify({"ok": True, "commands": commands})
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 500

    @app.get("/api/cmdrec_labels")
    def api_cmdrec_labels():
        sid = _get_sid()
        pipeline = _get_pipeline(sid)
        _, cmdrec, _ = _pipeline_props(pipeline)
        if cmdrec is None:
            return jsonify({"ok": False, "error": "cmdrec niet beschikbaar."}), 400
        try:
            labels = cmdrec.get_labels() or []
            labels = sorted({str(label).strip().upper() for label in labels if label})
            if "NONE" not in labels:
                labels.insert(0, "NONE")
            return jsonify({"ok": True, "labels": labels})
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 500

    @app.get("/api/dance_catalog")
    def api_dance_catalog():
        sid = _get_sid()
        pipeline = _get_pipeline(sid)
        _, cmdrec, _ = _pipeline_props(pipeline)
        if cmdrec is None:
            return jsonify({"ok": False, "error": "cmdrec niet beschikbaar."}), 400
        try:
            catalog = _dance_catalog(cmdrec)
            dances = []
            for item in catalog:
                key = item.get("key")
                behavior = item.get("behavior")
                if isinstance(key, str) and key.strip():
                    dances.append({"key": key.strip(), "behavior": behavior})
            return jsonify({"ok": True, "dances": dances})
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 500

    @app.post("/api/command_execute")
    def api_command_execute():
        payload = request.get_json(force=True, silent=True) or {}
        label = (payload.get("label") or "").strip()
        if not label:
            return jsonify({"ok": False, "error": "Missing 'label'."}), 400
        _touch_activity()
        sid = _get_sid()
        pipeline = _get_pipeline(sid)
        _, cmdrec, behavior_executor = _pipeline_props(pipeline)
        if behavior_executor is None:
            return jsonify({"ok": False, "error": "behavior executor niet beschikbaar."}), 400
        resolved = payload.get("resolved") if isinstance(payload.get("resolved"), dict) else None
        if label.upper() == "DANCE" and not resolved:
            resolved = _default_dance_resolution(cmdrec)
        cmd = CommandDecision(label=label, confidence=1.0, raw_text=label, resolved=resolved)
        try:
            behavior_executor.execute(cmd)
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        _touch_activity()
        return jsonify({"ok": True})

    @app.get("/api/nao_behaviors")
    def api_nao_behaviors():
        runtime_cfg = _get_runtime_cfg(_get_sid())
        base_url = _normalize_url(runtime_cfg.get("nao_base_url"))
        behavior_url = _normalize_url(runtime_cfg.get("behavior_manager_url"))
        base_enabled = bool(runtime_cfg.get("base_enabled", True))
        behavior_enabled = bool(runtime_cfg.get("behavior_enabled", True))
        mode = "none"
        if behavior_enabled and behavior_url and _check_ping(behavior_url, "/ping"):
            base_url = behavior_url
            mode = "behavior"
        elif base_enabled and base_url and _check_ping(base_url, "/ping"):
            mode = "base"
        else:
            return jsonify({"ok": False, "error": "Base/behavior manager is down."}), 400
        try:
            url = base_url + "/nao/list_behaviors" if mode == "behavior" else base_url + "/list_behaviors"
            resp = requests.get(url, timeout=3.0)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

        payload = data.get("data") if isinstance(data, dict) else None
        try:
            behaviors = sorted(_flatten_behaviors(payload))
            return jsonify({"ok": True, "behaviors": behaviors})
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 500

    @app.post("/api/nao_behavior_start")
    def api_nao_behavior_start():
        payload = request.get_json(force=True, silent=True) or {}
        behavior = (payload.get("behavior") or "").strip()
        if not behavior:
            return jsonify({"ok": False, "error": "Missing 'behavior'."}), 400
        _touch_activity()
        runtime_cfg = _get_runtime_cfg(_get_sid())
        behavior_url = _normalize_url(runtime_cfg.get("behavior_manager_url"))
        base_url = _normalize_url(runtime_cfg.get("nao_base_url"))
        behavior_enabled = bool(runtime_cfg.get("behavior_enabled", True))
        base_enabled = bool(runtime_cfg.get("base_enabled", True))
        candidates: List[Tuple[str, str]] = []
        if behavior_enabled and behavior_url:
            candidates.append((behavior_url, "behavior"))
        if base_enabled and base_url:
            candidates.append((base_url, "base"))
        if not candidates:
            return jsonify({"ok": False, "error": "NAO endpoint ontbreekt."}), 400

        last_error = None
        for base_url, mode in candidates:
            pause_state = None
            if runtime_cfg.get("custom_life_enabled"):
                try:
                    pause_url = base_url + ("/nao/custom_life_pause" if mode == "behavior" else "/custom_life_pause")
                    resp = requests.post(pause_url, json={}, timeout=3.0)
                    data = resp.json() if resp.ok else {}
                    if isinstance(data, dict):
                        pause_state = data.get("data") or data.get("state")
                except Exception:
                    pause_state = None
            try:
                url = base_url + "/nao/do_behavior" if mode == "behavior" else base_url + "/do_behavior"
                resp = requests.post(url, json={"behavior": behavior}, timeout=5.0)
                data = resp.json() if resp.ok else {}
            except Exception as exc:
                last_error = str(exc)
                continue
            finally:
                if runtime_cfg.get("custom_life_enabled"):
                    try:
                        settings = runtime_cfg.get("custom_life_settings") or {}
                        if settings:
                            apply_url = base_url + (
                                "/nao/custom_life_apply" if mode == "behavior" else "/custom_life_apply"
                            )
                            requests.post(apply_url, json={"settings": settings}, timeout=3.0)
                        elif pause_state:
                            resume_url = base_url + (
                                "/nao/custom_life_resume" if mode == "behavior" else "/custom_life_resume"
                            )
                            requests.post(resume_url, json={"state": pause_state}, timeout=3.0)
                    except Exception:
                        pass
            if isinstance(data, dict) and data.get("status") not in (None, "ok"):
                last_error = data.get("error") or "behavior failed"
                continue
            _touch_activity()
            return jsonify({"ok": True})
        return jsonify({"ok": False, "error": last_error or "behavior failed"}), 400

    @app.post("/api/nao_behavior_stop")
    def api_nao_behavior_stop():
        payload = request.get_json(force=True, silent=True) or {}
        behavior = (payload.get("behavior") or "").strip()
        if not behavior:
            return jsonify({"ok": False, "error": "Missing 'behavior'."}), 400
        _touch_activity()
        runtime_cfg = _get_runtime_cfg(_get_sid())
        behavior_url = _normalize_url(runtime_cfg.get("behavior_manager_url"))
        base_url = _normalize_url(runtime_cfg.get("nao_base_url"))
        behavior_enabled = bool(runtime_cfg.get("behavior_enabled", True))
        base_enabled = bool(runtime_cfg.get("base_enabled", True))
        candidates: List[Tuple[str, str]] = []
        if behavior_enabled and behavior_url:
            candidates.append((behavior_url, "behavior"))
        if base_enabled and base_url:
            candidates.append((base_url, "base"))
        if not candidates:
            return jsonify({"ok": False, "error": "NAO endpoint ontbreekt."}), 400

        last_error = None
        for base_url, mode in candidates:
            try:
                url = base_url + "/nao/stop_behavior" if mode == "behavior" else base_url + "/stop_behavior"
                resp = requests.post(url, json={"behavior": behavior}, timeout=5.0)
                data = resp.json() if resp.ok else {}
            except Exception as exc:
                last_error = str(exc)
                continue
            if isinstance(data, dict) and data.get("status") not in (None, "ok"):
                last_error = data.get("error") or "behavior failed"
                continue
            _touch_activity()
            return jsonify({"ok": True})
        return jsonify({"ok": False, "error": last_error or "behavior failed"}), 400

    @app.get("/api/process_status")
    def api_process_status():
        status = {
            "base": _proc_status("base"),
            "behavior": _proc_status("behavior"),
        }
        return jsonify({"ok": True, "processes": status})

    @app.get("/api/process_logs")
    def api_process_logs():
        name = (request.args.get("name") or "").strip().lower()
        if name not in ("base", "behavior"):
            return jsonify({"ok": False, "error": "Unknown process name."}), 400
        with proc_lock:
            lines = list(proc_logs.get(name, []))
        return jsonify({"ok": True, "name": name, "lines": lines})

    @app.post("/api/process_logs_clear")
    def api_process_logs_clear():
        payload = request.get_json(force=True, silent=True) or {}
        name = (payload.get("name") or "").strip().lower()
        if name not in ("base", "behavior"):
            return jsonify({"ok": False, "error": "Unknown process name."}), 400
        with proc_lock:
            proc_logs[name] = []
        return jsonify({"ok": True, "name": name})

    @app.post("/api/process_start")
    def api_process_start():
        payload = request.get_json(force=True, silent=True) or {}
        name = (payload.get("name") or "").strip().lower()
        if name not in ("base", "behavior"):
            return jsonify({"ok": False, "error": "Unknown process name."}), 400

        if name == "base":
            runtime_cfg = _get_runtime_cfg(_get_sid())
            payload_nao_ip = (payload.get("nao_ip") or "").strip()
            nao_ip = payload_nao_ip or (runtime_cfg.get("nao_ip") or "").strip()
            nao_ip_enabled = bool(payload.get("nao_ip_enabled", runtime_cfg.get("nao_ip_enabled", False)))
            if payload_nao_ip:
                runtime_cfg["nao_ip"] = payload_nao_ip
            if "nao_ip_enabled" in payload:
                runtime_cfg["nao_ip_enabled"] = nao_ip_enabled
            if not nao_ip or not nao_ip_enabled:
                return jsonify({"ok": False, "error": "NAO IP ontbreekt of is disabled."}), 400
            cmd, cwd, err = _base_controller_cmd(nao_ip)
        else:
            cmd, cwd, err = _behavior_manager_cmd()

        if err:
            return jsonify({"ok": False, "error": err}), 400
        result = _start_process(name, cmd, cwd)
        return jsonify(result)

    @app.post("/api/process_stop")
    def api_process_stop():
        payload = request.get_json(force=True, silent=True) or {}
        name = (payload.get("name") or "").strip().lower()
        if name not in ("base", "behavior"):
            return jsonify({"ok": False, "error": "Unknown process name."}), 400
        if name == "base":
            runtime_cfg = _get_runtime_cfg(_get_sid())
            _try_nao_rest(runtime_cfg, reason="base_stop")
        result = _stop_process(name)
        return jsonify(result)

    return app, base_stt, base_pipeline


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", help="Pad naar configs/<file>.json")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8080)
    args = ap.parse_args()

    config_path = args.config or DEFAULT_CONFIG_PATH
    cfg = _load_json(config_path)
    app, _, _ = create_app(cfg=cfg, config_path=os.path.abspath(config_path))
    def _sig_handler(signum, frame):
        try:
            shutdown = getattr(app, "_shutdown_rest", None)
            if callable(shutdown):
                shutdown()
        except Exception:
            pass
        raise SystemExit(0)

    try:
        signal.signal(signal.SIGINT, _sig_handler)
        signal.signal(signal.SIGTERM, _sig_handler)
    except Exception:
        pass
    #app.run(host=args.host, port=args.port, debug=False, threaded=True, ssl_context="adhoc")
    app.run(host=args.host, port=args.port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
