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
import hashlib
import json
import queue
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
import unicodedata
from pathlib import Path
import signal
from urllib.parse import urlparse
from typing import Any, Callable, Dict, List, Optional, Tuple

from flask import Flask, Response, g, has_request_context, jsonify, request, send_from_directory
import requests
import numpy as np
from werkzeug.exceptions import HTTPException

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
_LISTEN_MODE_VALUES = {"ptt", "continuous"}
_CONFIRM_POLICY_VALUES = {"when_guarded", "always", "never"}
_UI_ACTIVE_TAB_VALUES = {"prompt", "runtime", "commands", "camera", "review", "retrain", "logs"}
_UI_COLOR_SCHEME_VALUES = {
    "default",
    "brown",
    "forest",
    "gold",
    "rose",
    "teal",
    "slate",
    "night_blue",
    "night_brown",
    "night_teal",
    "amber",
    "dark",
    "night",
}
_LOCOMOTION_FREQUENCY_MIN = 0.05
_LOCOMOTION_FREQUENCY_MAX = 1.0
_LOCOMOTION_FREQUENCY_DEFAULT = 0.2
_LOCOMOTION_COLLISION_TANGENTIAL_M = 0.10
_LOCOMOTION_COLLISION_ORTHOGONAL_M = 0.40
_CONTINUOUS_PHASE_LOG_LIMIT = 40
_CUSTOM_LIFE_LOCK_UNTIL_NEXT_USER_TURN = "until_next_user_turn"
_CUSTOM_LIFE_LOCK_UNTIL_STOP = "until_stop"
_CUSTOM_LIFE_LOCK_MODES = {
    _CUSTOM_LIFE_LOCK_UNTIL_NEXT_USER_TURN,
    _CUSTOM_LIFE_LOCK_UNTIL_STOP,
}

_SHUTDOWN_HOOK_LOCK = threading.Lock()
_SHUTDOWN_HOOK_REGISTERED = False
_LATEST_SHUTDOWN_HOOK: Optional[Callable[[], None]] = None

_DM_EVENT_LEVELS = ("info", "warn", "error")
_DM_EVENT_LEVEL_SET = set(_DM_EVENT_LEVELS)
_DM_EVENT_CATEGORIES = ("dialog", "cmdrec", "state", "nao", "config", "http")
_DM_EVENT_CATEGORY_SET = set(_DM_EVENT_CATEGORIES)
_DM_EVENT_SOURCES = (
    "chat_text",
    "speech_ptt",
    "speech_continuous",
    "manual_command_ui",
    "script_api",
    "system_auto",
)
_DM_EVENT_SOURCE_SET = set(_DM_EVENT_SOURCES)
_DM_UI_PREF_KEYS = {"ui_active_tab", "ui_color_scheme"}
_DM_SECRET_KEY_RE = re.compile(r"(key|token|secret|password)", re.IGNORECASE)
_SUMMARY_CAPTURE_JOIN_TIMEOUT_S = 15.0
_SUMMARY_CAPTURE_TIMEOUT_DEFAULT_S = 2.0
_SUMMARY_CAPTURE_TIMEOUT_MAX_S = 5.0
_SUMMARY_LIVE_TRANSCRIPT_MAX_ITEMS = 500
_SUMMARY_STT_QUEUE_MAX_ITEMS = 16
_SUMMARY_CAPTURE_MIN_STOP_SILENCE_MS = 1800
_SUMMARY_CAPTURE_MIN_PRE_ROLL_MS = 800
_NAO_BEHAVIOR_CACHE_TTL_S = 5.0
_SUMMARY_DEFAULT_SYSTEM_PROMPT_FALLBACK = (
    "Je bent de workshop-assistent robot in een AI-geletterdheidsworkshop. "
    "De transcriptie bevat antwoorden op de vraag: "
    "'Wat zou je vandaag willen leren over AI en/of robotica?'. "
    "Jouw output wordt hardop uitgesproken via TTS. "
    "Maak daarom een korte, natuurlijk klinkende gesproken samenvatting zonder bullets, kopjes of markdown, "
    "met terugkerende leerwensen, eventuele zorgen, en een korte brug naar de volgende workshopstap. "
    "Gebruik alleen informatie uit de transcriptie."
)


def _new_request_id() -> str:
    return uuid.uuid4().hex


def _normalize_event_source(raw: Any) -> Optional[str]:
    value = str(raw or "").strip().lower()
    if value in _DM_EVENT_SOURCE_SET:
        return value
    return None


def _clip_value(value: Any, max_str_len: int = 240, max_items: int = 40, _depth: int = 0) -> Any:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        text = value.strip()
        if len(text) <= max_str_len:
            return text
        return text[: max(0, max_str_len - 3)] + "..."
    if _depth >= 4:
        return _clip_value(str(value), max_str_len=max_str_len, max_items=max_items, _depth=_depth + 1)
    if isinstance(value, dict):
        out: Dict[str, Any] = {}
        keys = list(value.keys())[:max_items]
        for key in keys:
            out[str(key)] = _clip_value(value.get(key), max_str_len=max_str_len, max_items=max_items, _depth=_depth + 1)
        if len(value) > max_items:
            out["_truncated_keys"] = len(value) - max_items
        return out
    if isinstance(value, (list, tuple)):
        out_list = [
            _clip_value(item, max_str_len=max_str_len, max_items=max_items, _depth=_depth + 1)
            for item in list(value)[:max_items]
        ]
        if len(value) > max_items:
            out_list.append({"_truncated_items": len(value) - max_items})
        return out_list
    return _clip_value(str(value), max_str_len=max_str_len, max_items=max_items, _depth=_depth + 1)


def _redact_if_needed(key: str, value: Any) -> Any:
    key_norm = str(key or "").strip().lower()
    if key_norm in {"changed_keys"}:
        return value
    if _DM_SECRET_KEY_RE.search(key_norm):
        return "***redacted***"
    return value


def _sanitize_event_value(value: Any, *, key: str = "") -> Any:
    value = _redact_if_needed(key, value)
    if isinstance(value, dict):
        out: Dict[str, Any] = {}
        for child_key, child_value in value.items():
            child_name = str(child_key)
            out[child_name] = _sanitize_event_value(child_value, key=child_name)
        return _clip_value(out)
    if isinstance(value, list):
        return _clip_value([_sanitize_event_value(item, key=key) for item in value])
    return _clip_value(value)


def _diff_runtime_cfg(before: JsonLike, after: JsonLike) -> Dict[str, Any]:
    before_obj = before if isinstance(before, dict) else {}
    after_obj = after if isinstance(after, dict) else {}
    changed_keys: List[str] = []
    changes: Dict[str, Dict[str, Any]] = {}
    all_keys = sorted(set(before_obj.keys()) | set(after_obj.keys()))
    for key in all_keys:
        old_val = before_obj.get(key)
        new_val = after_obj.get(key)
        if old_val == new_val:
            continue
        changed_keys.append(key)
        changes[key] = {
            "old": _sanitize_event_value(old_val, key=key),
            "new": _sanitize_event_value(new_val, key=key),
        }
    return {"changed_keys": changed_keys, "changes": changes}


def _run_latest_shutdown_hook() -> None:
    hook: Optional[Callable[[], None]]
    with _SHUTDOWN_HOOK_LOCK:
        hook = _LATEST_SHUTDOWN_HOOK
    if callable(hook):
        try:
            hook()
        except Exception:
            pass


def _new_continuous_state(now_ts: Optional[float] = None) -> Dict[str, Any]:
    now = float(time.time() if now_ts is None else now_ts)
    return {
        "running": False,
        "stop": None,
        "thread": None,
        "last_error": None,
        "phase": "idle",
        "phase_reason": "init",
        "phase_changed_at": now,
        "phase_seq": 0,
        "phase_log": [{"seq": 0, "phase": "idle", "reason": "init", "ts": now}],
        "wake_open_until": 0.0,
        "wake_open_once": False,
        "wake_stt": None,
        "wake_stt_key": None,
        "last_wake_text": "",
        "eye_phase": None,
        "eye_last_try": 0.0,
    }


def _ensure_continuous_phase_meta(state: Dict[str, Any], now_ts: Optional[float] = None) -> None:
    now = float(time.time() if now_ts is None else now_ts)
    phase = str(state.get("phase") or "idle").strip().lower() or "idle"
    reason = str(state.get("phase_reason") or "init").strip() or "init"
    try:
        seq = int(state.get("phase_seq", 0))
    except Exception:
        seq = 0
    changed_at_raw = state.get("phase_changed_at")
    try:
        changed_at = float(changed_at_raw)
    except Exception:
        changed_at = now

    log = state.get("phase_log")
    if not isinstance(log, list):
        log = []
    if not log:
        log = [{"seq": seq, "phase": phase, "reason": reason, "ts": changed_at}]
    if len(log) > _CONTINUOUS_PHASE_LOG_LIMIT:
        del log[: len(log) - _CONTINUOUS_PHASE_LOG_LIMIT]

    state["phase"] = phase
    state["phase_reason"] = reason
    state["phase_seq"] = seq
    state["phase_changed_at"] = changed_at
    state["phase_log"] = log


def _transition_continuous_phase(
    state: Dict[str, Any],
    phase: str,
    *,
    reason: str,
    now_ts: Optional[float] = None,
) -> bool:
    _ensure_continuous_phase_meta(state, now_ts=now_ts)
    now = float(time.time() if now_ts is None else now_ts)
    next_phase = str(phase or "").strip().lower() or "idle"
    next_reason = str(reason or "").strip() or "unspecified"
    prev_phase = str(state.get("phase") or "idle").strip().lower() or "idle"
    changed = next_phase != prev_phase

    state["phase"] = next_phase
    state["phase_reason"] = next_reason

    if not changed:
        return False

    seq = int(state.get("phase_seq", 0)) + 1
    state["phase_seq"] = seq
    state["phase_changed_at"] = now
    log = state.get("phase_log")
    if not isinstance(log, list):
        log = []
    log.append({"seq": seq, "phase": next_phase, "reason": next_reason, "ts": now})
    if len(log) > _CONTINUOUS_PHASE_LOG_LIMIT:
        del log[: len(log) - _CONTINUOUS_PHASE_LOG_LIMIT]
    state["phase_log"] = log
    return True


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


def _clean_listen_mode(raw: Any) -> str:
    value = str(raw or "").strip().lower()
    if value in _LISTEN_MODE_VALUES:
        return value
    return "ptt"


def _clean_confirm_policy(raw: Any) -> str:
    value = str(raw or "").strip().lower()
    if value in _CONFIRM_POLICY_VALUES:
        return value
    return "when_guarded"


def _clean_ui_active_tab(raw: Any) -> str:
    value = str(raw or "").strip().lower()
    if value in _UI_ACTIVE_TAB_VALUES:
        return value
    return "prompt"


def _clean_robot_name(raw: Any) -> str:
    value = str(raw or "").strip()
    if not value:
        return ""
    return value[:60]


def _clean_ui_color_scheme(raw: Any) -> str:
    value = str(raw or "").strip().lower()
    if value == "amber":
        return "gold"
    if value in {"brown_dark", "brown_deep"}:
        return "brown"
    if value in {"dark", "night"}:
        return "night_blue"
    if value in _UI_COLOR_SCHEME_VALUES:
        return value
    return "default"


def _clean_llm_temperature(raw: Any) -> Optional[float]:
    if raw is None:
        return None
    if isinstance(raw, str):
        text = raw.strip().lower()
        if not text or text == "null":
            return None
        raw = text
    try:
        value = float(raw)
    except Exception:
        return None
    if math.isnan(value) or math.isinf(value):
        return None
    if value < 0.0 or value > 2.0:
        return None
    return float(value)


def _clean_llm_top_p(raw: Any) -> Optional[float]:
    if raw is None:
        return None
    if isinstance(raw, str):
        text = raw.strip().lower()
        if not text or text == "null":
            return None
        raw = text
    try:
        value = float(raw)
    except Exception:
        return None
    if math.isnan(value) or math.isinf(value):
        return None
    if value <= 0.0 or value > 1.0:
        return None
    return float(value)


def _clean_llm_top_k(raw: Any) -> Optional[int]:
    if raw is None:
        return None
    if isinstance(raw, str):
        text = raw.strip().lower()
        if not text or text == "null":
            return None
        raw = text
    try:
        value = int(raw)
    except Exception:
        return None
    if value < 1:
        return None
    return int(value)


def _clean_locomotion_frequency(raw: Any, *, default: float = _LOCOMOTION_FREQUENCY_DEFAULT) -> float:
    try:
        value = float(raw)
    except Exception:
        value = float(default)
    if math.isnan(value) or math.isinf(value):
        value = float(default)
    value = max(_LOCOMOTION_FREQUENCY_MIN, min(_LOCOMOTION_FREQUENCY_MAX, value))
    return float(value)


def _continuous_capture_timeout_s(
    start_timeout_s: float,
    *,
    wake_mode: str,
    gate_open: bool,
    wake_open_until: float,
    now_s: Optional[float] = None,
    probe_max_s: float = 0.8,
) -> float:
    base = float(start_timeout_s)
    if wake_mode != "timeout" or not gate_open:
        return base

    now = float(time.time() if now_s is None else now_s)
    try:
        wake_until = float(wake_open_until or 0.0)
    except Exception:
        wake_until = 0.0

    if wake_until <= now:
        # Gate is effectively closed; re-check soon.
        return min(base, max(0.25, float(probe_max_s)))

    remaining = wake_until - now
    probe = max(0.25, min(float(probe_max_s), remaining))
    return min(base, probe)


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


def _url_port(raw_url: Optional[str], default_port: int) -> int:
    normalized = _normalize_url(raw_url)
    if not normalized:
        return int(default_port)
    try:
        parsed = urlparse(normalized)
        port = parsed.port
    except Exception:
        return int(default_port)
    if isinstance(port, int) and port > 0:
        return int(port)
    return int(default_port)


def _url_host_port(raw_url: Optional[str]) -> Tuple[Optional[str], Optional[int]]:
    normalized = _normalize_url(raw_url)
    if not normalized:
        return None, None
    try:
        parsed = urlparse(normalized)
    except Exception:
        return None, None
    host = (parsed.hostname or "").strip().lower() or None
    if parsed.port:
        return host, int(parsed.port)
    scheme = (parsed.scheme or "http").lower()
    return host, (443 if scheme == "https" else 80)


def _host_header_host_port(raw_host: Optional[str]) -> Tuple[Optional[str], Optional[int]]:
    value = str(raw_host or "").strip()
    if not value:
        return None, None
    try:
        parsed = urlparse("http://" + value)
    except Exception:
        return None, None
    host = (parsed.hostname or "").strip().lower() or None
    if parsed.port:
        return host, int(parsed.port)
    return host, 80


def _local_host_aliases() -> set:
    aliases = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}
    names = [socket.gethostname(), socket.getfqdn()]
    for name in names:
        clean = str(name or "").strip().lower()
        if not clean:
            continue
        aliases.add(clean)
        try:
            for addr in socket.gethostbyname_ex(clean)[2]:
                addr_clean = str(addr or "").strip().lower()
                if addr_clean:
                    aliases.add(addr_clean)
        except Exception:
            pass
    return aliases


def _url_conflicts_with_webapp(raw_url: Optional[str], request_host_header: Optional[str]) -> bool:
    target_host, target_port = _url_host_port(raw_url)
    web_host, web_port = _host_header_host_port(request_host_header)
    if not target_host or target_port is None or web_port is None:
        return False
    if int(target_port) != int(web_port):
        return False
    if web_host and target_host == web_host:
        return True
    local_hosts = _local_host_aliases()
    return bool(web_host and target_host in local_hosts and web_host in local_hosts)


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
    llm_options = llm_params.get("options", {}) if isinstance(llm_params.get("options"), dict) else {}
    master_prompt_file = llm_params.get("system_prompt_file", None)
    llm_temperature = llm_options.get("temperature", llm_params.get("temperature"))
    llm_top_p = llm_options.get("top_p", llm_params.get("top_p"))
    llm_top_k = llm_options.get("top_k", llm_params.get("top_k"))

    output_cfg = (cfg_src.get("output", {}) or {})
    output_type = (output_cfg.get("type") or "none").lower()
    output_params = output_cfg.get("params", {}) if isinstance(output_cfg.get("params"), dict) else {}
    output_target = output_cfg.get("target") if output_cfg.get("target") is not None else output_params.get("target")
    if not output_target:
        if output_type == "none":
            output_target = "none"
        else:
            output_target = "nao" if output_type in ("nao_tts", "nao_py2", "nao") else "server"
    tts_engine = output_cfg.get("engine") if output_cfg.get("engine") is not None else output_params.get("tts_engine")
    if not tts_engine:
        if output_type == "none":
            tts_engine = "none"
        else:
            tts_engine = "nao_native" if output_type in ("nao_tts", "nao_py2", "nao") else "piper"

    input_device = mic_params.get("input_device", None)
    if mic_cfg.get("type") == "nao_ssh":
        input_device = "nao_ssh"

    return {
        "confirm_policy": _clean_confirm_policy(cfg_src.get("confirm_policy")),
        "robot_name": _clean_robot_name(cfg_src.get("robot_name") or cfg_src.get("agent_name") or ""),
        "nao_base_url": (fallback.get("base_url") or "").rstrip("/"),
        "behavior_manager_url": behavior_manager_url,
        "nao_ip": nao_ip or "",
        "nao_ip_enabled": bool(cfg_src.get("nao_ip_enabled")) if "nao_ip_enabled" in cfg_src else bool(nao_ip),
        "base_enabled": bool(cfg_src.get("base_enabled", True)),
        "behavior_enabled": bool(cfg_src.get("behavior_enabled", True)),
        "base_autostart": bool(cfg_src.get("base_autostart", False)),
        "behavior_autostart": bool(cfg_src.get("behavior_autostart", False)),
        "input_device": input_device,
        "stt_type": (stt_cfg.get("type") or "vosk"),
        "mic_params_ptt": mic_ptt,
        "mic_params_continuous": mic_cont,
        "azure_initial_silence_ms": stt_params.get("initial_silence_ms"),
        "azure_end_silence_ms": stt_params.get("end_silence_ms"),
        "wake_mode": _clean_wake_mode(input_params.get("wake_mode", "never")),
        "wake_timeout_s": _clean_wake_timeout_s(input_params.get("wake_timeout_s", 20), default=20),
        "wake_words": _clean_wake_words(input_params.get("wake_words")),
        "listen_mode": _clean_listen_mode(cfg_src.get("listen_mode", "ptt")),
        "ui_active_tab": _clean_ui_active_tab(cfg_src.get("ui_active_tab", "prompt")),
        "ui_color_scheme": _clean_ui_color_scheme(cfg_src.get("ui_color_scheme", "default")),
        "locomotion_frequency": _clean_locomotion_frequency(
            cfg_src.get("locomotion_frequency", _LOCOMOTION_FREQUENCY_DEFAULT),
            default=_LOCOMOTION_FREQUENCY_DEFAULT,
        ),
        "locomotion_arms_enabled": bool(cfg_src.get("locomotion_arms_enabled", True)),
        "cmdrec": cfg_src.get("cmdrec", "latest"),
        "ptt_use_vad": bool(cfg_src.get("ptt_use_vad", False)),
        "llm_type": llm_cfg.get("type", "echo"),
        "llm_model": llm_params.get("model", ""),
        "llm_temperature": _clean_llm_temperature(llm_temperature),
        "llm_top_p": _clean_llm_top_p(llm_top_p),
        "llm_top_k": _clean_llm_top_k(llm_top_k),
        "master_prompt_file": master_prompt_file,
        "master_prompt_text": llm_params.get("system_prompt", None),
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
    cfg_out["robot_name"] = _clean_robot_name(runtime_cfg.get("robot_name") or cfg_out.get("robot_name") or "")
    cfg_out["ui_color_scheme"] = _clean_ui_color_scheme(runtime_cfg.get("ui_color_scheme", cfg_out.get("ui_color_scheme", "default")))
    cfg_out["confirm_policy"] = _clean_confirm_policy(runtime_cfg.get("confirm_policy", cfg_out.get("confirm_policy", "when_guarded")))

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
    llm_options = llm_params.get("options", {}) if isinstance(llm_params.get("options"), dict) else {}
    if llm_type == "echo":
        cfg_out["llm"] = {"type": "echo"}
    else:
        llm_cfg["type"] = llm_type
        model_value = runtime_cfg.get("llm_model")
        if model_value:
            llm_params["model"] = model_value
        mp_text_present = "master_prompt_text" in runtime_cfg
        mp_text = runtime_cfg.get("master_prompt_text", None)
        if mp_text_present:
            text_value = str(mp_text or "")
            if text_value:
                llm_params["system_prompt"] = text_value
                llm_params.pop("system_prompt_file", None)
            else:
                llm_params.pop("system_prompt", None)
        mp_file = runtime_cfg.get("master_prompt_file", None)
        if mp_file and not llm_params.get("system_prompt"):
            llm_params["system_prompt_file"] = mp_file
        if "llm_temperature" in runtime_cfg:
            llm_temperature = _clean_llm_temperature(runtime_cfg.get("llm_temperature"))
        else:
            llm_temperature = _clean_llm_temperature(llm_options.get("temperature", llm_params.get("temperature")))
        if "llm_top_p" in runtime_cfg:
            llm_top_p = _clean_llm_top_p(runtime_cfg.get("llm_top_p"))
        else:
            llm_top_p = _clean_llm_top_p(llm_options.get("top_p", llm_params.get("top_p")))
        if "llm_top_k" in runtime_cfg:
            llm_top_k = _clean_llm_top_k(runtime_cfg.get("llm_top_k"))
        else:
            llm_top_k = _clean_llm_top_k(llm_options.get("top_k", llm_params.get("top_k")))
        llm_params.pop("temperature", None)
        llm_params.pop("top_p", None)
        llm_params.pop("top_k", None)
        if llm_temperature is not None:
            llm_options["temperature"] = llm_temperature
        else:
            llm_options.pop("temperature", None)
        if llm_top_p is not None:
            llm_options["top_p"] = llm_top_p
        else:
            llm_options.pop("top_p", None)
        if llm_top_k is not None:
            llm_options["top_k"] = llm_top_k
        else:
            llm_options.pop("top_k", None)
        if llm_options:
            llm_params["options"] = llm_options
        else:
            llm_params.pop("options", None)
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


def _summary_capture_mic_config(cfg: JsonLike) -> JsonLike:
    """
    Maak summary-capture iets minder agressief met segmentatie:
    - langere stilte nodig om utterance te sluiten
    - ruimere pre-roll voor eerste woorden
    """
    out = copy.deepcopy(cfg)
    input_cfg = out.setdefault("input", {}) or {}
    if not isinstance(input_cfg, dict):
        input_cfg = {}
        out["input"] = input_cfg
    mic_cfg = input_cfg.setdefault("mic", {}) or {}
    if not isinstance(mic_cfg, dict):
        mic_cfg = {}
        input_cfg["mic"] = mic_cfg

    base_params = mic_cfg.get("params")
    if not isinstance(base_params, dict):
        base_params = {}
    cont_params = mic_cfg.get("params_continuous")
    if not isinstance(cont_params, dict):
        cont_params = {}

    base_params = dict(base_params)
    cont_params = dict(cont_params)

    def _int_or_none(value: Any) -> Optional[int]:
        try:
            return int(value)
        except Exception:
            return None

    stop_silence_ms = _int_or_none(cont_params.get("stop_silence_ms"))
    if stop_silence_ms is None:
        stop_silence_ms = _int_or_none(base_params.get("stop_silence_ms"))
    if stop_silence_ms is None or stop_silence_ms < _SUMMARY_CAPTURE_MIN_STOP_SILENCE_MS:
        cont_params["stop_silence_ms"] = _SUMMARY_CAPTURE_MIN_STOP_SILENCE_MS

    pre_roll_ms = _int_or_none(cont_params.get("pre_roll_ms"))
    if pre_roll_ms is None:
        pre_roll_ms = _int_or_none(base_params.get("pre_roll_ms"))
    if pre_roll_ms is None or pre_roll_ms < _SUMMARY_CAPTURE_MIN_PRE_ROLL_MS:
        cont_params["pre_roll_ms"] = _SUMMARY_CAPTURE_MIN_PRE_ROLL_MS

    mic_cfg["params"] = base_params
    mic_cfg["params_continuous"] = cont_params
    return out


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


def create_app(
    *,
    cfg: JsonLike,
    config_path: str,
    instance_id: Optional[str] = None,
    runtime_state_dir: Optional[str] = None,
) -> Tuple[Flask, Any, InputLLMOutputPipeline]:
    app = Flask(__name__, static_folder="web", static_url_path="")

    base_cfg = copy.deepcopy(cfg)
    run_cfg = base_cfg.setdefault("run", {})
    run_cfg["web_ui_enabled"] = True
    resolved_instance_id = str(instance_id or run_cfg.get("instance_id") or "").strip()
    if not resolved_instance_id:
        resolved_instance_id = "default"
    base_config_path = config_path
    base_pipeline = build_pipeline_from_config(cfg, config_path=config_path)
    base_stt = make_stt_backend_from_config(cfg)
    pipeline_lock = threading.Lock()
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    runtime_cfg_lock = threading.RLock()
    runtime_cfg_default = _extract_runtime_config(base_cfg)

    def _runtime_state_key(value: str) -> str:
        key = re.sub(r"[^a-zA-Z0-9_.-]+", "_", str(value or "").strip())
        return key or "default"

    if runtime_state_dir:
        runtime_state_base_dir = str(runtime_state_dir)
    else:
        runtime_state_base_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "configs", "runtime")
    runtime_state_enabled = config_path != "<memory>"
    runtime_state_path = (
        os.path.join(runtime_state_base_dir, f"runtime_{_runtime_state_key(resolved_instance_id)}.json")
        if runtime_state_enabled
        else None
    )
    runtime_cfg_global: JsonLike = copy.deepcopy(runtime_cfg_default)

    def _sanitize_runtime_cfg_inplace(cfg_obj: JsonLike) -> JsonLike:
        cfg_obj.pop("cmdrec_stop_rule_mode", None)
        cfg_obj["listen_mode"] = _clean_listen_mode(cfg_obj.get("listen_mode", "ptt"))
        cfg_obj["confirm_policy"] = _clean_confirm_policy(cfg_obj.get("confirm_policy", "when_guarded"))
        cfg_obj["ui_active_tab"] = _clean_ui_active_tab(cfg_obj.get("ui_active_tab", "prompt"))
        cfg_obj["ui_color_scheme"] = _clean_ui_color_scheme(cfg_obj.get("ui_color_scheme", "default"))
        cfg_obj["robot_name"] = _clean_robot_name(cfg_obj.get("robot_name", ""))
        cfg_obj["llm_temperature"] = _clean_llm_temperature(cfg_obj.get("llm_temperature"))
        cfg_obj["llm_top_p"] = _clean_llm_top_p(cfg_obj.get("llm_top_p"))
        cfg_obj["llm_top_k"] = _clean_llm_top_k(cfg_obj.get("llm_top_k"))
        cfg_obj["locomotion_frequency"] = _clean_locomotion_frequency(
            cfg_obj.get("locomotion_frequency", _LOCOMOTION_FREQUENCY_DEFAULT),
            default=_LOCOMOTION_FREQUENCY_DEFAULT,
        )
        cfg_obj["locomotion_arms_enabled"] = bool(cfg_obj.get("locomotion_arms_enabled", True))
        return cfg_obj

    def _runtime_cfg_snapshot() -> JsonLike:
        with runtime_cfg_lock:
            return copy.deepcopy(runtime_cfg_global)

    def _save_runtime_cfg_state() -> None:
        if not runtime_state_enabled or not runtime_state_path:
            return
        try:
            os.makedirs(os.path.dirname(runtime_state_path), exist_ok=True)
            with open(runtime_state_path, "w", encoding="utf-8") as fh:
                json.dump(_runtime_cfg_snapshot(), fh, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _load_runtime_cfg_state() -> None:
        if not runtime_state_enabled or not runtime_state_path or not os.path.isfile(runtime_state_path):
            return
        try:
            with open(runtime_state_path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
            if not isinstance(raw, dict):
                return
            with runtime_cfg_lock:
                runtime_cfg_global.clear()
                runtime_cfg_global.update(copy.deepcopy(runtime_cfg_default))
                runtime_cfg_global.update(raw)
                _sanitize_runtime_cfg_inplace(runtime_cfg_global)
        except Exception:
            pass

    _sanitize_runtime_cfg_inplace(runtime_cfg_global)
    _load_runtime_cfg_state()
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
    proc_log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", "process")
    proc_log_paths: Dict[str, str] = {
        "base": os.path.join(proc_log_dir, "base.log"),
        "behavior": os.path.join(proc_log_dir, "behavior.log"),
    }
    proc_log_backup_suffix = ".1"
    proc_log_max_bytes = 5 * 1024 * 1024
    dm_event_lock = threading.Lock()
    dm_events: List[Dict[str, Any]] = []
    dm_event_tail_limit = 4000
    dm_event_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", "dm_events")
    dm_event_path = os.path.join(dm_event_dir, f"dm_events_{_runtime_state_key(resolved_instance_id)}.jsonl")
    dm_event_backup_suffix = ".1"
    dm_event_max_bytes = 5 * 1024 * 1024
    dm_event_file_lock = threading.Lock()
    dm_event_pending_writes: List[Dict[str, Any]] = []
    dm_event_pending_max = 8000
    dm_event_flush_batch_size = 250
    dm_event_flush_interval_s = 0.75
    dm_event_flush_wakeup = threading.Event()
    dm_event_writer_stop = threading.Event()
    dm_event_writer_thread: Optional[threading.Thread] = None
    dm_event_dropped_writes = 0

    # In-memory session histories for intranet testing
    sessions: Dict[str, History] = {}
    history_version_state: Dict[str, Dict[str, Any]] = {}
    pending_confirms: Dict[str, Dict[str, Any]] = {}
    cmdrec_state: Dict[str, Dict[str, Any]] = {}
    runtime_context_state: Dict[str, Dict[str, Any]] = {}
    behavior_catalog_cache_lock = threading.Lock()
    behavior_catalog_cache: Dict[str, Dict[str, Any]] = {}
    pipeline_by_sid: Dict[str, InputLLMOutputPipeline] = {}
    stt_by_sid: Dict[str, Any] = {}
    continuous_state: Dict[str, Dict[str, Any]] = {}
    ptt_state: Dict[str, Dict[str, Any]] = {}
    summary_state_by_sid: Dict[str, Dict[str, Any]] = {}
    summary_state_lock = threading.RLock()
    last_activity = {"ts": time.time()}
    activity_lock = threading.Lock()

    confirm_method = parse_confirm_method(cfg.get("confirm_method", "web"))
    confirm_timeout_s = float(cfg.get("confirm_timeout_s", 10.0))

    def _get_sid() -> str:
        sid = request.cookies.get("sid")
        if not sid:
            sid = secrets.token_urlsafe(16)
        return sid

    def _request_ui_mode() -> str:
        mode = str(request.cookies.get("ui_mode") or "").strip().lower()
        if mode in ("server", "client"):
            return mode
        return "unknown"

    def _get_request_id() -> str:
        if has_request_context():
            current = getattr(g, "dm_request_id", None)
            if current:
                return str(current)
        return _new_request_id()

    def _get_request_started_at() -> float:
        if has_request_context():
            started = getattr(g, "dm_request_started_at", None)
            try:
                if started is not None:
                    return float(started)
            except Exception:
                pass
        return time.time()

    def _infer_event_source(
        *,
        endpoint: Optional[str] = None,
        explicit_source: Optional[Any] = None,
        stt_used: Optional[bool] = None,
        forced_source: Optional[str] = None,
    ) -> str:
        forced = _normalize_event_source(forced_source)
        if forced:
            return forced
        explicit = _normalize_event_source(explicit_source)
        if explicit:
            return explicit
        ep = str(endpoint or "").strip().lower()
        if ep == "/api/send":
            return "speech_ptt" if bool(stt_used) else "chat_text"
        if ep in (
            "/api/command_execute",
            "/api/nao_behavior_start",
            "/api/nao_behavior_stop",
            "/api/nao_move_toward",
            "/api/nao_stop_move",
            "/api/nao_stop_audio",
            "/api/nao_wake_up",
            "/api/nao_rest",
            "/api/nao_custom_life_set",
            "/api/runtime_config",
            "/api/process_start",
        ):
            return "manual_command_ui"
        if ep in ("/api/script/do", "/api/script/say"):
            return "script_api"
        return "system_auto"

    def _dm_now_iso() -> str:
        now = time.time()
        whole = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now))
        ms = int((now % 1.0) * 1000.0)
        return f"{whole}.{ms:03d}Z"

    def _ensure_dm_event_dir() -> None:
        try:
            os.makedirs(dm_event_dir, exist_ok=True)
        except OSError:
            pass

    def _rotate_dm_event_file_if_needed(path: str) -> None:
        if not path:
            return
        try:
            if not os.path.exists(path):
                return
            if os.path.getsize(path) < dm_event_max_bytes:
                return
            backup = path + dm_event_backup_suffix
            if os.path.exists(backup):
                os.remove(backup)
            os.replace(path, backup)
        except OSError:
            pass

    def _append_dm_event_file_batch(entries: List[Dict[str, Any]]) -> None:
        if not entries:
            return
        _ensure_dm_event_dir()
        with dm_event_file_lock:
            _rotate_dm_event_file_if_needed(dm_event_path)
            try:
                with open(dm_event_path, "a", encoding="utf-8") as fh:
                    for entry in entries:
                        fh.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")
            except OSError:
                pass

    def _load_dm_event_tail(limit: int) -> List[Dict[str, Any]]:
        entries: List[Dict[str, Any]] = []
        for candidate in (dm_event_path + dm_event_backup_suffix, dm_event_path):
            if not os.path.exists(candidate):
                continue
            try:
                with open(candidate, "r", encoding="utf-8", errors="replace") as fh:
                    for raw_line in fh:
                        line = raw_line.strip()
                        if not line:
                            continue
                        try:
                            item = json.loads(line)
                        except Exception:
                            continue
                        if isinstance(item, dict):
                            entries.append(item)
            except OSError:
                continue
        if limit <= 0:
            return entries
        return entries[-limit:]

    def _clear_dm_event_files() -> None:
        with dm_event_file_lock:
            for candidate in (dm_event_path, dm_event_path + dm_event_backup_suffix):
                try:
                    if os.path.exists(candidate):
                        os.remove(candidate)
                except OSError:
                    pass

    def _drain_dm_event_writes(*, force: bool = False) -> int:
        written = 0
        while True:
            with dm_event_lock:
                if not dm_event_pending_writes:
                    batch: List[Dict[str, Any]] = []
                elif force:
                    batch = list(dm_event_pending_writes)
                    dm_event_pending_writes.clear()
                else:
                    batch = list(dm_event_pending_writes[:dm_event_flush_batch_size])
                    del dm_event_pending_writes[: len(batch)]
            if not batch:
                break
            _append_dm_event_file_batch(batch)
            written += len(batch)
            if not force:
                break
        return written

    def _dm_event_writer_loop() -> None:
        while not dm_event_writer_stop.is_set():
            dm_event_flush_wakeup.wait(timeout=dm_event_flush_interval_s)
            dm_event_flush_wakeup.clear()
            _drain_dm_event_writes(force=False)
        _drain_dm_event_writes(force=True)

    def _stop_dm_event_writer() -> None:
        if dm_event_writer_thread is None:
            return
        dm_event_writer_stop.set()
        dm_event_flush_wakeup.set()
        try:
            dm_event_writer_thread.join(timeout=2.0)
        except Exception:
            pass

    def _log_dm_event(
        *,
        sid: str,
        level: str,
        category: str,
        event: str,
        source: str,
        message: str,
        data: Optional[Dict[str, Any]] = None,
        turn_id: Optional[str] = None,
        request_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        nonlocal dm_event_dropped_writes
        level_norm = str(level or "").strip().lower()
        category_norm = str(category or "").strip().lower()
        source_norm = _infer_event_source(forced_source=source)
        if level_norm not in _DM_EVENT_LEVEL_SET:
            level_norm = "info"
        if category_norm not in _DM_EVENT_CATEGORY_SET:
            category_norm = "dialog"
        payload = {
            "ts": _dm_now_iso(),
            "instance_id": resolved_instance_id,
            "sid": sid,
            "level": level_norm,
            "category": category_norm,
            "event": str(event or "").strip() or "event",
            "source": source_norm,
            "message": _clip_value(message or "", max_str_len=320),
            "data": _sanitize_event_value(data or {}),
        }
        if turn_id:
            payload["turn_id"] = str(turn_id)
        if request_id:
            payload["request_id"] = str(request_id)
        wake_writer = False
        with dm_event_lock:
            dm_events.append(payload)
            if len(dm_events) > dm_event_tail_limit:
                del dm_events[: len(dm_events) - dm_event_tail_limit]
            if len(dm_event_pending_writes) >= dm_event_pending_max:
                drop_count = len(dm_event_pending_writes) - dm_event_pending_max + 1
                del dm_event_pending_writes[:drop_count]
                dm_event_dropped_writes += drop_count
            dm_event_pending_writes.append(payload)
            wake_writer = len(dm_event_pending_writes) >= dm_event_flush_batch_size
        if wake_writer:
            dm_event_flush_wakeup.set()
        return payload

    def _runtime_cfg_change_scope(changed_keys: List[str]) -> str:
        if changed_keys and all(key in _DM_UI_PREF_KEYS for key in changed_keys):
            return "ui_pref_only"
        return "runtime"

    def _log_runtime_cfg_change(
        *,
        sid: str,
        before: JsonLike,
        after: JsonLike,
        source: str,
        request_id: Optional[str],
        reason: str,
    ) -> bool:
        diff = _diff_runtime_cfg(before, after)
        changed_keys = list(diff.get("changed_keys") or [])
        if not changed_keys:
            return False
        _log_dm_event(
            sid=sid,
            level="info",
            category="config",
            event="runtime_config_changed",
            source=source,
            message=f"Runtime config updated ({len(changed_keys)} key(s)).",
            request_id=request_id,
            data={
                "changed_keys": changed_keys,
                "changes": diff.get("changes") or {},
                "change_scope": _runtime_cfg_change_scope(changed_keys),
                "reason": str(reason or "runtime_config_update"),
            },
        )
        return True

    def _structured_http_error_data(
        *,
        endpoint: str,
        status: int,
        message: str,
        request_id: str,
        started_at: float,
        error_type: str,
    ) -> Dict[str, Any]:
        latency_ms = max(0, int(round((time.time() - started_at) * 1000.0)))
        return {
            "type": str(error_type or "http_error"),
            "message": _clip_value(message or "", max_str_len=260),
            "endpoint": endpoint,
            "status": int(status),
            "latency_ms": latency_ms,
            "correlation": request_id,
        }

    def _get_history(sid: str) -> History:
        return sessions.setdefault(sid, [])

    def _history_signature(history: History) -> str:
        try:
            packed = json.dumps(history or [], ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        except Exception:
            packed = repr(history or [])
        return hashlib.sha1(packed.encode("utf-8", errors="replace")).hexdigest()

    _EMPTY_HISTORY_SIGNATURE = _history_signature([])

    def _history_version_for_sid(sid: str, history: History) -> int:
        meta = history_version_state.setdefault(
            sid,
            {"version": 0, "signature": _EMPTY_HISTORY_SIGNATURE},
        )
        signature = _history_signature(history)
        previous_signature = str(meta.get("signature") or "")
        if signature != previous_signature:
            meta["signature"] = signature
            try:
                current_version = int(meta.get("version", 0))
            except Exception:
                current_version = 0
            meta["version"] = current_version + 1
        try:
            return int(meta.get("version", 0))
        except Exception:
            meta["version"] = 0
            return 0

    def _get_cmdrec_state(sid: str) -> Dict[str, Any]:
        return cmdrec_state.setdefault(
            sid,
            {"mode": "DIALOG", "active_behavior": None, "pending_dance": None},
        )

    def _get_runtime_state(sid: str) -> Dict[str, Any]:
        state = runtime_context_state.setdefault(
            sid,
            {
                "last_action": None,
                "custom_life_lock_mode": None,
                "custom_life_paused": False,
                "custom_life_pause_state": None,
                "custom_life_pause_base_url": None,
                "custom_life_pause_mode": None,
                "command_stop_available": False,
                "command_stop_label": None,
                "command_stop_scope": None,
                "motion_last_seq": None,
            },
        )
        if "custom_life_lock_mode" not in state:
            state["custom_life_lock_mode"] = None
        if "custom_life_paused" not in state:
            state["custom_life_paused"] = False
        if "custom_life_pause_state" not in state:
            state["custom_life_pause_state"] = None
        if "custom_life_pause_base_url" not in state:
            state["custom_life_pause_base_url"] = None
        if "custom_life_pause_mode" not in state:
            state["custom_life_pause_mode"] = None
        if "command_stop_available" not in state:
            state["command_stop_available"] = False
        if "command_stop_label" not in state:
            state["command_stop_label"] = None
        if "command_stop_scope" not in state:
            state["command_stop_scope"] = None
        if "motion_last_seq" not in state:
            state["motion_last_seq"] = None
        return state

    def _get_continuous_state(sid: str) -> Dict[str, Any]:
        state = continuous_state.setdefault(sid, _new_continuous_state())
        _ensure_continuous_phase_meta(state)
        return state

    def _get_ptt_state(sid: str) -> Dict[str, Any]:
        return ptt_state.setdefault(
            sid,
            {"running": False, "stop": None, "thread": None, "last_error": None, "audio": None, "vad_cfg": None},
        )

    def _new_summary_state() -> Dict[str, Any]:
        return {
            "phase": "idle",
            "capture_thread": None,
            "stt_thread": None,
            "stop_event": None,
            "audio_queue": None,
            "transcripts": [],
            "draft_text": None,
            "last_prompt_meta": None,
            "last_error": None,
            "capture_stats": {
                "loops": 0,
                "timeouts": 0,
                "audio_chunks": 0,
                "dropped_audio_chunks": 0,
                "stt_calls": 0,
                "transcript_count": 0,
                "stt_queue_max": _SUMMARY_STT_QUEUE_MAX_ITEMS,
                "stt_queue_size": 0,
                "started_at": None,
                "last_audio_at": None,
                "last_transcript_at": None,
            },
        }

    def _get_summary_state(sid: str) -> Dict[str, Any]:
        with summary_state_lock:
            state = summary_state_by_sid.setdefault(sid, _new_summary_state())
            if "phase" not in state:
                state["phase"] = "idle"
            if "capture_thread" not in state:
                state["capture_thread"] = None
            if "stt_thread" not in state:
                state["stt_thread"] = None
            if "stop_event" not in state:
                state["stop_event"] = None
            if "audio_queue" not in state:
                state["audio_queue"] = None
            if "transcripts" not in state or not isinstance(state.get("transcripts"), list):
                state["transcripts"] = []
            if "draft_text" not in state:
                state["draft_text"] = None
            if "last_prompt_meta" not in state:
                state["last_prompt_meta"] = None
            if "last_error" not in state:
                state["last_error"] = None
            if "capture_stats" not in state or not isinstance(state.get("capture_stats"), dict):
                state["capture_stats"] = {
                    "loops": 0,
                    "timeouts": 0,
                    "audio_chunks": 0,
                    "dropped_audio_chunks": 0,
                    "stt_calls": 0,
                    "transcript_count": 0,
                    "stt_queue_max": _SUMMARY_STT_QUEUE_MAX_ITEMS,
                    "stt_queue_size": 0,
                    "started_at": None,
                    "last_audio_at": None,
                    "last_transcript_at": None,
                }
            return state

    def _summary_clear_state(state: Dict[str, Any]) -> None:
        state["phase"] = "idle"
        state["capture_thread"] = None
        state["stt_thread"] = None
        state["stop_event"] = None
        state["audio_queue"] = None
        state["transcripts"] = []
        state["draft_text"] = None
        state["last_prompt_meta"] = None
        state["last_error"] = None
        state["capture_stats"] = {
            "loops": 0,
            "timeouts": 0,
            "audio_chunks": 0,
            "dropped_audio_chunks": 0,
            "stt_calls": 0,
            "transcript_count": 0,
            "stt_queue_max": _SUMMARY_STT_QUEUE_MAX_ITEMS,
            "stt_queue_size": 0,
            "started_at": None,
            "last_audio_at": None,
            "last_transcript_at": None,
        }

    def _summary_stop_capture(state: Dict[str, Any], *, join_timeout_s: float = _SUMMARY_CAPTURE_JOIN_TIMEOUT_S) -> bool:
        with summary_state_lock:
            stop_event = state.get("stop_event")
            capture_thread = state.get("capture_thread")
            stt_thread = state.get("stt_thread")
        if stop_event is not None:
            try:
                stop_event.set()
            except Exception:
                pass
        if capture_thread is not None and hasattr(capture_thread, "is_alive"):
            try:
                if capture_thread.is_alive():
                    capture_thread.join(timeout=join_timeout_s)
            except Exception:
                pass
        if stt_thread is not None and hasattr(stt_thread, "is_alive"):
            try:
                if stt_thread.is_alive():
                    stt_thread.join(timeout=join_timeout_s)
            except Exception:
                pass
        capture_alive = bool(capture_thread is not None and hasattr(capture_thread, "is_alive") and capture_thread.is_alive())
        stt_alive = bool(stt_thread is not None and hasattr(stt_thread, "is_alive") and stt_thread.is_alive())
        alive = bool(capture_alive or stt_alive)
        if not alive:
            with summary_state_lock:
                state["capture_thread"] = None
                state["stt_thread"] = None
                state["stop_event"] = None
                state["audio_queue"] = None
                stats = state.get("capture_stats")
                if isinstance(stats, dict):
                    stats["stt_queue_size"] = 0
        return not alive

    def _summary_cleanup_sid(sid: str) -> None:
        with summary_state_lock:
            state = summary_state_by_sid.get(sid)
        if state is None:
            return
        _summary_stop_capture(state)
        with summary_state_lock:
            summary_state_by_sid.pop(sid, None)

    def _summary_cleanup_all() -> None:
        with summary_state_lock:
            sid_keys = list(summary_state_by_sid.keys())
        for sid_key in sid_keys:
            _summary_cleanup_sid(sid_key)

    def _set_last_action(sid: str, summary: Optional[str]) -> None:
        _get_runtime_state(sid)["last_action"] = summary

    def _set_command_stop_available(sid: str, label: Optional[str], *, scope: Optional[str] = None) -> None:
        state = _get_runtime_state(sid)
        state["command_stop_available"] = True
        state["command_stop_label"] = str(label or "").strip() or None
        scope_norm = str(scope or "").strip().lower()
        state["command_stop_scope"] = scope_norm if scope_norm in _CUSTOM_LIFE_LOCK_MODES else None

    def _clear_command_stop_available(sid: str) -> None:
        state = _get_runtime_state(sid)
        state["command_stop_available"] = False
        state["command_stop_label"] = None
        state["command_stop_scope"] = None

    def _command_stop_payload(sid: str) -> Dict[str, Any]:
        state = _get_runtime_state(sid)
        return {
            "command_stop_available": bool(state.get("command_stop_available")),
            "command_stop_label": state.get("command_stop_label"),
        }

    def _attach_command_stop_payload(sid: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        payload.update(_command_stop_payload(sid))
        return payload

    def _normalize_command_label(label: Optional[str]) -> str:
        return str(label or "").upper().strip().replace("\\_", "_")

    def _custom_life_merge_lock_mode(current_mode: Optional[str], requested_mode: Optional[str]) -> Optional[str]:
        current = str(current_mode or "").strip().lower()
        requested = str(requested_mode or "").strip().lower()
        if current not in _CUSTOM_LIFE_LOCK_MODES:
            current = ""
        if requested not in _CUSTOM_LIFE_LOCK_MODES:
            requested = ""
        if current == _CUSTOM_LIFE_LOCK_UNTIL_STOP or requested == _CUSTOM_LIFE_LOCK_UNTIL_STOP:
            return _CUSTOM_LIFE_LOCK_UNTIL_STOP
        if requested:
            return requested
        return current or None

    def _custom_life_lock_mode_for_command(cmd: CommandDecision) -> Optional[str]:
        label = _normalize_command_label(cmd.label)
        if label in ("WALK_WITH_ME",):
            return _CUSTOM_LIFE_LOCK_UNTIL_STOP
        if label == "LOCOMOTION_REQUEST":
            resolved = cmd.resolved or {}
            locomotion = str(resolved.get("locomotion") or "").strip().lower()
            if locomotion == "exit":
                return None
            return _CUSTOM_LIFE_LOCK_UNTIL_STOP
        if label in ("BOX", "HIGH_FIVE", "DANCE"):
            return _CUSTOM_LIFE_LOCK_UNTIL_NEXT_USER_TURN
        return None

    def _custom_life_lock_mode_for_behavior_name(behavior: str) -> Optional[str]:
        behavior_norm = str(behavior or "").strip().lower()
        if not behavior_norm:
            return None
        if "walkwithme" in behavior_norm:
            return _CUSTOM_LIFE_LOCK_UNTIL_STOP
        if behavior_norm.startswith("dances/"):
            return _CUSTOM_LIFE_LOCK_UNTIL_NEXT_USER_TURN
        if behavior_norm in ("greetings", "greetings/doboxsit", "greetings/dohighfive"):
            return _CUSTOM_LIFE_LOCK_UNTIL_NEXT_USER_TURN
        return None

    def _walk_behavior_active_for_sid(sid: str) -> bool:
        runtime_state = _get_runtime_state(sid)
        if runtime_state.get("custom_life_lock_mode") == _CUSTOM_LIFE_LOCK_UNTIL_STOP:
            return True
        cmd_state = _get_cmdrec_state(sid)
        active = _normalize_command_label(cmd_state.get("active_behavior"))
        return active in ("WALK_WITH_ME", "LOCOMOTION_REQUEST")

    def _post_stop_standup_delay_s(runtime_cfg_local: Optional[JsonLike] = None) -> float:
        cfg_local = runtime_cfg_local or {}
        raw = cfg_local.get("nao_post_stop_standup_delay_s", base_cfg.get("nao_post_stop_standup_delay_s", 0.8))
        try:
            delay_s = float(raw)
        except Exception:
            delay_s = 0.8
        return max(0.0, delay_s)

    def _execute_standup_after_stop_if_needed(
        sid: str,
        behavior_executor,
        *,
        runtime_cfg_local: Optional[JsonLike] = None,
        source: str = "system_auto",
        request_id: Optional[str] = None,
        turn_id: Optional[str] = None,
    ) -> None:
        if behavior_executor is None:
            return
        if not _walk_behavior_active_for_sid(sid):
            return
        _log_dm_event(
            sid=sid,
            level="info",
            category="nao",
            event="post_stop_standup_start",
            source=source,
            message="Attempting automatic stand-up after stop.",
            request_id=request_id,
            turn_id=turn_id,
            data={},
        )
        try:
            delay_s = _post_stop_standup_delay_s(runtime_cfg_local)
            if delay_s > 0.0:
                time.sleep(delay_s)
            behavior_executor.execute(
                CommandDecision(
                    label="STAND_UP",
                    confidence=1.0,
                    raw_text="auto_standup_after_stop",
                )
            )
            _log_dm_event(
                sid=sid,
                level="info",
                category="nao",
                event="post_stop_standup_result",
                source=source,
                message="Automatic stand-up completed.",
                request_id=request_id,
                turn_id=turn_id,
                data={"ok": True},
            )
        except Exception as exc:
            print(f"[NAO] post-stop standup failed: {exc}")
            _log_dm_event(
                sid=sid,
                level="error",
                category="nao",
                event="post_stop_standup_result",
                source=source,
                message="Automatic stand-up failed.",
                request_id=request_id,
                turn_id=turn_id,
                data={"ok": False, "error": str(exc), "type": exc.__class__.__name__},
            )

    def _set_behavior_executor_custom_life_management(behavior_executor, enabled: bool) -> None:
        if behavior_executor is None:
            return
        setter = getattr(behavior_executor, "set_custom_life_management_enabled", None)
        if callable(setter):
            try:
                setter(bool(enabled))
            except Exception:
                pass

    def _custom_life_pause_on_endpoint(base_url: str, endpoint_mode: str) -> Tuple[bool, Optional[Dict[str, Any]]]:
        pause_url = base_url + ("/nao/custom_life_pause" if endpoint_mode == "behavior" else "/custom_life_pause")
        try:
            resp = requests.post(pause_url, json={}, timeout=3.0)
            resp.raise_for_status()
            data = resp.json()
        except Exception:
            return False, None
        pause_state = None
        if isinstance(data, dict):
            payload = data.get("data") or data.get("state")
            if isinstance(payload, dict):
                pause_state = payload
        return True, pause_state

    def _custom_life_restore_on_endpoint(
        base_url: str,
        endpoint_mode: str,
        runtime_cfg_local: JsonLike,
        pause_state: Optional[Dict[str, Any]],
    ) -> None:
        settings = runtime_cfg_local.get("custom_life_settings") or {}
        if runtime_cfg_local.get("custom_life_enabled") and settings:
            restore_url = base_url + ("/nao/custom_life_apply" if endpoint_mode == "behavior" else "/custom_life_apply")
            resp = requests.post(restore_url, json={"settings": settings}, timeout=3.0)
            resp.raise_for_status()
            return
        if pause_state:
            restore_url = base_url + ("/nao/custom_life_resume" if endpoint_mode == "behavior" else "/custom_life_resume")
            resp = requests.post(restore_url, json={"state": pause_state}, timeout=3.0)
            resp.raise_for_status()

    def _custom_life_clear_lock_state(state: Dict[str, Any]) -> None:
        state["custom_life_lock_mode"] = None
        state["custom_life_paused"] = False
        state["custom_life_pause_state"] = None
        state["custom_life_pause_base_url"] = None
        state["custom_life_pause_mode"] = None

    def _custom_life_lock_is_active(state: Dict[str, Any]) -> bool:
        mode = str(state.get("custom_life_lock_mode") or "").strip().lower()
        return bool(state.get("custom_life_paused")) and mode in _CUSTOM_LIFE_LOCK_MODES

    def _custom_life_store_lock_state(
        state: Dict[str, Any],
        *,
        lock_mode: str,
        pause_state: Optional[Dict[str, Any]],
        base_url: str,
        endpoint_mode: str,
    ) -> None:
        state["custom_life_lock_mode"] = lock_mode
        state["custom_life_paused"] = True
        state["custom_life_pause_state"] = pause_state
        state["custom_life_pause_base_url"] = base_url
        state["custom_life_pause_mode"] = endpoint_mode

    def _acquire_custom_life_lock(
        sid: str,
        runtime_cfg_local: JsonLike,
        lock_mode: Optional[str],
        *,
        base_url: Optional[str] = None,
        endpoint_mode: Optional[str] = None,
    ) -> bool:
        if lock_mode not in _CUSTOM_LIFE_LOCK_MODES:
            return False
        state = _get_runtime_state(sid)
        if _custom_life_lock_is_active(state):
            state["custom_life_lock_mode"] = _custom_life_merge_lock_mode(state.get("custom_life_lock_mode"), lock_mode)
            return True
        if not runtime_cfg_local.get("custom_life_enabled"):
            return False
        resolved_base = base_url
        resolved_mode = endpoint_mode
        if not resolved_base or resolved_mode not in ("behavior", "base"):
            resolved_base, resolved_mode = _resolve_nao_audio_url(runtime_cfg_local)
        if not resolved_base or resolved_mode not in ("behavior", "base"):
            return False
        paused, pause_state = _custom_life_pause_on_endpoint(resolved_base, resolved_mode)
        if not paused:
            return False
        _custom_life_store_lock_state(
            state,
            lock_mode=lock_mode,
            pause_state=pause_state,
            base_url=resolved_base,
            endpoint_mode=resolved_mode,
        )
        return True

    def _release_custom_life_lock(sid: str, runtime_cfg_local: JsonLike) -> bool:
        state = _get_runtime_state(sid)
        if not _custom_life_lock_is_active(state):
            if state.get("custom_life_lock_mode") in _CUSTOM_LIFE_LOCK_MODES:
                _custom_life_clear_lock_state(state)
            return False

        base_url = state.get("custom_life_pause_base_url")
        endpoint_mode = state.get("custom_life_pause_mode")
        pause_state = state.get("custom_life_pause_state")
        if not base_url or endpoint_mode not in ("behavior", "base"):
            base_url, endpoint_mode = _resolve_nao_audio_url(runtime_cfg_local)
        if base_url and endpoint_mode in ("behavior", "base"):
            try:
                _custom_life_restore_on_endpoint(base_url, endpoint_mode, runtime_cfg_local, pause_state)
            except Exception as exc:
                print(f"[NAO] restore custom life lock failed: {exc}")
        _custom_life_clear_lock_state(state)
        return True

    def _release_custom_life_lock_if_needed_on_user_turn(sid: str, runtime_cfg_local: JsonLike) -> None:
        state = _get_runtime_state(sid)
        if state.get("custom_life_lock_mode") == _CUSTOM_LIFE_LOCK_UNTIL_NEXT_USER_TURN:
            _release_custom_life_lock(sid, runtime_cfg_local)
        if state.get("command_stop_scope") == _CUSTOM_LIFE_LOCK_UNTIL_NEXT_USER_TURN:
            _clear_command_stop_available(sid)

    # runtime pipeline helpers
    def _get_runtime_cfg(sid: str) -> JsonLike:
        del sid
        with runtime_cfg_lock:
            _sanitize_runtime_cfg_inplace(runtime_cfg_global)
            return runtime_cfg_global

    def _auto_rest_timeout_s(runtime_cfg: JsonLike) -> float:
        value = runtime_cfg.get("nao_auto_rest_after_s", base_cfg.get("nao_auto_rest_after_s", 0))
        try:
            return float(value or 0)
        except Exception:
            return 0.0

    def _touch_activity() -> None:
        with activity_lock:
            last_activity["ts"] = time.time()

    def _ensure_sid_runtime_pipeline(sid: str) -> None:
        if sid in pipeline_by_sid and sid in stt_by_sid:
            return
        runtime_cfg = _get_runtime_cfg(sid)
        merged = _apply_runtime_overrides(base_cfg, runtime_cfg)
        _rebuild_pipeline_for_sid(sid, merged)

    def _get_pipeline(sid: str) -> InputLLMOutputPipeline:
        _ensure_sid_runtime_pipeline(sid)
        return pipeline_by_sid.get(sid, base_pipeline)

    def _get_stt(sid: str):
        _ensure_sid_runtime_pipeline(sid)
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

    def _nao_behavior_cache_key(runtime_cfg: JsonLike) -> str:
        behavior_url = _normalize_url(runtime_cfg.get("behavior_manager_url")) or ""
        base_url = _normalize_url(runtime_cfg.get("nao_base_url")) or ""
        behavior_enabled = bool(runtime_cfg.get("behavior_enabled", True))
        base_enabled = bool(runtime_cfg.get("base_enabled", True))
        return "|".join(
            [
                "behavior_enabled=1" if behavior_enabled else "behavior_enabled=0",
                f"behavior_url={behavior_url}",
                "base_enabled=1" if base_enabled else "base_enabled=0",
                f"base_url={base_url}",
            ]
        )

    def _resolve_nao_behavior_endpoint(runtime_cfg: JsonLike, *, require_ping: bool = True) -> Tuple[Optional[str], str]:
        base_url = _normalize_url(runtime_cfg.get("nao_base_url"))
        behavior_url = _normalize_url(runtime_cfg.get("behavior_manager_url"))
        base_enabled = bool(runtime_cfg.get("base_enabled", True))
        behavior_enabled = bool(runtime_cfg.get("behavior_enabled", True))

        if behavior_enabled and behavior_url:
            if not require_ping or _check_ping(behavior_url, "/ping"):
                return behavior_url, "behavior"
        if base_enabled and base_url:
            if not require_ping or _check_ping(base_url, "/ping"):
                return base_url, "base"
        return None, "none"

    def _fetch_nao_behaviors(runtime_cfg: JsonLike) -> Tuple[Optional[List[str]], str]:
        base_url, mode = _resolve_nao_behavior_endpoint(runtime_cfg, require_ping=True)
        if not base_url or mode not in ("behavior", "base"):
            return None, "endpoint_unavailable"
        try:
            url = base_url + "/nao/list_behaviors" if mode == "behavior" else base_url + "/list_behaviors"
            resp = requests.get(url, timeout=3.0)
            resp.raise_for_status()
            data = resp.json()
            payload = data.get("data") if isinstance(data, dict) else None
            behaviors = sorted(_flatten_behaviors(payload))
            return behaviors, mode
        except Exception:
            return None, mode

    def _available_nao_behaviors(runtime_cfg: JsonLike) -> Optional[List[str]]:
        cache_key = _nao_behavior_cache_key(runtime_cfg)
        now = time.time()
        cached_entry = None
        with behavior_catalog_cache_lock:
            cached_entry = behavior_catalog_cache.get(cache_key)
            if cached_entry is not None:
                ts = float(cached_entry.get("ts") or 0.0)
                if now - ts <= _NAO_BEHAVIOR_CACHE_TTL_S:
                    cached_behaviors = cached_entry.get("behaviors")
                    if isinstance(cached_behaviors, list):
                        return list(cached_behaviors)
                    return None

        fetched_behaviors, mode = _fetch_nao_behaviors(runtime_cfg)
        if fetched_behaviors is None and cached_entry is not None:
            stale_behaviors = cached_entry.get("behaviors")
            if isinstance(stale_behaviors, list):
                return list(stale_behaviors)

        with behavior_catalog_cache_lock:
            behavior_catalog_cache[cache_key] = {
                "ts": now,
                "mode": mode,
                "behaviors": list(fetched_behaviors) if isinstance(fetched_behaviors, list) else None,
            }

        if isinstance(fetched_behaviors, list):
            return list(fetched_behaviors)
        return None

    def _normalize_behavior_name(value: Any) -> str:
        return str(value or "").strip().casefold()

    def _filter_dance_catalog(
        catalog: List[Dict[str, Any]],
        available_behaviors: Optional[List[str]],
    ) -> List[Dict[str, Any]]:
        if not catalog:
            return []
        if available_behaviors is None:
            return list(catalog)
        allowed = {
            _normalize_behavior_name(behavior)
            for behavior in available_behaviors
            if isinstance(behavior, str) and behavior.strip()
        }

        filtered: List[Dict[str, Any]] = []
        for item in catalog:
            behavior = item.get("behavior")
            if isinstance(behavior, str) and behavior.strip():
                if _normalize_behavior_name(behavior) not in allowed:
                    continue
            filtered.append(item)
        return filtered

    def _dance_catalog(cmdrec_obj, runtime_cfg: Optional[JsonLike] = None) -> List[Dict[str, Any]]:
        if cmdrec_obj is None:
            return []
        try:
            catalog = cmdrec_obj.get_dance_catalog() or []
        except Exception:
            return []
        if runtime_cfg is None:
            return list(catalog)
        available_behaviors = _available_nao_behaviors(runtime_cfg)
        return _filter_dance_catalog(list(catalog), available_behaviors)

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
            spoken_text = text
            normalizer = getattr(pipeline, "normalize_output_text", None)
            if callable(normalizer):
                spoken_text = str(normalizer(text) or "")
            if not spoken_text:
                return
            pipeline.output.emit(spoken_text)
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

    def _format_dance_catalog_for_runtime_prompt(catalog: List[Dict[str, Any]]) -> str:
        keys = _dance_keys(catalog)
        if not keys:
            return ""
        lines = [f"- {key}" for key in keys]
        return "\n".join(lines)

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

    def _default_dance_resolution(cmdrec_obj, runtime_cfg: Optional[JsonLike] = None) -> Dict[str, str]:
        policy = _dance_followup_policy(cmdrec_obj)
        dance_key = _policy_value(policy, "default_dance_key", "happy")
        catalog = _dance_catalog(cmdrec_obj, runtime_cfg=runtime_cfg)
        dance_behavior = None
        for item in catalog:
            if (item.get("key") or "").strip() == dance_key:
                dance_behavior = item.get("behavior")
                break
        if not dance_behavior and catalog:
            first = catalog[0]
            first_key = first.get("key")
            if isinstance(first_key, str) and first_key.strip():
                dance_key = first_key.strip()
            first_behavior = first.get("behavior")
            if isinstance(first_behavior, str) and first_behavior.strip():
                dance_behavior = first_behavior.strip()
        if not dance_behavior and isinstance(dance_key, str) and dance_key.strip():
            fallback_behavior = "dances/" + dance_key.strip()
            if runtime_cfg is not None:
                available_behaviors = _available_nao_behaviors(runtime_cfg)
                if available_behaviors is not None:
                    available_set = {
                        _normalize_behavior_name(name)
                        for name in available_behaviors
                        if isinstance(name, str) and name.strip()
                    }
                    if _normalize_behavior_name(fallback_behavior) in available_set:
                        dance_behavior = fallback_behavior
                else:
                    dance_behavior = fallback_behavior
            else:
                dance_behavior = fallback_behavior
        return {"dance_key": dance_key, "dance_behavior": dance_behavior}  # type: ignore[return-value]

    def _command_behavior_preview(
        label: str,
        cmdrec_obj,
        behavior_executor,
        runtime_cfg: Optional[JsonLike] = None,
    ) -> Optional[str]:
        if not label:
            return None
        if label.upper() == "DANCE":
            resolved = _default_dance_resolution(cmdrec_obj, runtime_cfg=runtime_cfg)
            behavior = resolved.get("dance_behavior")
            return behavior if isinstance(behavior, str) and behavior.strip() else None
        if behavior_executor is not None and hasattr(behavior_executor, "_behavior_for_command"):
            try:
                cmd = CommandDecision(label=label, confidence=1.0, raw_text=label, resolved=None)
                if label.upper() == "DANCE":
                    cmd.resolved = _default_dance_resolution(cmdrec_obj, runtime_cfg=runtime_cfg)
                return behavior_executor._behavior_for_command(cmd)  # type: ignore[attr-defined]
            except Exception:
                return None
        return None

    def _sync_runtime_dance_catalog_for_sid(
        sid: str,
        *,
        pipeline,
        cmdrec_obj,
        runtime_cfg: JsonLike,
    ) -> None:
        runtime_context_enabled = bool(
            getattr(pipeline, "runtime_context_enabled", False)
            or getattr(pipeline, "_runtime_context_enabled", False)
        )
        if not runtime_context_enabled:
            return
        runtime_context_static = getattr(pipeline, "runtime_context_static", None)
        if runtime_context_static is None:
            runtime_context_static = getattr(pipeline, "_runtime_context_static", None)
        if not isinstance(runtime_context_static, dict):
            runtime_context_static = {}

        catalog = _dance_catalog(cmdrec_obj, runtime_cfg=runtime_cfg)
        dances_text = _format_dance_catalog_for_runtime_prompt(catalog)
        current_text = str(runtime_context_static.get("dance_catalog") or "")
        if current_text == dances_text:
            return

        updated = dict(runtime_context_static)
        updated["dance_catalog"] = dances_text
        if hasattr(pipeline, "runtime_context_static"):
            pipeline.runtime_context_static = dict(updated)
        if hasattr(pipeline, "_runtime_context_static"):
            pipeline._runtime_context_static = dict(updated)

    def _system_prompt_for_sid(sid: str) -> str:
        pipeline = _get_pipeline(sid)
        _, cmdrec, _ = _pipeline_props(pipeline)
        runtime_cfg = _get_runtime_cfg(sid)
        _sync_runtime_dance_catalog_for_sid(
            sid,
            pipeline=pipeline,
            cmdrec_obj=cmdrec,
            runtime_cfg=runtime_cfg,
        )
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

    _CMD_STATE_KEEP = object()

    def _clip_for_log(value: Any, max_len: int = 120) -> str:
        text = str(value or "").strip()
        if len(text) <= max_len:
            return text
        return text[: max_len - 3] + "..."

    def _log_state_event(
        sid: str,
        *,
        event: str,
        reason: str,
        command_label: Optional[str] = None,
        text: Optional[str] = None,
        confidence: Optional[float] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> None:
        state = _get_cmdrec_state(sid)
        payload: Dict[str, Any] = {
            "sid": sid,
            "event": event,
            "reason": reason,
            "mode": str(state.get("mode") or ""),
            "active_behavior": str(state.get("active_behavior") or ""),
        }
        if command_label:
            payload["command"] = str(command_label)
        if text:
            payload["text"] = _clip_for_log(text)
        if confidence is not None:
            payload["confidence"] = round(float(confidence), 4)
        if isinstance(extra, dict):
            for key, value in extra.items():
                payload[str(key)] = value
        try:
            encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        except Exception:
            encoded = str(payload)
        _append_proc_log("base", f"[DM][STATE] {encoded}")

    def _set_cmd_state(
        sid: str,
        state: Dict[str, Any],
        *,
        mode: Any = _CMD_STATE_KEEP,
        active_behavior: Any = _CMD_STATE_KEEP,
        reason: str,
        command_label: Optional[str] = None,
        text: Optional[str] = None,
        confidence: Optional[float] = None,
        source: str = "system_auto",
        request_id: Optional[str] = None,
        turn_id: Optional[str] = None,
    ) -> None:
        prev_mode = str(state.get("mode") or "")
        prev_active = str(state.get("active_behavior") or "")
        if mode is not _CMD_STATE_KEEP:
            state["mode"] = mode
        if active_behavior is not _CMD_STATE_KEEP:
            state["active_behavior"] = active_behavior
        new_mode = str(state.get("mode") or "")
        new_active = str(state.get("active_behavior") or "")
        _log_state_event(
            sid,
            event="transition" if (prev_mode != new_mode or prev_active != new_active) else "no_change",
            reason=reason,
            command_label=command_label,
            text=text,
            confidence=confidence,
            extra={
                "prev_mode": prev_mode,
                "prev_active_behavior": prev_active,
                "new_mode": new_mode,
                "new_active_behavior": new_active,
            },
        )
        _log_dm_event(
            sid=sid,
            level="info",
            category="state",
            event="state_transition",
            source=source,
            message="Command state transition updated.",
            request_id=request_id,
            turn_id=turn_id,
            data={
                "reason": reason,
                "command": command_label,
                "changed": bool(prev_mode != new_mode or prev_active != new_active),
                "prev_mode": prev_mode,
                "prev_active_behavior": prev_active,
                "new_mode": new_mode,
                "new_active_behavior": new_active,
                "confidence": confidence,
            },
        )

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
        source: str,
        request_id: Optional[str],
        turn_id: Optional[str],
    ) -> Optional[Dict[str, Any]]:
        if not decision.is_command or not decision.command:
            return None

        state = _get_cmdrec_state(sid)
        cmd = decision.command
        _log_state_event(
            sid,
            event="command_detected",
            reason="route_decision",
            command_label=cmd.label,
            text=text,
            confidence=cmd.confidence,
        )
        _log_dm_event(
            sid=sid,
            level="info",
            category="cmdrec",
            event="command_detected",
            source=source,
            message=f"Command detected: {cmd.label}",
            request_id=request_id,
            turn_id=turn_id,
            data={
                "command": cmd.label,
                "confidence": cmd.confidence,
                "reason": decision.reason,
                "top3": decision.top3,
            },
        )

        if cmd.label == "STOP":
            _append_user(history, text)
            pending_confirms.pop(sid, None)
            if behavior_executor:
                _set_behavior_executor_custom_life_management(behavior_executor, False)
                behavior_executor.execute(cmd)
                _execute_standup_after_stop_if_needed(
                    sid,
                    behavior_executor,
                    runtime_cfg_local=runtime_cfg,
                    source="system_auto",
                    request_id=request_id,
                    turn_id=turn_id,
                )
            _release_custom_life_lock(sid, runtime_cfg)
            _clear_command_stop_available(sid)
            _stop_nao_audio_best_effort(
                runtime_cfg,
                source="system_auto",
                request_id=request_id,
                turn_id=turn_id,
                sid=sid,
                reason="command_stop",
            )
            _touch_activity()
            _set_cmd_state(
                sid,
                state,
                mode="DIALOG",
                active_behavior=None,
                reason="command_stop_executed",
                command_label=cmd.label,
                text=text,
                confidence=cmd.confidence,
                source=source,
                request_id=request_id,
                turn_id=turn_id,
            )
            _set_last_action(sid, _format_action_summary("Uitgevoerd", cmd.label, cmd.resolved))
            _append_assistant(history, "OK. Uitgevoerd: STOP")
            _log_dm_event(
                sid=sid,
                level="info",
                category="cmdrec",
                event="command_executed",
                source=source,
                message="Command executed: STOP",
                request_id=request_id,
                turn_id=turn_id,
                data={"command": cmd.label, "confidence": cmd.confidence, "resolved": cmd.resolved or {}},
            )
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
            _log_dm_event(
                sid=sid,
                level="info",
                category="dialog",
                event="dialog_reply",
                source=source,
                message="Assistant reply generated after command execution.",
                request_id=request_id,
                turn_id=turn_id,
                data={"reply": payload.get("reply") or "", "emit_used": emit_used},
            )
            return payload

        confirm_policy = _clean_confirm_policy(runtime_cfg.get("confirm_policy") or base_cfg.get("confirm_policy"))
        confirm_needed = False
        if confirm_method != "none":
            if confirm_policy == "always":
                confirm_needed = True
            elif confirm_policy == "never":
                confirm_needed = False
            else:
                confirm_needed = bool(cmdrec_obj and cmdrec_obj.is_guarded(cmd.label))

        if confirm_needed:
            _log_state_event(
                sid,
                event="confirm_required",
                reason="guarded_command",
                command_label=cmd.label,
                text=text,
                confidence=cmd.confidence,
            )
            _log_dm_event(
                sid=sid,
                level="info",
                category="cmdrec",
                event="confirm_required",
                source=source,
                message=f"Confirmation required for command: {cmd.label}",
                request_id=request_id,
                turn_id=turn_id,
                data={
                    "command": cmd.label,
                    "confidence": cmd.confidence,
                    "confirm_policy": confirm_policy,
                },
            )
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
        lock_mode = _custom_life_lock_mode_for_command(cmd)
        if lock_mode:
            _acquire_custom_life_lock(sid, runtime_cfg, lock_mode)
        if behavior_executor:
            _set_behavior_executor_custom_life_management(behavior_executor, False)
            behavior_executor.execute(cmd)
        _touch_activity()
        _set_cmd_state(
            sid,
            state,
            mode="PERFORMING",
            active_behavior=cmd.label,
            reason="command_executed",
            command_label=cmd.label,
            text=text,
            confidence=cmd.confidence,
            source=source,
            request_id=request_id,
            turn_id=turn_id,
        )
        if lock_mode in _CUSTOM_LIFE_LOCK_MODES:
            _set_command_stop_available(sid, cmd.label, scope=lock_mode)
        else:
            _clear_command_stop_available(sid)
        _set_last_action(sid, _format_action_summary("Uitgevoerd", cmd.label, cmd.resolved))
        _append_assistant(history, f"OK. Uitgevoerd: {cmd.label}")
        _log_dm_event(
            sid=sid,
            level="info",
            category="cmdrec",
            event="command_executed",
            source=source,
            message=f"Command executed: {cmd.label}",
            request_id=request_id,
            turn_id=turn_id,
            data={"command": cmd.label, "confidence": cmd.confidence, "resolved": cmd.resolved or {}},
        )
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
        _log_dm_event(
            sid=sid,
            level="info",
            category="dialog",
            event="dialog_reply",
            source=source,
            message="Assistant reply generated after command execution.",
            request_id=request_id,
            turn_id=turn_id,
            data={"reply": payload.get("reply") or "", "emit_used": emit_used},
        )
        return payload

    def _ensure_proc_log_dir() -> None:
        try:
            os.makedirs(proc_log_dir, exist_ok=True)
        except OSError:
            pass

    def _proc_log_path(name: str) -> Optional[str]:
        return proc_log_paths.get(name)

    def _rotate_proc_log_if_needed(path: str) -> None:
        if not path:
            return
        try:
            if not os.path.exists(path):
                return
            if os.path.getsize(path) < proc_log_max_bytes:
                return
            backup = path + proc_log_backup_suffix
            if os.path.exists(backup):
                os.remove(backup)
            os.replace(path, backup)
        except OSError:
            pass

    def _append_proc_log_file(name: str, entry: str) -> None:
        path = _proc_log_path(name)
        if not path:
            return
        _ensure_proc_log_dir()
        _rotate_proc_log_if_needed(path)
        try:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(entry + "\n")
        except OSError:
            pass

    def _load_proc_log_tail(name: str, limit: int) -> List[str]:
        path = _proc_log_path(name)
        if not path or not os.path.exists(path):
            return []
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                lines = [ln.rstrip("\r\n") for ln in fh if ln.rstrip("\r\n")]
        except OSError:
            return []
        if limit <= 0:
            return lines
        return lines[-limit:]

    def _clear_proc_log_files(name: str) -> None:
        path = _proc_log_path(name)
        if not path:
            return
        for candidate in (path, path + proc_log_backup_suffix):
            try:
                if os.path.exists(candidate):
                    os.remove(candidate)
            except OSError:
                pass

    def _append_proc_log(name: str, line: str) -> None:
        line = line.rstrip("\r\n")
        if not line:
            return
        ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        entry = f"[{ts}] {line}"
        with proc_lock:
            buf = proc_logs.setdefault(name, [])
            buf.append(entry)
            if len(buf) > max_log_lines:
                del buf[: len(buf) - max_log_lines]
            _append_proc_log_file(name, entry)

    _ensure_proc_log_dir()
    for _proc_name in list(proc_logs.keys()):
        proc_logs[_proc_name] = _load_proc_log_tail(_proc_name, max_log_lines)
    _ensure_dm_event_dir()
    dm_events[:] = _load_dm_event_tail(dm_event_tail_limit)
    dm_event_writer_thread = threading.Thread(target=_dm_event_writer_loop, daemon=True)
    dm_event_writer_thread.start()

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
            _append_proc_log(name, f"[err] start failed: {exc}")
            return {"ok": False, "error": str(exc)}

        with proc_lock:
            proc_state[name] = {"proc": proc, "exit_code": None}
        _append_proc_log(name, "--- started ---")

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
        _append_proc_log(name, "--- stopped ---")
        return {"ok": True, "stopped": True}

    def _pick_shutdown_runtime_cfg() -> JsonLike:
        return _runtime_cfg_snapshot()

    def _try_nao_rest(runtime_cfg: JsonLike, *, reason: str) -> bool:
        def _rest_ok(resp) -> bool:
            if resp is None:
                return False
            if getattr(resp, "status_code", 500) >= 400:
                return False
            try:
                payload = resp.json()
            except Exception:
                return True
            if isinstance(payload, dict):
                if payload.get("ok") is False:
                    return False
                status = payload.get("status")
                if status not in (None, "ok"):
                    return False
            return True

        base_url = _normalize_url(runtime_cfg.get("nao_base_url"))
        behavior_url = _normalize_url(runtime_cfg.get("behavior_manager_url"))
        base_enabled = bool(runtime_cfg.get("base_enabled", True))
        behavior_enabled = bool(runtime_cfg.get("behavior_enabled", True))

        if behavior_enabled and behavior_url and _check_ping(behavior_url, "/ping"):
            try:
                resp = requests.post(behavior_url + "/nao/rest", json={}, timeout=2.0)
                if _rest_ok(resp):
                    return True
            except Exception:
                pass

        if base_enabled and base_url and _check_ping(base_url, "/ping"):
            try:
                resp = requests.post(base_url + "/rest", json={}, timeout=2.0)
                if _rest_ok(resp):
                    return True
            except Exception:
                pass
        return False

    def _stop_all_processes() -> None:
        _stop_dm_event_writer()
        _summary_cleanup_all()
        _try_nao_rest(_pick_shutdown_runtime_cfg(), reason="shutdown")
        for name in ("behavior", "base"):
            _stop_process(name)

    global _SHUTDOWN_HOOK_REGISTERED, _LATEST_SHUTDOWN_HOOK
    with _SHUTDOWN_HOOK_LOCK:
        _LATEST_SHUTDOWN_HOOK = _stop_all_processes
        if not _SHUTDOWN_HOOK_REGISTERED:
            atexit.register(_run_latest_shutdown_hook)
            _SHUTDOWN_HOOK_REGISTERED = True

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
            if _try_nao_rest(runtime_cfg, reason="idle"):
                _touch_activity()

    threading.Thread(target=_auto_rest_loop, daemon=True).start()

    def _base_controller_cmd(nao_ip: str, web_port: int) -> Tuple[List[str], str, Optional[str]]:
        base_dir = os.path.join(repo_root, "py2_nao_base_controller")
        python_path = os.path.join(base_dir, "venv", "Scripts", "python.exe")
        script_path = os.path.join(base_dir, "nao_api.py")
        if not os.path.exists(python_path):
            return [], base_dir, "venv\\Scripts\\python.exe niet gevonden (py2_nao_base_controller)."
        if not os.path.exists(script_path):
            return [], base_dir, "nao_api.py niet gevonden."
        cmd = [python_path, script_path, "--port", str(int(web_port))]
        if nao_ip:
            cmd += ["--nao_ip", nao_ip]
        return cmd, base_dir, None

    def _behavior_manager_cmd(web_port: int, py2_api_url: str) -> Tuple[List[str], str, Optional[str]]:
        base_dir = os.path.join(repo_root, "py3_nao_behavior_manager")
        python_path = os.path.join(base_dir, "venv", "Scripts", "python.exe")
        script_path = os.path.join(base_dir, "py3_server.py")
        if not os.path.exists(python_path):
            return [], base_dir, "venv\\Scripts\\python.exe niet gevonden (py3_nao_behavior_manager)."
        if not os.path.exists(script_path):
            return [], base_dir, "py3_server.py niet gevonden."
        cmd = [
            python_path,
            script_path,
            "--port",
            str(int(web_port)),
            "--py2-api-url",
            str(py2_api_url or "http://127.0.0.1:5000"),
        ]
        return cmd, base_dir, None

    _al_key_cache: Dict[str, Dict[str, Any]] = {}

    def _cmdrec_data_path(*names: str) -> str:
        return os.path.join(repo_root, "py3_command_recognition_train", "data", *names)

    def _active_train_base_relpath() -> str:
        train_v2 = _cmdrec_data_path("train_base_v2.jsonl")
        if os.path.exists(train_v2):
            return "data/train_base_v2.jsonl"
        return "data/train_base.jsonl"

    def _active_train_base_abspath() -> str:
        return _cmdrec_data_path(os.path.basename(_active_train_base_relpath()))

    def _active_gold_relpath() -> str:
        gold_v2 = _cmdrec_data_path("gold_v2.jsonl")
        if os.path.exists(gold_v2):
            return "data/gold_v2.jsonl"
        return "data/gold_v1.jsonl"

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
        train_base_path = _active_train_base_abspath()
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
        raw = (text or "").casefold()
        if not raw:
            return ""
        # Treat punctuation/symbol-only differences as duplicates in AL datasets.
        cleaned_chars: List[str] = []
        for ch in raw:
            cat = unicodedata.category(ch)
            if cat.startswith("P") or cat.startswith("S"):
                cleaned_chars.append(" ")
            else:
                cleaned_chars.append(ch)
        return " ".join("".join(cleaned_chars).split()).strip()

    def _dedupe_al_text_entries(entries: List[Dict[str, Any]], *, keep: str = "newest") -> List[Dict[str, Any]]:
        keep_newest = str(keep or "newest").strip().lower() != "oldest"
        src = reversed(entries) if keep_newest else iter(entries)
        out: List[Dict[str, Any]] = []
        seen: set[str] = set()
        for entry in src:
            if not isinstance(entry, dict):
                continue
            key = _normalize_key(entry.get("text", ""))
            if key:
                if key in seen:
                    continue
                seen.add(key)
            out.append(entry)
        if keep_newest:
            out.reverse()
        return out

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

    @app.before_request
    def _dm_before_request() -> None:
        if not str(request.path or "").startswith("/api/"):
            return
        g.dm_request_id = _new_request_id()
        g.dm_request_started_at = time.time()

    @app.after_request
    def _dm_after_request(resp: Response) -> Response:
        if not str(request.path or "").startswith("/api/"):
            return resp
        request_id = str(getattr(g, "dm_request_id", "") or _new_request_id())
        started_at = _get_request_started_at()
        resp.headers["X-Request-Id"] = request_id
        if resp.status_code < 400 or str(resp.headers.get("X-DM-Error-Logged", "")) == "1":
            return resp
        message = "Request failed."
        error_type = "http_error_response"
        try:
            payload = resp.get_json(silent=True)
            if isinstance(payload, dict):
                if payload.get("error"):
                    message = str(payload.get("error"))
                if payload.get("type"):
                    error_type = str(payload.get("type"))
        except Exception:
            pass
        source = _infer_event_source(endpoint=request.path)
        _log_dm_event(
            sid=(request.cookies.get("sid") or "no_session"),
            level="error",
            category="http",
            event="http_error_response",
            source=source,
            message=f"{request.path} returned {resp.status_code}.",
            request_id=request_id,
            data=_structured_http_error_data(
                endpoint=str(request.path),
                status=int(resp.status_code),
                message=message,
                request_id=request_id,
                started_at=started_at,
                error_type=error_type,
            ),
        )
        return resp

    @app.errorhandler(Exception)
    def _api_unhandled_exception(exc: Exception):
        if isinstance(exc, HTTPException):
            return exc
        if str(request.path or "").startswith("/api/"):
            sid = request.cookies.get("sid") or "no_session"
            request_id = _get_request_id()
            started_at = _get_request_started_at()
            _log_dm_event(
                sid=sid,
                level="error",
                category="http",
                event="http_unhandled_exception",
                source=_infer_event_source(endpoint=request.path),
                message="Unhandled API exception.",
                request_id=request_id,
                data=_structured_http_error_data(
                    endpoint=str(request.path),
                    status=500,
                    message=str(exc),
                    request_id=request_id,
                    started_at=started_at,
                    error_type=exc.__class__.__name__,
                ),
            )
            resp = jsonify({"ok": False, "error": "Internal server error.", "request_id": request_id})
            resp.status_code = 500
            resp.headers["X-DM-Error-Logged"] = "1"
            return resp
        return ("Internal server error.", 500)

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
        history_version = _history_version_for_sid(sid, history)
        payload = {
            "ok": True,
            "history": history,
            "history_version": history_version,
            "system_prompt": _system_prompt_for_sid(sid),
        }
        _attach_command_stop_payload(sid, payload)
        resp = jsonify(payload)
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
        event_source: Optional[str] = None,
        request_id: Optional[str] = None,
        endpoint: str = "/api/send",
        forced_source: Optional[str] = None,
    ) -> Dict[str, Any]:
        emit_used = "pipeline" if str(emit_req).lower() == "pipeline" else "none"
        stt_used, stt_edited = _extract_stt_meta(input_meta)
        turn_id = uuid.uuid4().hex
        rid = request_id or _get_request_id()
        source = _infer_event_source(
            endpoint=endpoint,
            explicit_source=event_source,
            stt_used=stt_used,
            forced_source=forced_source,
        )
        _touch_activity()

        def _log_cmdrec_route(decision_obj: RouteDecision) -> None:
            _log_dm_event(
                sid=sid,
                level="info",
                category="cmdrec",
                event="cmdrec_route",
                source=source,
                message="Command route decision computed.",
                request_id=rid,
                turn_id=turn_id,
                data={
                    "routed_as": "command" if decision_obj.is_command else "dialog",
                    "reason": decision_obj.reason,
                    "label": decision_obj.command.label if decision_obj.command else None,
                    "confidence": decision_obj.command.confidence if decision_obj.command else None,
                    "top3": decision_obj.top3,
                },
            )

        def _log_dialog_reply(reply_text: str) -> None:
            _log_dm_event(
                sid=sid,
                level="info",
                category="dialog",
                event="dialog_reply",
                source=source,
                message="Dialog reply emitted.",
                request_id=rid,
                turn_id=turn_id,
                data={"reply": reply_text, "emit_used": emit_used},
            )

        pipeline = _get_pipeline(sid)
        debug_cmdrec, cmdrec, behavior_executor = _pipeline_props(pipeline)
        runtime_cfg = _get_runtime_cfg(sid)
        _sync_runtime_dance_catalog_for_sid(
            sid,
            pipeline=pipeline,
            cmdrec_obj=cmdrec,
            runtime_cfg=runtime_cfg,
        )
        _set_behavior_executor_custom_life_management(behavior_executor, False)
        _release_custom_life_lock_if_needed_on_user_turn(sid, runtime_cfg)
        if reset:
            sessions[sid] = []
            _set_last_action(sid, None)
            _clear_command_stop_available(sid)

        history = _get_history(sid)
        _expire_pending_if_needed(sid, history)

        pending = pending_confirms.get(sid)
        if pending and cmdrec is not None:
            state = _get_cmdrec_state(sid)
            decision = cmdrec.route(text, state["mode"], state["active_behavior"])
            _log_cmdrec_route(decision)
            debug = _make_debug(decision, debug_cmdrec)

            if decision.is_command and decision.command and decision.command.label == "STOP":
                _append_user(history, text)
                pending_confirms.pop(sid, None)
                if behavior_executor:
                    _set_behavior_executor_custom_life_management(behavior_executor, False)
                    behavior_executor.execute(decision.command)
                    _execute_standup_after_stop_if_needed(
                        sid,
                        behavior_executor,
                        runtime_cfg_local=runtime_cfg,
                        source="system_auto",
                        request_id=rid,
                        turn_id=turn_id,
                    )
                _release_custom_life_lock(sid, runtime_cfg)
                _clear_command_stop_available(sid)
                _stop_nao_audio_best_effort(
                    runtime_cfg,
                    source="system_auto",
                    request_id=rid,
                    turn_id=turn_id,
                    sid=sid,
                    reason="pending_confirm_stop",
                )
                _set_cmd_state(
                    sid,
                    state,
                    mode="DIALOG",
                    active_behavior=None,
                    reason="pending_confirm_stop_executed",
                    command_label=decision.command.label,
                    text=text,
                    confidence=decision.command.confidence,
                    source=source,
                    request_id=rid,
                    turn_id=turn_id,
                )
                _set_last_action(
                    sid,
                    _format_action_summary(
                        "Uitgevoerd",
                        decision.command.label,
                        decision.command.resolved,
                    ),
                )
                _append_assistant(history, "OK. Uitgevoerd: STOP")
                _log_dm_event(
                    sid=sid,
                    level="info",
                    category="cmdrec",
                    event="command_executed",
                    source=source,
                    message="Pending confirmation STOP executed.",
                    request_id=rid,
                    turn_id=turn_id,
                    data={"command": "STOP", "confidence": decision.command.confidence},
                )
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
                _log_dialog_reply(payload.get("reply") or "")
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
            _log_dialog_reply(payload.get("reply") or "")
            return payload

        debug_for_response = None

        # If we are waiting for a dance choice, try to resolve before normal routing.
        state = _get_cmdrec_state(sid)
        if state.get("pending_dance") and cmdrec is not None:
            catalog = _dance_catalog(cmdrec, runtime_cfg=runtime_cfg)
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
                    source=source,
                    request_id=rid,
                    turn_id=turn_id,
                )
                if result is not None:
                    return result
        if cmdrec is not None:
            state = _get_cmdrec_state(sid)
            decision = cmdrec.route(text, state["mode"], state["active_behavior"])
            _log_cmdrec_route(decision)
            debug = _make_debug(decision, debug_cmdrec)
            debug_for_response = debug

            # DANCE resolved? If not, ask follow-up and mark pending.
            if decision.is_command and decision.command and decision.command.label == "DANCE":
                resolved = decision.command.resolved if isinstance(decision.command.resolved, dict) else {}
                dance_key = resolved.get("dance_key")
                catalog = _dance_catalog(cmdrec, runtime_cfg=runtime_cfg)
                if catalog and dance_key and str(dance_key).strip().lower() != "unknown":
                    key_norm = str(dance_key).strip().casefold()
                    available = any(
                        isinstance(item.get("key"), str) and item.get("key", "").strip().casefold() == key_norm
                        for item in catalog
                    )
                    if not available:
                        resolved["dance_key"] = "unknown"
                        resolved.pop("dance_behavior", None)
                        decision.command.resolved = resolved
                        dance_key = "unknown"
                if not dance_key or str(dance_key).strip().lower() == "unknown":
                    state["pending_dance"] = True
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
                source=source,
                request_id=rid,
                turn_id=turn_id,
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
        _log_dialog_reply(reply)
        return payload

    @app.post("/api/send")
    def api_send():
        payload = request.get_json(force=True, silent=True) or {}
        text = (payload.get("text") or "").strip()
        if not text:
            return jsonify({"ok": False, "error": "Lege tekst."}), 400

        emit_req = (payload.get("emit") or "pipeline")
        sid = _get_sid()
        stt_used, _stt_edited = _extract_stt_meta(payload.get("input_meta"))
        source = _infer_event_source(
            endpoint="/api/send",
            explicit_source=payload.get("source"),
            stt_used=stt_used,
        )
        request_id = _get_request_id()
        result = _process_text(
            sid,
            text,
            emit_req=emit_req,
            reset=payload.get("reset") is True,
            input_meta=payload.get("input_meta"),
            event_source=source,
            request_id=request_id,
            endpoint="/api/send",
        )
        _attach_command_stop_payload(sid, result)
        resp = jsonify(result)
        resp.headers["X-Request-Id"] = request_id
        resp.set_cookie("sid", sid, max_age=60 * 60 * 24 * 7, samesite="Lax")
        return resp

    def _continuous_eye_color_for_phase(phase: str) -> str:
        phase_norm = (phase or "").strip().lower()
        if phase_norm == "stopped":
            return "#D50000"  # explicitly stopped
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

    def _set_continuous_phase(
        sid: str,
        state: Dict[str, Any],
        phase: str,
        *,
        reason: str,
        runtime_cfg_local: Optional[JsonLike] = None,
        source: str = "speech_continuous",
        request_id: Optional[str] = None,
    ) -> bool:
        prev_phase = str(state.get("phase") or "idle")
        changed = _transition_continuous_phase(state, phase, reason=reason)
        if not changed:
            return False
        cfg_local = runtime_cfg_local if runtime_cfg_local is not None else _get_runtime_cfg(sid)
        next_phase = str(state.get("phase") or "idle")
        _set_continuous_eye_phase(cfg_local, state, next_phase)
        _log_dm_event(
            sid=sid,
            level="info",
            category="state",
            event="continuous_phase_changed",
            source=source,
            message=f"Continuous phase changed: {prev_phase} -> {next_phase}",
            request_id=request_id,
            data={"from_phase": prev_phase, "to_phase": next_phase, "reason": reason},
        )
        return True

    def _continuous_gate_phase(runtime_cfg: JsonLike, state: Dict[str, Any]) -> str:
        wake_mode = _wake_mode(runtime_cfg)
        if wake_mode == "always":
            return "listening" if bool(state.get("wake_open_once")) else "wake_listening"
        if wake_mode == "timeout":
            now_s = time.time()
            gate_open = now_s <= float(state.get("wake_open_until", 0.0) or 0.0)
            return "listening" if gate_open else "wake_listening"
        return "listening"

    @app.post("/api/continuous_start")
    def api_continuous_start():
        sid = _get_sid()
        state = _get_continuous_state(sid)
        if state.get("running"):
            resp = jsonify({"ok": True, "running": True, "error": state.get("last_error")})
            resp.set_cookie("sid", sid, max_age=60 * 60 * 24 * 7, samesite="Lax")
            return resp

        stop_event = threading.Event()
        state["stop"] = stop_event
        state["running"] = True
        state["last_error"] = None
        state["eye_phase"] = None
        state["eye_last_try"] = 0.0
        runtime_cfg = _get_runtime_cfg(sid)
        start_phase = _continuous_gate_phase(runtime_cfg, state)
        _set_continuous_phase(
            sid,
            state,
            start_phase,
            reason="continuous_start",
            runtime_cfg_local=runtime_cfg,
        )

        def _worker():
            try:
                while not stop_event.is_set():
                    runtime_cfg = _get_runtime_cfg(sid)
                    merged = _apply_runtime_overrides(base_cfg, runtime_cfg)
                    wake_mode = _wake_mode(runtime_cfg)
                    wake_timeout_s = _wake_timeout_s(runtime_cfg)
                    wake_words = _wake_words(runtime_cfg)
                    if wake_mode == "always":
                        _set_continuous_phase(
                            sid,
                            state,
                            "listening" if state.get("wake_open_once") else "wake_listening",
                            reason="wake_mode_always_gate",
                            runtime_cfg_local=runtime_cfg,
                        )
                    elif wake_mode == "timeout":
                        _set_continuous_phase(
                            sid,
                            state,
                            "listening" if (time.time() <= float(state.get("wake_open_until", 0.0) or 0.0)) else "wake_listening",
                            reason="wake_mode_timeout_gate",
                            runtime_cfg_local=runtime_cfg,
                        )
                    else:
                        _set_continuous_phase(
                            sid,
                            state,
                            "listening",
                            reason="wake_mode_never_open",
                            runtime_cfg_local=runtime_cfg,
                        )
                        state["wake_open_once"] = False
                        state["wake_open_until"] = 0.0
                    try:
                        mic = _make_mic_from_config(merged, mode="continuous")
                    except Exception as exc:
                        state["last_error"] = str(exc)
                        _set_continuous_phase(
                            sid,
                            state,
                            "idle",
                            reason="mic_init_error",
                            runtime_cfg_local=runtime_cfg,
                        )
                        time.sleep(1.0)
                        continue

                    timeout_s = _resolve_start_timeout(
                        (merged.get("input", {}).get("params") or {}),
                        default=10**9,
                    )
                    if wake_mode == "always":
                        gate_open_for_capture = bool(state.get("wake_open_once"))
                    elif wake_mode == "timeout":
                        gate_open_for_capture = time.time() <= float(state.get("wake_open_until", 0.0) or 0.0)
                    else:
                        gate_open_for_capture = True
                    timeout_s = _continuous_capture_timeout_s(
                        timeout_s,
                        wake_mode=wake_mode,
                        gate_open=gate_open_for_capture,
                        wake_open_until=float(state.get("wake_open_until", 0.0) or 0.0),
                    )
                    try:
                        audio = mic.capture_utterance(timeout_s=timeout_s)
                    except TimeoutError:
                        timeout_phase = _continuous_gate_phase(runtime_cfg, state)
                        _set_continuous_phase(
                            sid,
                            state,
                            timeout_phase,
                            reason="capture_timeout_gate_open" if timeout_phase == "listening" else "capture_timeout_wait_wake",
                            runtime_cfg_local=runtime_cfg,
                        )
                        continue
                    except Exception as exc:
                        state["last_error"] = str(exc)
                        _set_continuous_phase(
                            sid,
                            state,
                            "idle",
                            reason="capture_error",
                            runtime_cfg_local=runtime_cfg,
                        )
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
                        _set_continuous_phase(
                            sid,
                            state,
                            "wake_listening",
                            reason="gate_closed_wait_wake",
                            runtime_cfg_local=runtime_cfg,
                        )
                        wake_key = (tuple([_normalize_text(w) for w in wake_words]),)
                        if state.get("wake_stt_key") != wake_key or state.get("wake_stt") is None:
                            try:
                                state["wake_stt"] = _build_wake_stt(merged, wake_words)
                                state["wake_stt_key"] = wake_key
                            except Exception as exc:
                                state["last_error"] = "wake-stt: " + str(exc)
                                _set_continuous_phase(
                                    sid,
                                    state,
                                    "idle",
                                    reason="wake_stt_build_error",
                                    runtime_cfg_local=runtime_cfg,
                                )
                                time.sleep(0.5)
                                continue
                        try:
                            wake_res = state["wake_stt"].transcribe(audio)
                            wake_text = (wake_res.text or "").strip()
                            state["last_wake_text"] = wake_text
                        except Exception as exc:
                            state["last_error"] = "wake-stt: " + str(exc)
                            _set_continuous_phase(
                                sid,
                                state,
                                "idle",
                                reason="wake_stt_transcribe_error",
                                runtime_cfg_local=runtime_cfg,
                            )
                            continue
                        if not _text_has_wake_word(wake_text, wake_words):
                            _set_continuous_phase(
                                sid,
                                state,
                                "wake_listening",
                                reason="wake_word_not_detected",
                                runtime_cfg_local=runtime_cfg,
                            )
                            continue
                        state["last_error"] = None
                        if wake_mode == "always":
                            state["wake_open_once"] = True
                        else:
                            state["wake_open_until"] = time.time() + float(wake_timeout_s)
                        _set_continuous_phase(
                            sid,
                            state,
                            "listening",
                            reason="wake_word_detected",
                            runtime_cfg_local=runtime_cfg,
                        )
                        continue

                    _set_continuous_phase(
                        sid,
                        state,
                        "transcribing",
                        reason="stt_transcribe_start",
                        runtime_cfg_local=runtime_cfg,
                    )
                    try:
                        res = _get_stt(sid).transcribe(audio)
                    except Exception as exc:
                        state["last_error"] = str(exc)
                        _set_continuous_phase(
                            sid,
                            state,
                            "idle",
                            reason="stt_transcribe_error",
                            runtime_cfg_local=runtime_cfg,
                        )
                        continue

                    text_in = (res.text or "").strip()
                    _set_continuous_phase(
                        sid,
                        state,
                        "thinking",
                        reason="pipeline_processing",
                        runtime_cfg_local=runtime_cfg,
                    )
                    if not text_in:
                        if wake_mode == "timeout" and float(state.get("wake_open_until", 0.0) or 0.0) < time.time():
                            _set_continuous_phase(
                                sid,
                                state,
                                "wake_listening",
                                reason="empty_input_gate_closed",
                                runtime_cfg_local=runtime_cfg,
                            )
                        continue

                    try:
                        with pipeline_lock:
                            _process_text(
                                sid,
                                text_in,
                                emit_req="pipeline",
                                reset=False,
                                input_meta={"stt_used": True, "stt_edited": False},
                                endpoint="/api/continuous_start",
                                forced_source="speech_continuous",
                                request_id=_new_request_id(),
                            )
                        if wake_mode == "always":
                            state["wake_open_once"] = False
                            _set_continuous_phase(
                                sid,
                                state,
                                "wake_listening",
                                reason="turn_done_wait_wake",
                                runtime_cfg_local=runtime_cfg,
                            )
                        elif wake_mode == "timeout":
                            state["wake_open_until"] = time.time() + float(wake_timeout_s)
                            _set_continuous_phase(
                                sid,
                                state,
                                "listening",
                                reason="turn_done_gate_open",
                                runtime_cfg_local=runtime_cfg,
                            )
                        else:
                            _set_continuous_phase(
                                sid,
                                state,
                                "listening",
                                reason="turn_done_listening",
                                runtime_cfg_local=runtime_cfg,
                            )
                        state["last_error"] = None
                    except Exception as exc:
                        state["last_error"] = str(exc)
                        _set_continuous_phase(
                            sid,
                            state,
                            "idle",
                            reason="pipeline_error",
                            runtime_cfg_local=runtime_cfg,
                        )
                        continue
            finally:
                state["running"] = False
                state["wake_open_once"] = False
                state["wake_open_until"] = 0.0
                state["last_wake_text"] = ""
                if state.get("phase") != "stopped":
                    _set_continuous_phase(sid, state, "idle", reason="worker_exit")

        t = threading.Thread(target=_worker, daemon=True)
        state["thread"] = t
        t.start()
        resp = jsonify({"ok": True, "running": True})
        resp.set_cookie("sid", sid, max_age=60 * 60 * 24 * 7, samesite="Lax")
        return resp

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
        _set_continuous_phase(
            sid,
            state,
            "stopped",
            reason="manual_stop",
            runtime_cfg_local=runtime_cfg,
        )
        resp = jsonify({"ok": True, "running": False})
        resp.set_cookie("sid", sid, max_age=60 * 60 * 24 * 7, samesite="Lax")
        return resp

    @app.get("/api/continuous_state")
    def api_continuous_state():
        sid = _get_sid()
        state = _get_continuous_state(sid)
        _ensure_continuous_phase_meta(state)
        out = {
            "ok": True,
            "running": bool(state.get("running")),
            "error": state.get("last_error"),
            "phase": state.get("phase"),
            "phase_reason": state.get("phase_reason"),
            "phase_seq": int(state.get("phase_seq", 0)),
            "phase_changed_at": float(state.get("phase_changed_at") or 0.0),
            "phase_log": list(state.get("phase_log") or []),
            "last_wake_text": state.get("last_wake_text", ""),
        }
        _attach_command_stop_payload(sid, out)
        resp = jsonify(out)
        resp.set_cookie("sid", sid, max_age=60 * 60 * 24 * 7, samesite="Lax")
        return resp

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
            reviewed_entries = _dedupe_al_text_entries(_load_jsonl(reviewed_path), keep="newest")
            if reviewed_entries:
                _write_jsonl(reviewed_entries, reviewed_path)
            reviewed_count = len(reviewed_entries)
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
        train_base_rel = _active_train_base_relpath()
        gold_rel = _active_gold_relpath()
        _append_retrain_log(f"INFO: Retrain datasets: train_base={train_base_rel} gold={gold_rel}")
        cmd = [
            _cmdrec_train_python(),
            "tools/retrain_cmdrec.py",
            "--train-base",
            train_base_rel,
            "--gold",
            gold_rel,
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
        entries = _dedupe_al_text_entries(entries, keep="newest")
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
            source_entries = _dedupe_al_text_entries(source_entries, keep="newest")
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
            queue_entries = _dedupe_al_text_entries(queue_entries, keep="newest")
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
            reviewed_entries = _dedupe_al_text_entries(reviewed_entries, keep="newest")
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
                gold_entries = _dedupe_al_text_entries(gold_entries, keep="newest")
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
            queue_entries = _dedupe_al_text_entries(queue_entries, keep="newest")
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
                reviewed_entries = _dedupe_al_text_entries(reviewed_entries, keep="newest")
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
                    gold_entries = _dedupe_al_text_entries(gold_entries, keep="newest")
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
        runtime_cfg = _get_runtime_cfg(sid)
        pipeline = _get_pipeline(sid)
        _, _, behavior_executor = _pipeline_props(pipeline)
        _set_behavior_executor_custom_life_management(behavior_executor, False)
        stt_used, stt_edited = _extract_stt_meta(pending.get("input_meta"))
        confirm_request_id = _get_request_id()

        if confirmed:
            _log_state_event(
                sid,
                event="confirm_received",
                reason="approved",
                command_label=cmd.label,
                text=pending.get("input_text"),
                confidence=pending.get("confidence") or cmd.confidence,
            )
            lock_mode = _custom_life_lock_mode_for_command(cmd)
            if lock_mode:
                _acquire_custom_life_lock(sid, runtime_cfg, lock_mode)
            if behavior_executor:
                behavior_executor.execute(cmd)
                if _normalize_command_label(cmd.label) == "STOP":
                    _execute_standup_after_stop_if_needed(
                        sid,
                        behavior_executor,
                        runtime_cfg_local=runtime_cfg,
                        source="system_auto",
                        request_id=confirm_request_id,
                    )
            if _normalize_command_label(cmd.label) == "STOP":
                _release_custom_life_lock(sid, runtime_cfg)
                _clear_command_stop_available(sid)
                _stop_nao_audio_best_effort(
                    runtime_cfg,
                    source="system_auto",
                    request_id=confirm_request_id,
                    sid=sid,
                    reason="confirm_stop",
                )
            elif lock_mode in _CUSTOM_LIFE_LOCK_MODES:
                _set_command_stop_available(sid, cmd.label, scope=lock_mode)
            else:
                _clear_command_stop_available(sid)
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
            _log_state_event(
                sid,
                event="confirm_done",
                reason="approved",
                command_label=cmd.label,
            )
        else:
            _log_state_event(
                sid,
                event="confirm_received",
                reason="declined",
                command_label=cmd.label,
                text=pending.get("input_text"),
                confidence=pending.get("confidence") or cmd.confidence,
            )
            if _normalize_command_label(cmd.label) == "STOP":
                _clear_command_stop_available(sid)
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

        out = {
            "ok": True,
            "history": history,
            "system_prompt": _system_prompt_for_sid(sid),
        }
        _attach_command_stop_payload(sid, out)
        resp = jsonify(out)
        resp.set_cookie("sid", sid, max_age=60 * 60 * 24 * 7, samesite="Lax")
        return resp

    @app.post("/api/reset")
    def api_reset():
        sid = _get_sid()
        _summary_cleanup_sid(sid)
        sessions[sid] = []
        _set_last_action(sid, None)
        _clear_command_stop_available(sid)
        payload = {"ok": True, "history": [], "system_prompt": _system_prompt_for_sid(sid)}
        _attach_command_stop_payload(sid, payload)
        resp = jsonify(payload)
        resp.set_cookie("sid", sid, max_age=60 * 60 * 24 * 7, samesite="Lax")
        return resp

    _audio_probe_lock = threading.Lock()
    _audio_probe_cache: Dict[str, Any] = {"ts": 0.0, "snapshot": None}

    def _probe_audio_snapshot(timeout_s: float = 2.0) -> Optional[Dict[str, Any]]:
        now = time.time()
        with _audio_probe_lock:
            cached = _audio_probe_cache.get("snapshot")
            if cached is not None and (now - float(_audio_probe_cache.get("ts", 0.0))) < 0.75:
                return cached
        probe_code = (
            "import json\n"
            "import sounddevice as sd\n"
            "raw = sd.query_devices()\n"
            "default_device = list(sd.default.device) if sd.default.device else [None, None]\n"
            "out = {'default_hostapi': sd.default.hostapi, 'default_device': default_device, 'devices': []}\n"
            "for idx, d in enumerate(raw):\n"
            "    out['devices'].append({"
            "'index': idx, "
            "'name': d.get('name', ''), "
            "'hostapi': d.get('hostapi'), "
            "'max_input_channels': d.get('max_input_channels', 0), "
            "'max_output_channels': d.get('max_output_channels', 0)"
            "})\n"
            "print(json.dumps(out, ensure_ascii=False))\n"
        )
        try:
            out = subprocess.check_output(
                [sys.executable, "-c", probe_code],
                stderr=subprocess.DEVNULL,
                timeout=timeout_s,
                text=True,
            )
            snap = json.loads(out.strip())
            if not isinstance(snap, dict):
                return None
            with _audio_probe_lock:
                _audio_probe_cache["snapshot"] = snap
                _audio_probe_cache["ts"] = time.time()
            return snap
        except Exception:
            return None

    def _list_audio_devices() -> Dict[str, Any]:
        if sd is None:
            return {"devices": [], "default_input": None}
        devices = []
        try:
            snap = _probe_audio_snapshot()
            if snap:
                raw = snap.get("devices", [])
                default_hostapi = snap.get("default_hostapi")
                default_device = snap.get("default_device") or [None, None]
                default_in = default_device[0] if len(default_device) > 0 else None
            else:
                raw = sd.query_devices()
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
            snap = _probe_audio_snapshot()
            if snap:
                raw = snap.get("devices", [])
                default_hostapi = snap.get("default_hostapi")
                default_device = snap.get("default_device") or [None, None]
                default_out = default_device[1] if len(default_device) > 1 else None
            else:
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

    def _master_prompts_dir() -> str:
        override = str(os.environ.get("DM_MASTER_PROMPTS_DIR", "") or "").strip()
        if override:
            return os.path.abspath(override)
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        return os.path.join(repo_root, "py3_dialog_manager", "configs", "master_prompts")

    def _normalize_master_prompt_name(raw: Any) -> Optional[str]:
        name = str(raw or "").strip().replace("\\", "/")
        if not name:
            return None
        if name.startswith("master_prompts/"):
            name = name[len("master_prompts/") :]
        name = os.path.basename(name).strip()
        if not name or name in (".", "..") or "/" in name or "\\" in name:
            return None
        if not name.lower().endswith(".txt"):
            name = f"{name}.txt"
        return f"master_prompts/{name}"

    def _master_prompt_abs_path(display_name: str) -> str:
        rel = display_name[len("master_prompts/") :]
        return os.path.join(_master_prompts_dir(), rel)

    @app.get("/api/master_prompt_content")
    def api_master_prompt_content():
        display_name = _normalize_master_prompt_name(request.args.get("name"))
        if not display_name:
            return jsonify({"ok": False, "error": "prompt naam ontbreekt of ongeldig"}), 400
        abs_path = _master_prompt_abs_path(display_name)
        if not os.path.exists(abs_path):
            return jsonify({"ok": False, "error": "prompt bestaat niet"}), 404
        try:
            with open(abs_path, "r", encoding="utf-8", errors="replace") as fh:
                content = fh.read()
        except OSError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 500
        return jsonify({"ok": True, "name": display_name, "content": content})

    @app.post("/api/master_prompt_save")
    def api_master_prompt_save():
        payload = request.get_json(force=True, silent=True) or {}
        content_raw = payload.get("content")
        if content_raw is None:
            return jsonify({"ok": False, "error": "content ontbreekt"}), 400
        content = str(content_raw)
        current_name = _normalize_master_prompt_name(payload.get("name"))
        save_as_name = _normalize_master_prompt_name(payload.get("save_as"))
        target_name = save_as_name or current_name
        if not target_name:
            return jsonify({"ok": False, "error": "prompt naam ontbreekt"}), 400

        prompts_dir = _master_prompts_dir()
        try:
            os.makedirs(prompts_dir, exist_ok=True)
        except OSError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 500

        target_path = os.path.abspath(_master_prompt_abs_path(target_name))
        prompts_root = os.path.normcase(os.path.abspath(prompts_dir))
        target_norm = os.path.normcase(target_path)
        if not target_norm.startswith(prompts_root + os.sep):
            return jsonify({"ok": False, "error": "ongeldige prompt locatie"}), 400
        try:
            with open(target_path, "w", encoding="utf-8") as fh:
                fh.write(content)
        except OSError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 500

        items: List[str] = []
        try:
            for name in sorted(os.listdir(prompts_dir)):
                if name.startswith(".") or not name.lower().endswith(".txt"):
                    continue
                items.append("master_prompts/" + name)
        except OSError:
            pass
        return jsonify({"ok": True, "name": target_name, "prompts": items})

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

    def _split_ollama_models() -> Tuple[List[str], List[str], Optional[str]]:
        if not shutil.which("ollama"):
            return [], [], "ollama cli niet gevonden"
        try:
            proc = subprocess.run(
                ["ollama", "list"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            lines = (proc.stdout or "").splitlines()
            local_models: List[str] = []
            cloud_models: List[str] = []
            seen_local = set()
            seen_cloud = set()
            for line in lines[1:]:
                parts = line.split()
                if not parts:
                    continue
                name = str(parts[0] or "").strip()
                if not name:
                    continue
                key = name.lower()
                if key.endswith("cloud"):
                    if key in seen_cloud:
                        continue
                    seen_cloud.add(key)
                    cloud_models.append(name)
                else:
                    if key in seen_local:
                        continue
                    seen_local.add(key)
                    local_models.append(name)
            return local_models, cloud_models, None
        except Exception as exc:
            return [], [], str(exc)

    @app.get("/api/ollama_models_local")
    def api_ollama_models_local():
        local_models, _cloud_models, err = _split_ollama_models()
        payload = {"ok": True, "models": local_models}
        if err:
            payload["error"] = err
        return jsonify(payload)

    @app.get("/api/ollama_models_cloud")
    def api_ollama_models_cloud():
        _local_models, cloud_models, err = _split_ollama_models()
        payload = {"ok": True, "models": cloud_models}
        if err:
            payload["error"] = err
        return jsonify(payload)

    @app.get("/api/runtime_config")
    def api_runtime_config():
        sid = _get_sid()
        cfg_out = _get_runtime_cfg(sid)
        resp = jsonify(
            {
                "ok": True,
                "config": cfg_out,
                "system_prompt": _system_prompt_for_sid(sid),
                "instance_id": resolved_instance_id,
            }
        )
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
                "instance_id": resolved_instance_id,
            }
        )
        resp.set_cookie("sid", sid, max_age=60 * 60 * 24 * 7, samesite="Lax")
        return resp

    @app.get("/api/runtime_defaults")
    def api_runtime_defaults():
        cfg_out = _extract_runtime_config(base_cfg)
        merged = _apply_runtime_overrides(base_cfg, cfg_out)
        return jsonify({"ok": True, "config": cfg_out, "effective_config": merged, "instance_id": resolved_instance_id})

    @app.post("/api/runtime_config")
    def api_runtime_config_set():
        payload = request.get_json(force=True, silent=True) or {}
        sid = _get_sid()
        source = _infer_event_source(endpoint="/api/runtime_config", explicit_source=payload.get("source"))
        request_id = _get_request_id()
        before_cfg = _runtime_cfg_snapshot()
        if _request_ui_mode() == "client":
            cfg_out = _get_runtime_cfg(sid)
            resp = jsonify(
                {
                    "ok": True,
                    "config": cfg_out,
                    "system_prompt": _system_prompt_for_sid(sid),
                    "instance_id": resolved_instance_id,
                    "client_ignored": True,
                }
            )
            resp.headers["X-Request-Id"] = request_id
            resp.set_cookie("sid", sid, max_age=60 * 60 * 24 * 7, samesite="Lax")
            return resp

        if payload.get("reset"):
            with runtime_cfg_lock:
                runtime_cfg_global.clear()
                runtime_cfg_global.update(copy.deepcopy(runtime_cfg_default))
                _sanitize_runtime_cfg_inplace(runtime_cfg_global)
            _save_runtime_cfg_state()
            _summary_cleanup_all()
            pipeline_by_sid.pop(sid, None)
            stt_by_sid.pop(sid, None)
            pipeline_by_sid.clear()
            stt_by_sid.clear()
            with behavior_catalog_cache_lock:
                behavior_catalog_cache.clear()
            cfg_out = _get_runtime_cfg(sid)
            if not cfg_out.get("custom_life_enabled", False):
                _release_custom_life_lock(sid, cfg_out)
            _log_runtime_cfg_change(
                sid=sid,
                before=before_cfg,
                after=cfg_out,
                source=source,
                request_id=request_id,
                reason="runtime_config_reset",
            )
            resp = jsonify(
                {
                    "ok": True,
                    "config": cfg_out,
                    "system_prompt": _system_prompt_for_sid(sid),
                    "instance_id": resolved_instance_id,
                }
            )
            resp.headers["X-Request-Id"] = request_id
            resp.set_cookie("sid", sid, max_age=60 * 60 * 24 * 7, samesite="Lax")
            return resp

        candidate = _runtime_cfg_snapshot()
        candidate.update(payload.get("config", payload))
        _sanitize_runtime_cfg_inplace(candidate)

        if candidate.get("base_enabled", True):
            llm_type = (candidate.get("llm_type") or "").lower()
            llm_model = (candidate.get("llm_model") or "").strip()
            if llm_type != "echo" and not llm_model:
                return jsonify({"ok": False, "error": "LLM model ontbreekt."}), 400

        web_host, web_port = _host_header_host_port(request.host)
        if _url_conflicts_with_webapp(candidate.get("nao_base_url"), request.host):
            port_hint = f":{web_port}" if web_port is not None else ""
            return jsonify(
                {
                    "ok": False,
                    "error": (
                        f"nao_base_url wijst naar de dialog manager zelf (poort {port_hint or 'onbekend'}). "
                        "Kies een andere poort voor de base controller."
                    ),
                    "field": "nao_base_url",
                    "web_host": web_host,
                    "web_port": web_port,
                }
            ), 400
        if _url_conflicts_with_webapp(candidate.get("behavior_manager_url"), request.host):
            port_hint = f":{web_port}" if web_port is not None else ""
            return jsonify(
                {
                    "ok": False,
                    "error": (
                        f"behavior_manager_url wijst naar de dialog manager zelf (poort {port_hint or 'onbekend'}). "
                        "Kies een andere poort voor de behavior manager."
                    ),
                    "field": "behavior_manager_url",
                    "web_host": web_host,
                    "web_port": web_port,
                }
            ), 400

        merged = _apply_runtime_overrides(base_cfg, candidate)
        with runtime_cfg_lock:
            runtime_cfg_global.clear()
            runtime_cfg_global.update(candidate)
        _save_runtime_cfg_state()

        sid_keys = set(pipeline_by_sid.keys())
        sid_keys.add(sid)
        for sid_key in sid_keys:
            _rebuild_pipeline_for_sid(sid_key, merged)
        with behavior_catalog_cache_lock:
            behavior_catalog_cache.clear()
        runtime_cfg = _get_runtime_cfg(sid)
        _apply_custom_life(sid, runtime_cfg)
        if not runtime_cfg.get("custom_life_enabled", False):
            _release_custom_life_lock(sid, runtime_cfg)
        _log_runtime_cfg_change(
            sid=sid,
            before=before_cfg,
            after=runtime_cfg,
            source=source,
            request_id=request_id,
            reason="runtime_config_apply",
        )
        resp = jsonify(
            {
                "ok": True,
                "config": runtime_cfg,
                "system_prompt": _system_prompt_for_sid(sid),
                "effective_config": merged,
                "output_enabled": bool(runtime_cfg.get("base_enabled", True)),
                "instance_id": resolved_instance_id,
            }
        )
        resp.headers["X-Request-Id"] = request_id
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
        nao_ping = _check_tcp(nao_host, nao_port) if nao_ip_enabled else False
        # Only probe /is_awake when NAO checks are explicitly enabled and TCP is up.
        probe_nao_via_services = bool(nao_ip_enabled and nao_ping)
        base_conflict = _url_conflicts_with_webapp(base_url, request.host) if base_enabled else False
        behavior_conflict = _url_conflicts_with_webapp(behavior_url, request.host) if behavior_enabled else False
        base_ping = _check_ping(base_url, "/ping") if (base_enabled and not base_conflict) else False
        base_nao_ping = _check_ping(base_url, "/is_awake") if (base_enabled and base_ping and probe_nao_via_services) else False
        behavior_ping = _check_ping(behavior_url, "/ping") if (behavior_enabled and not behavior_conflict) else False
        behavior_nao_ping = _check_ping(behavior_url + "/nao" if behavior_url else None, "/is_awake") if (
            behavior_enabled and behavior_ping and probe_nao_via_services
        ) else False
        return jsonify(
            {
                "ok": True,
                "base": {"ping": base_ping, "nao_ping": base_nao_ping, "conflict": base_conflict},
                "behavior": {"ping": behavior_ping, "nao_ping": behavior_nao_ping, "conflict": behavior_conflict},
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

    def _stop_nao_audio_best_effort(
        runtime_cfg: JsonLike,
        *,
        source: str = "system_auto",
        request_id: Optional[str] = None,
        turn_id: Optional[str] = None,
        sid: Optional[str] = None,
        reason: Optional[str] = None,
    ) -> Dict[str, Any]:
        sid_value = str(sid or "system")
        _log_dm_event(
            sid=sid_value,
            level="info",
            category="nao",
            event="nao_stop_audio_start",
            source=source,
            message="Stopping NAO audio.",
            request_id=request_id,
            turn_id=turn_id,
            data={"reason": reason or "stop_audio"},
        )
        base_url, mode = _resolve_nao_audio_url(runtime_cfg)
        if not base_url:
            out = {"ok": False, "mode": mode, "error": "NAO audio endpoint ontbreekt."}
            _log_dm_event(
                sid=sid_value,
                level="error",
                category="nao",
                event="nao_stop_audio_result",
                source=source,
                message="NAO audio stop failed: missing endpoint.",
                request_id=request_id,
                turn_id=turn_id,
                data={"ok": False, "mode": mode, "error": out["error"]},
            )
            return out
        try:
            url = base_url + "/nao/stop_audio" if mode == "behavior" else base_url + "/stop_audio"
            resp = requests.post(url, json={}, timeout=3.0)
            data = resp.json() if resp.ok else {}
        except Exception as exc:
            out = {"ok": False, "mode": mode, "error": str(exc)}
            _log_dm_event(
                sid=sid_value,
                level="error",
                category="nao",
                event="nao_stop_audio_result",
                source=source,
                message="NAO audio stop request failed.",
                request_id=request_id,
                turn_id=turn_id,
                data={"ok": False, "mode": mode, "error": str(exc), "type": exc.__class__.__name__},
            )
            return out

        if isinstance(data, dict):
            status = (data.get("status") or "").strip().lower()
            if status == "error":
                out = {"ok": False, "mode": mode, "error": data.get("error") or "stop_audio failed"}
                _log_dm_event(
                    sid=sid_value,
                    level="error",
                    category="nao",
                    event="nao_stop_audio_result",
                    source=source,
                    message="NAO audio stop returned error status.",
                    request_id=request_id,
                    turn_id=turn_id,
                    data={
                        "ok": False,
                        "mode": mode,
                        "error": out["error"],
                        "status": status,
                        "http_status": getattr(resp, "status_code", None),
                    },
                )
                return out
            payload = data.get("data") if isinstance(data.get("data"), dict) else {}
            out = {"ok": True, "mode": mode, "status": status or "ok", "details": payload}
            _log_dm_event(
                sid=sid_value,
                level="info",
                category="nao",
                event="nao_stop_audio_result",
                source=source,
                message="NAO audio stopped.",
                request_id=request_id,
                turn_id=turn_id,
                data={"ok": True, "mode": mode, "status": out["status"]},
            )
            return out
        out = {"ok": True, "mode": mode}
        _log_dm_event(
            sid=sid_value,
            level="info",
            category="nao",
            event="nao_stop_audio_result",
            source=source,
            message="NAO audio stop completed.",
            request_id=request_id,
            turn_id=turn_id,
            data={"ok": True, "mode": mode},
        )
        return out

    def _iter_nao_motion_candidates(runtime_cfg: JsonLike) -> List[Tuple[str, str]]:
        behavior_url = _normalize_url(runtime_cfg.get("behavior_manager_url"))
        base_url = _normalize_url(runtime_cfg.get("nao_base_url"))
        behavior_enabled = bool(runtime_cfg.get("behavior_enabled", True))
        base_enabled = bool(runtime_cfg.get("base_enabled", True))
        candidates: List[Tuple[str, str]] = []
        if behavior_enabled and behavior_url:
            candidates.append((behavior_url, "behavior"))
        if base_enabled and base_url:
            candidates.append((base_url, "base"))
        return candidates

    def _naoqi_call_on_endpoint(
        endpoint_base_url: str,
        mode: str,
        *,
        module: str,
        method: str,
        args: Optional[List[Any]] = None,
        kwargs: Optional[Dict[str, Any]] = None,
        timeout_s: float = 3.0,
    ) -> Dict[str, Any]:
        path = "/nao/naoqi/call" if mode == "behavior" else "/naoqi/call"
        url = endpoint_base_url.rstrip("/") + path
        payload = {
            "module": module,
            "method": method,
            "args": list(args or []),
            "kwargs": dict(kwargs or {}),
        }
        resp = requests.post(url, json=payload, timeout=timeout_s)
        if not resp.ok:
            body = (resp.text or "").strip()
            raise RuntimeError(f"{method} failed: upstream status {resp.status_code} ({body or 'no body'})")
        data = resp.json() if resp.content else {}
        if isinstance(data, dict):
            status = str(data.get("status") or "").strip().lower()
            if status == "error":
                raise RuntimeError(str(data.get("error") or f"{method} failed"))
            return data
        return {}

    def _stop_move_on_endpoint(
        endpoint_base_url: str,
        mode: str,
        *,
        timeout_s: float = 3.0,
    ) -> Dict[str, Any]:
        path = "/nao/stop_move" if mode == "behavior" else "/stop_move"
        url = endpoint_base_url.rstrip("/") + path
        resp = requests.post(url, json={}, timeout=timeout_s)
        if not resp.ok:
            body = (resp.text or "").strip()
            raise RuntimeError(f"stop_move failed: upstream status {resp.status_code} ({body or 'no body'})")
        data = resp.json() if resp.content else {}
        if isinstance(data, dict):
            status = str(data.get("status") or "").strip().lower()
            if status == "error":
                raise RuntimeError(str(data.get("error") or "stop_move failed"))
            return data
        return {}

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

    def _get_nao_awake_state(runtime_cfg: JsonLike) -> Dict[str, Any]:
        base_url, mode = _resolve_nao_audio_url(runtime_cfg)
        if not base_url:
            return {"ok": False, "error": "NAO endpoint ontbreekt."}
        try:
            url = base_url + "/nao/is_awake" if mode == "behavior" else base_url + "/is_awake"
            resp = requests.get(url, timeout=3.0)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

        if not isinstance(data, dict):
            return {"ok": False, "error": "invalid response"}
        status = str(data.get("status") or "").strip().lower()
        if status and status != "ok":
            return {"ok": False, "error": data.get("error") or "is_awake failed"}
        payload = data.get("data") if isinstance(data.get("data"), dict) else data
        if not isinstance(payload, dict) or "is_awake" not in payload:
            return {"ok": False, "error": "missing is_awake"}
        return {"ok": True, "is_awake": bool(payload.get("is_awake")), "mode": mode}

    def _set_nao_awake_state(runtime_cfg: JsonLike, awake: bool) -> Dict[str, Any]:
        base_url, mode = _resolve_nao_audio_url(runtime_cfg)
        if not base_url:
            return {"ok": False, "error": "NAO endpoint ontbreekt."}
        action = "wake_up" if awake else "rest"
        try:
            url = base_url + ("/nao/" + action if mode == "behavior" else "/" + action)
            resp = requests.post(url, json={}, timeout=8.0)
            resp.raise_for_status()
            data = resp.json() if resp.content else {}
        except Exception as exc:
            # Wake/rest kan op echte robot soms langer duren; behandel timeouts als "mogelijk nog bezig"
            # zodat de UI niet direct een harde fout toont.
            if isinstance(exc, requests.exceptions.Timeout):
                return {"ok": True, "pending": True, "is_awake": None, "mode": mode}
            msg = str(exc)
            if "read timed out" in msg.lower():
                return {"ok": True, "pending": True, "is_awake": None, "mode": mode}
            return {"ok": False, "error": str(exc)}

        if isinstance(data, dict):
            status = str(data.get("status") or "").strip().lower()
            if status and status != "ok":
                return {"ok": False, "error": data.get("error") or ("%s failed" % action)}

        state = _get_nao_awake_state(runtime_cfg)
        if not state.get("ok"):
            return {"ok": True, "is_awake": bool(awake), "mode": mode}
        return {"ok": True, "is_awake": bool(state.get("is_awake")), "mode": mode}

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

    def _get_nao_custom_life_ui_state(runtime_cfg: JsonLike) -> Dict[str, Any]:
        enabled = bool(runtime_cfg.get("custom_life_enabled", False))
        state_resp = _get_nao_custom_life_state(runtime_cfg)
        if not state_resp.get("ok"):
            return {"ok": False, "enabled": enabled, "error": state_resp.get("error")}
        state = state_resp.get("state") if isinstance(state_resp.get("state"), dict) else {}
        life_state = state.get("life_state")
        modules = state.get("modules") if isinstance(state.get("modules"), dict) else {}
        active_modules = any(bool(modules.get(key)) for key in ("basic_awareness", "background_movement", "breathing"))
        return {
            "ok": True,
            "enabled": enabled,
            "life_state": life_state,
            "active_modules": active_modules,
            "state": state,
        }

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
                    "ui_color_scheme": "default",
                    "llm_temperature": None,
                    "llm_top_p": None,
                    "llm_top_k": None,
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
                    "ui_color_scheme": "default",
                    "llm_temperature": None,
                    "llm_top_p": None,
                    "llm_top_k": None,
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
                    "llm_model": "gpt-oss:120b-cloud",
                    "ui_color_scheme": "default",
                    "llm_temperature": None,
                    "llm_top_p": None,
                    "llm_top_k": None,
                    "master_prompt_file": "master_prompts/atlantis_Alex.txt",
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

    @app.post("/api/nao_stop_audio")
    def api_nao_stop_audio():
        payload = request.get_json(force=True, silent=True) or {}
        sid = _get_sid()
        runtime_cfg = _get_runtime_cfg(sid)
        source = _infer_event_source(endpoint="/api/nao_stop_audio", explicit_source=payload.get("source"))
        request_id = _get_request_id()
        result = _stop_nao_audio_best_effort(
            runtime_cfg,
            source=source,
            request_id=request_id,
            sid=sid,
            reason="manual_nao_stop_audio",
        )
        if not result.get("ok"):
            code = 400 if "ontbreekt" in str(result.get("error") or "").lower() else 502
            resp = jsonify({"ok": False, "error": result.get("error"), "mode": result.get("mode")})
            resp.status_code = code
            resp.headers["X-Request-Id"] = request_id
            return resp
        resp = jsonify({"ok": True, "mode": result.get("mode"), "status": result.get("status", "ok"), "details": result.get("details") or {}})
        resp.headers["X-Request-Id"] = request_id
        return resp

    @app.post("/api/nao_move_toward")
    def api_nao_move_toward():
        payload = request.get_json(force=True, silent=True) or {}
        sid = _get_sid()
        runtime_cfg = _get_runtime_cfg(sid)
        runtime_state = _get_runtime_state(sid)
        source = _infer_event_source(endpoint="/api/nao_move_toward", explicit_source=payload.get("source"))
        request_id = _get_request_id()

        def _move_resp(body: Dict[str, Any], status_code: int = 200):
            level = "error" if status_code >= 400 or not bool(body.get("ok", False)) else "info"
            _log_dm_event(
                sid=sid,
                level=level,
                category="nao",
                event="nao_move_toward_result",
                source=source,
                message="NAO moveToward result.",
                request_id=request_id,
                data={
                    "ok": bool(body.get("ok")),
                    "status": status_code,
                    "ignored": bool(body.get("ignored", False)),
                    "mode": body.get("mode"),
                    "error": body.get("error"),
                    "applied": body.get("applied"),
                    "seq": body.get("seq"),
                },
            )
            resp = jsonify(body)
            resp.status_code = status_code
            resp.headers["X-Request-Id"] = request_id
            return resp

        def _parse_required_axis(name: str) -> float:
            if name not in payload:
                raise ValueError(f"Missing '{name}'.")
            try:
                value = float(payload.get(name))
            except Exception:
                raise ValueError(f"Ongeldige '{name}' waarde.")
            if math.isnan(value) or math.isinf(value):
                raise ValueError(f"Ongeldige '{name}' waarde.")
            value = max(-1.0, min(1.0, value))
            return float(value)

        raw_seq = payload.get("seq", None)
        seq: Optional[int] = None
        if raw_seq is not None:
            try:
                seq = int(raw_seq)
            except Exception:
                return _move_resp({"ok": False, "error": "Ongeldige 'seq' waarde."}, 400)
            try:
                last_seq_raw = runtime_state.get("motion_last_seq", None)
                last_seq = int(last_seq_raw) if last_seq_raw is not None else None
            except Exception:
                last_seq = None
            if last_seq is not None and seq <= last_seq:
                return _move_resp({"ok": True, "ignored": True, "seq": seq, "mode": "stale"})
            runtime_state["motion_last_seq"] = seq

        try:
            x = _parse_required_axis("x")
            y = _parse_required_axis("y")
            theta = _parse_required_axis("theta")
        except ValueError as exc:
            return _move_resp({"ok": False, "error": str(exc)}, 400)

        _log_dm_event(
            sid=sid,
            level="info",
            category="nao",
            event="nao_move_toward_start",
            source=source,
            message="NAO moveToward requested.",
            request_id=request_id,
            data={
                "x": x,
                "y": y,
                "theta": theta,
                "seq": seq,
            },
        )

        default_frequency = _clean_locomotion_frequency(
            runtime_cfg.get("locomotion_frequency", _LOCOMOTION_FREQUENCY_DEFAULT),
            default=_LOCOMOTION_FREQUENCY_DEFAULT,
        )
        frequency = _clean_locomotion_frequency(payload.get("frequency", default_frequency), default=default_frequency)
        arms_enabled = bool(payload.get("arms_enabled", runtime_cfg.get("locomotion_arms_enabled", True)))

        candidates = _iter_nao_motion_candidates(runtime_cfg)
        if not candidates:
            return _move_resp({"ok": False, "error": "NAO endpoint ontbreekt."}, 400)
        _touch_activity()

        last_error: Optional[str] = None
        for endpoint_base_url, mode in candidates:
            try:
                _naoqi_call_on_endpoint(
                    endpoint_base_url,
                    mode,
                    module="ALMotion",
                    method="setExternalCollisionProtectionEnabled",
                    args=["Move", True],
                )
                _naoqi_call_on_endpoint(
                    endpoint_base_url,
                    mode,
                    module="ALMotion",
                    method="setTangentialSecurityDistance",
                    args=[_LOCOMOTION_COLLISION_TANGENTIAL_M],
                )
                _naoqi_call_on_endpoint(
                    endpoint_base_url,
                    mode,
                    module="ALMotion",
                    method="setOrthogonalSecurityDistance",
                    args=[_LOCOMOTION_COLLISION_ORTHOGONAL_M],
                )
                _naoqi_call_on_endpoint(
                    endpoint_base_url,
                    mode,
                    module="ALMotion",
                    method="setMoveArmsEnabled",
                    args=[arms_enabled, arms_enabled],
                )
                _naoqi_call_on_endpoint(
                    endpoint_base_url,
                    mode,
                    module="ALMotion",
                    method="moveToward",
                    args=[x, y, theta, [["Frequency", frequency]]],
                )
            except Exception as exc:
                last_error = str(exc)
                continue

            out = {
                "ok": True,
                "mode": mode,
                "ignored": False,
                "applied": {
                    "x": x,
                    "y": y,
                    "theta": theta,
                    "frequency": frequency,
                    "arms_enabled": arms_enabled,
                    "collision": {
                        "move": True,
                        "tangential_m": _LOCOMOTION_COLLISION_TANGENTIAL_M,
                        "orthogonal_m": _LOCOMOTION_COLLISION_ORTHOGONAL_M,
                    },
                },
            }
            if seq is not None:
                out["seq"] = seq
            return _move_resp(out, 200)

        return _move_resp({"ok": False, "error": last_error or "moveToward failed"}, 502)

    @app.post("/api/nao_stop_move")
    def api_nao_stop_move():
        payload = request.get_json(force=True, silent=True) or {}
        sid = _get_sid()
        runtime_cfg = _get_runtime_cfg(sid)
        source = _infer_event_source(endpoint="/api/nao_stop_move", explicit_source=payload.get("source"))
        request_id = _get_request_id()
        candidates = _iter_nao_motion_candidates(runtime_cfg)
        if not candidates:
            resp = jsonify({"ok": False, "error": "NAO endpoint ontbreekt."})
            resp.status_code = 400
            resp.headers["X-Request-Id"] = request_id
            return resp
        _touch_activity()
        _log_dm_event(
            sid=sid,
            level="info",
            category="nao",
            event="nao_stop_move_start",
            source=source,
            message="NAO stop move requested.",
            request_id=request_id,
            data={},
        )

        last_error: Optional[str] = None
        for endpoint_base_url, mode in candidates:
            try:
                _stop_move_on_endpoint(endpoint_base_url, mode)
                _log_dm_event(
                    sid=sid,
                    level="info",
                    category="nao",
                    event="nao_stop_move_result",
                    source=source,
                    message="NAO stop move succeeded.",
                    request_id=request_id,
                    data={"ok": True, "mode": mode},
                )
                resp = jsonify({"ok": True, "mode": mode})
                resp.headers["X-Request-Id"] = request_id
                return resp
            except Exception as exc:
                last_error = str(exc)
                continue
        _log_dm_event(
            sid=sid,
            level="error",
            category="nao",
            event="nao_stop_move_result",
            source=source,
            message="NAO stop move failed.",
            request_id=request_id,
            data={"ok": False, "error": last_error or "stop_move failed"},
        )
        resp = jsonify({"ok": False, "error": last_error or "stop_move failed"})
        resp.status_code = 502
        resp.headers["X-Request-Id"] = request_id
        return resp

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

    @app.get("/api/nao_camera_snapshot")
    def api_nao_camera_snapshot():
        runtime_cfg = _get_runtime_cfg(_get_sid())
        behavior_url = _normalize_url(runtime_cfg.get("behavior_manager_url"))
        base_url = _normalize_url(runtime_cfg.get("nao_base_url"))
        behavior_enabled = bool(runtime_cfg.get("behavior_enabled", True))
        base_enabled = bool(runtime_cfg.get("base_enabled", True))

        params = {}
        for key in ("camera", "resolution", "fps"):
            value = request.args.get(key, None)
            if value is not None:
                params[key] = value

        candidates: List[str] = []
        if behavior_enabled and behavior_url:
            candidates.append(behavior_url.rstrip("/") + "/nao/camera_snapshot")
        if base_enabled and base_url:
            candidates.append(base_url.rstrip("/") + "/camera_snapshot")
        if not candidates:
            return jsonify({"ok": False, "error": "NAO endpoint ontbreekt."}), 400

        def _extract_upstream_error(resp_obj):
            body = ""
            try:
                payload = resp_obj.json()
                if isinstance(payload, dict):
                    body = (
                        payload.get("error")
                        or payload.get("details")
                        or payload.get("status")
                        or ""
                    )
            except Exception:
                body = (resp_obj.text or "").strip()
            if len(body) > 240:
                body = body[:240] + "..."
            return body

        last_error = "camera snapshot failed"
        for url in candidates:
            try:
                resp = requests.get(url, params=params, timeout=2.5)
            except Exception as exc:
                last_error = "upstream request failed (%s): %s" % (url, str(exc))
                continue
            if resp.status_code >= 400:
                body = _extract_upstream_error(resp)
                last_error = "upstream status %s (%s): %s" % (
                    resp.status_code,
                    url,
                    (body or "camera snapshot failed"),
                )
                continue
            content_type = (resp.headers.get("Content-Type") or "image/bmp").strip()
            if not content_type.lower().startswith("image/"):
                body = _extract_upstream_error(resp)
                last_error = "upstream returned non-image (%s): %s" % (
                    url,
                    (body or content_type),
                )
                continue
            out = Response(resp.content, status=200, mimetype=content_type)
            out.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
            return out
        return jsonify({"ok": False, "error": last_error}), 502

    @app.get("/api/nao_people_detection_state")
    def api_nao_people_detection_state():
        runtime_cfg = _get_runtime_cfg(_get_sid())
        base_url, mode = _resolve_nao_audio_url(runtime_cfg)
        if not base_url:
            return jsonify({"ok": False, "error": "NAO audio endpoint ontbreekt."}), 400
        try:
            url = base_url + "/nao/people_detection_state" if mode == "behavior" else base_url + "/people_detection_state"
            resp = requests.get(url, timeout=3.0)
            resp.raise_for_status()
            payload = resp.json()
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 502

        if not isinstance(payload, dict):
            return jsonify({"ok": False, "error": "invalid response"}), 502
        if payload.get("status") not in ("ok", None):
            return jsonify({"ok": False, "error": payload.get("error") or "people_detection_state failed"}), 502
        state = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        return jsonify({"ok": True, "state": state})

    @app.post("/api/nao_people_detection_set")
    def api_nao_people_detection_set():
        runtime_cfg = _get_runtime_cfg(_get_sid())
        base_url, mode = _resolve_nao_audio_url(runtime_cfg)
        if not base_url:
            return jsonify({"ok": False, "error": "NAO audio endpoint ontbreekt."}), 400
        payload = request.get_json(force=True, silent=True) or {}
        api_name = (payload.get("api") or "").strip()
        enabled = payload.get("enabled", None)
        if not api_name:
            return jsonify({"ok": False, "error": "Missing 'api'."}), 400
        if enabled is None:
            return jsonify({"ok": False, "error": "Missing 'enabled'."}), 400

        try:
            url = base_url + "/nao/people_detection_set" if mode == "behavior" else base_url + "/people_detection_set"
            resp = requests.post(url, json={"api": api_name, "enabled": bool(enabled)}, timeout=3.0)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 502

        if not isinstance(data, dict):
            return jsonify({"ok": False, "error": "invalid response"}), 502
        if data.get("status") not in ("ok", None):
            return jsonify({"ok": False, "error": data.get("error") or "people_detection_set failed"}), 502
        out = data.get("data") if isinstance(data.get("data"), dict) else {}
        state = out.get("state") if isinstance(out.get("state"), dict) else None
        return jsonify({"ok": True, "state": state, "applied": {"api": api_name, "enabled": bool(enabled)}})

    @app.get("/api/nao_autonomous_life_state")
    def api_nao_autonomous_life_state():
        runtime_cfg = _get_runtime_cfg(_get_sid())
        return jsonify(_get_nao_autonomous_life_state(runtime_cfg))

    @app.get("/api/nao_awake_state")
    def api_nao_awake_state():
        runtime_cfg = _get_runtime_cfg(_get_sid())
        return jsonify(_get_nao_awake_state(runtime_cfg))

    @app.post("/api/nao_wake_up")
    def api_nao_wake_up():
        payload = request.get_json(force=True, silent=True) or {}
        sid = _get_sid()
        runtime_cfg = _get_runtime_cfg(sid)
        source = _infer_event_source(endpoint="/api/nao_wake_up", explicit_source=payload.get("source"))
        request_id = _get_request_id()
        _log_dm_event(
            sid=sid,
            level="info",
            category="nao",
            event="nao_wake_up_start",
            source=source,
            message="NAO wake-up requested.",
            request_id=request_id,
            data={},
        )
        result = _set_nao_awake_state(runtime_cfg, True)
        if not result.get("ok"):
            _log_dm_event(
                sid=sid,
                level="error",
                category="nao",
                event="nao_wake_up_result",
                source=source,
                message="NAO wake-up failed.",
                request_id=request_id,
                data={"ok": False, "error": result.get("error")},
            )
            resp = jsonify({"ok": False, "error": result.get("error")})
            resp.status_code = 400
            resp.headers["X-Request-Id"] = request_id
            return resp
        out = {
            "ok": True,
            "is_awake": bool(result.get("is_awake")) if result.get("is_awake") is not None else None,
            "mode": result.get("mode"),
        }
        if result.get("pending"):
            out["pending"] = True
        _log_dm_event(
            sid=sid,
            level="info",
            category="nao",
            event="nao_wake_up_result",
            source=source,
            message="NAO wake-up completed.",
            request_id=request_id,
            data={"ok": True, "mode": out.get("mode"), "pending": bool(out.get("pending", False))},
        )
        resp = jsonify(out)
        resp.headers["X-Request-Id"] = request_id
        return resp

    @app.post("/api/nao_rest")
    def api_nao_rest():
        payload = request.get_json(force=True, silent=True) or {}
        sid = _get_sid()
        runtime_cfg = _get_runtime_cfg(sid)
        source = _infer_event_source(endpoint="/api/nao_rest", explicit_source=payload.get("source"))
        request_id = _get_request_id()
        _log_dm_event(
            sid=sid,
            level="info",
            category="nao",
            event="nao_rest_start",
            source=source,
            message="NAO rest requested.",
            request_id=request_id,
            data={},
        )
        result = _set_nao_awake_state(runtime_cfg, False)
        if not result.get("ok"):
            _log_dm_event(
                sid=sid,
                level="error",
                category="nao",
                event="nao_rest_result",
                source=source,
                message="NAO rest failed.",
                request_id=request_id,
                data={"ok": False, "error": result.get("error")},
            )
            resp = jsonify({"ok": False, "error": result.get("error")})
            resp.status_code = 400
            resp.headers["X-Request-Id"] = request_id
            return resp
        out = {
            "ok": True,
            "is_awake": bool(result.get("is_awake")) if result.get("is_awake") is not None else None,
            "mode": result.get("mode"),
        }
        if result.get("pending"):
            out["pending"] = True
        _log_dm_event(
            sid=sid,
            level="info",
            category="nao",
            event="nao_rest_result",
            source=source,
            message="NAO rest completed.",
            request_id=request_id,
            data={"ok": True, "mode": out.get("mode"), "pending": bool(out.get("pending", False))},
        )
        resp = jsonify(out)
        resp.headers["X-Request-Id"] = request_id
        return resp

    @app.get("/api/nao_custom_life_state")
    def api_nao_custom_life_state():
        runtime_cfg = _get_runtime_cfg(_get_sid())
        return jsonify(_get_nao_custom_life_state(runtime_cfg))

    @app.get("/api/nao_custom_life_ui_state")
    def api_nao_custom_life_ui_state():
        runtime_cfg = _get_runtime_cfg(_get_sid())
        return jsonify(_get_nao_custom_life_ui_state(runtime_cfg))

    @app.post("/api/nao_custom_life_set")
    def api_nao_custom_life_set():
        payload = request.get_json(force=True, silent=True) or {}
        enabled = payload.get("enabled", None)
        if enabled is None:
            return jsonify({"ok": False, "error": "Missing 'enabled'."}), 400
        sid = _get_sid()
        source = _infer_event_source(endpoint="/api/nao_custom_life_set", explicit_source=payload.get("source"))
        request_id = _get_request_id()
        runtime_cfg = _get_runtime_cfg(sid)
        before_cfg = copy.deepcopy(runtime_cfg)
        _log_dm_event(
            sid=sid,
            level="info",
            category="nao",
            event="nao_custom_life_set_start",
            source=source,
            message="NAO custom life toggle requested.",
            request_id=request_id,
            data={"enabled": bool(enabled)},
        )
        runtime_cfg["custom_life_enabled"] = bool(enabled)
        _save_runtime_cfg_state()
        _log_runtime_cfg_change(
            sid=sid,
            before=before_cfg,
            after=runtime_cfg,
            source=source,
            request_id=request_id,
            reason="nao_custom_life_set",
        )
        _apply_custom_life(sid, runtime_cfg)
        if not runtime_cfg.get("custom_life_enabled", False):
            _release_custom_life_lock(sid, runtime_cfg)
        state = _get_nao_custom_life_ui_state(runtime_cfg)
        if not state.get("ok"):
            _log_dm_event(
                sid=sid,
                level="warn",
                category="nao",
                event="nao_custom_life_set_result",
                source=source,
                message="NAO custom life updated with warning.",
                request_id=request_id,
                data={"ok": True, "warning": state.get("error"), "enabled": bool(runtime_cfg.get("custom_life_enabled", False))},
            )
            resp = jsonify(
                {
                    "ok": True,
                    "enabled": bool(runtime_cfg.get("custom_life_enabled", False)),
                    "life_state": None,
                    "active_modules": False,
                    "warning": state.get("error"),
                }
            )
            resp.headers["X-Request-Id"] = request_id
            return resp
        _log_dm_event(
            sid=sid,
            level="info",
            category="nao",
            event="nao_custom_life_set_result",
            source=source,
            message="NAO custom life updated.",
            request_id=request_id,
            data={"ok": True, "enabled": bool(state.get("enabled"))},
        )
        resp = jsonify(
            {
                "ok": True,
                "enabled": bool(state.get("enabled")),
                "life_state": state.get("life_state"),
                "active_modules": bool(state.get("active_modules")),
                "state": state.get("state"),
            }
        )
        resp.headers["X-Request-Id"] = request_id
        return resp

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
        runtime_cfg = _get_runtime_cfg(sid)
        if cmdrec is None:
            return jsonify({"ok": False, "error": "cmdrec niet beschikbaar."}), 400
        try:
            labels = cmdrec.get_labels() or []
            exclude = {"LOCOMOTION_REQUEST", "WALK_WITH_ME", "REST"}
            commands = []
            for label in sorted(labels):
                label_upper = label.upper()
                if label_upper in exclude or "LOCOMOTION" in label_upper:
                    continue
                behavior = _command_behavior_preview(label, cmdrec, behavior_executor, runtime_cfg=runtime_cfg)
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
        runtime_cfg = _get_runtime_cfg(sid)
        _, cmdrec, _ = _pipeline_props(pipeline)
        if cmdrec is None:
            return jsonify({"ok": False, "error": "cmdrec niet beschikbaar."}), 400
        try:
            catalog = _dance_catalog(cmdrec, runtime_cfg=runtime_cfg)
            dances = []
            for item in catalog:
                key = item.get("key")
                behavior = item.get("behavior")
                if isinstance(key, str) and key.strip():
                    dances.append({"key": key.strip(), "behavior": behavior})
            return jsonify({"ok": True, "dances": dances})
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 500

    def _resolve_dance_by_key(
        cmdrec_obj: Any,
        dance_key: str,
        *,
        runtime_cfg: Optional[JsonLike] = None,
    ) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        key_norm = str(dance_key or "").strip()
        if not key_norm:
            return None, "missing_dance_key"
        catalog = _dance_catalog(cmdrec_obj, runtime_cfg=runtime_cfg)
        for item in catalog:
            key = item.get("key")
            if not isinstance(key, str) or not key.strip():
                continue
            if key.strip().casefold() != key_norm.casefold():
                continue
            resolved: Dict[str, Any] = {"dance_key": key.strip()}
            behavior = item.get("behavior")
            if isinstance(behavior, str) and behavior.strip():
                resolved["dance_behavior"] = behavior.strip()
            return resolved, None
        return None, "dance_not_found"

    def _command_execute_impl(
        sid: str,
        payload: Dict[str, Any],
        *,
        source: str = "manual_command_ui",
        request_id: Optional[str] = None,
    ) -> Tuple[Dict[str, Any], int]:
        label = (payload.get("label") or "").strip()
        if not label:
            return {"ok": False, "error": "Missing 'label'."}, 400
        _touch_activity()
        runtime_cfg = _get_runtime_cfg(sid)
        pipeline = _get_pipeline(sid)
        _, cmdrec, behavior_executor = _pipeline_props(pipeline)
        _set_behavior_executor_custom_life_management(behavior_executor, False)
        if behavior_executor is None:
            return {"ok": False, "error": "behavior executor niet beschikbaar."}, 400
        resolved = payload.get("resolved") if isinstance(payload.get("resolved"), dict) else None
        if label.upper() == "DANCE":
            if resolved:
                dance_key = str(resolved.get("dance_key") or "").strip()
                if dance_key and cmdrec is not None:
                    resolved_checked, err = _resolve_dance_by_key(cmdrec, dance_key, runtime_cfg=runtime_cfg)
                    if err:
                        return {"ok": False, "error": err, "dance_key": dance_key}, 400
                    resolved = resolved_checked
            if not resolved:
                resolved = _default_dance_resolution(cmdrec, runtime_cfg=runtime_cfg)
        cmd = CommandDecision(label=label, confidence=1.0, raw_text=label, resolved=resolved)
        _log_dm_event(
            sid=sid,
            level="info",
            category="cmdrec",
            event="command_execute_requested",
            source=source,
            message=f"Manual command execute requested: {cmd.label}",
            request_id=request_id,
            data={"command": cmd.label, "resolved": cmd.resolved or {}, "origin": "manual_execute"},
        )
        lock_mode = _custom_life_lock_mode_for_command(cmd)
        if lock_mode:
            _acquire_custom_life_lock(sid, runtime_cfg, lock_mode)
        try:
            behavior_executor.execute(cmd)
        except Exception as exc:
            _log_dm_event(
                sid=sid,
                level="error",
                category="cmdrec",
                event="command_execute_error",
                source=source,
                message=f"Manual command failed: {cmd.label}",
                request_id=request_id,
                data={"command": cmd.label, "error": str(exc), "type": exc.__class__.__name__},
            )
            return {"ok": False, "error": str(exc)}, 400
        if _normalize_command_label(cmd.label) == "STOP":
            _execute_standup_after_stop_if_needed(
                sid,
                behavior_executor,
                runtime_cfg_local=runtime_cfg,
                source="system_auto",
                request_id=request_id,
            )
            _release_custom_life_lock(sid, runtime_cfg)
            _clear_command_stop_available(sid)
            _stop_nao_audio_best_effort(
                runtime_cfg,
                source="system_auto",
                request_id=request_id,
                sid=sid,
                reason="manual_command_stop",
            )
        elif lock_mode in _CUSTOM_LIFE_LOCK_MODES:
            _set_command_stop_available(sid, cmd.label, scope=lock_mode)
        else:
            _clear_command_stop_available(sid)
        _touch_activity()
        out = {"ok": True}
        _attach_command_stop_payload(sid, out)
        _log_dm_event(
            sid=sid,
            level="info",
            category="cmdrec",
            event="command_executed",
            source=source,
            message=f"Manual command executed: {cmd.label}",
            request_id=request_id,
            data={"command": cmd.label, "resolved": cmd.resolved or {}, "origin": "manual_execute"},
        )
        _log_dm_event(
            sid=sid,
            level="info",
            category="dialog",
            event="dialog_reply",
            source=source,
            message="Manual command execution completed.",
            request_id=request_id,
            data={"reply": f"OK. Uitgevoerd: {cmd.label}", "emit_used": "none"},
        )
        return out, 200

    def _nao_behavior_start_impl(
        sid: str,
        behavior: str,
        *,
        source: str = "manual_command_ui",
        request_id: Optional[str] = None,
    ) -> Tuple[Dict[str, Any], int]:
        behavior = str(behavior or "").strip()
        if not behavior:
            return {"ok": False, "error": "Missing 'behavior'."}, 400
        _touch_activity()
        _log_dm_event(
            sid=sid,
            level="info",
            category="nao",
            event="nao_behavior_start",
            source=source,
            message="Behavior start requested.",
            request_id=request_id,
            data={"behavior": behavior},
        )
        runtime_cfg = _get_runtime_cfg(sid)
        behavior_url = _normalize_url(runtime_cfg.get("behavior_manager_url"))
        base_url = _normalize_url(runtime_cfg.get("nao_base_url"))
        behavior_enabled = bool(runtime_cfg.get("behavior_enabled", True))
        base_enabled = bool(runtime_cfg.get("base_enabled", True))
        lock_mode = _custom_life_lock_mode_for_behavior_name(behavior)
        state = _get_runtime_state(sid)
        lock_active = _custom_life_lock_is_active(state)
        candidates: List[Tuple[str, str]] = []
        if behavior_enabled and behavior_url:
            candidates.append((behavior_url, "behavior"))
        if base_enabled and base_url:
            candidates.append((base_url, "base"))
        if not candidates:
            return {"ok": False, "error": "NAO endpoint ontbreekt."}, 400

        last_error = None
        for endpoint_base_url, mode in candidates:
            paused_now = False
            pause_state = None
            if lock_mode and not lock_active:
                paused_now, pause_state = _custom_life_pause_on_endpoint(endpoint_base_url, mode)
            try:
                url = endpoint_base_url + "/nao/do_behavior" if mode == "behavior" else endpoint_base_url + "/do_behavior"
                resp = requests.post(url, json={"behavior": behavior}, timeout=5.0)
                data = resp.json() if resp.ok else {}
            except Exception as exc:
                last_error = str(exc)
                if paused_now:
                    try:
                        _custom_life_restore_on_endpoint(endpoint_base_url, mode, runtime_cfg, pause_state)
                    except Exception:
                        pass
                continue
            if isinstance(data, dict) and data.get("status") not in (None, "ok"):
                last_error = data.get("error") or "behavior failed"
                if paused_now:
                    try:
                        _custom_life_restore_on_endpoint(endpoint_base_url, mode, runtime_cfg, pause_state)
                    except Exception:
                        pass
                continue
            if lock_mode:
                if lock_active:
                    state["custom_life_lock_mode"] = _custom_life_merge_lock_mode(state.get("custom_life_lock_mode"), lock_mode)
                elif paused_now:
                    _custom_life_store_lock_state(
                        state,
                        lock_mode=lock_mode,
                        pause_state=pause_state,
                        base_url=endpoint_base_url,
                        endpoint_mode=mode,
                    )
                    lock_active = True
            _touch_activity()
            _log_dm_event(
                sid=sid,
                level="info",
                category="nao",
                event="nao_behavior_start_result",
                source=source,
                message="Behavior start succeeded.",
                request_id=request_id,
                data={"ok": True, "behavior": behavior, "mode": mode},
            )
            return {"ok": True}, 200
        _log_dm_event(
            sid=sid,
            level="error",
            category="nao",
            event="nao_behavior_start_result",
            source=source,
            message="Behavior start failed.",
            request_id=request_id,
            data={"ok": False, "behavior": behavior, "error": last_error or "behavior failed"},
        )
        return {"ok": False, "error": last_error or "behavior failed"}, 400

    def _nao_behavior_stop_impl(
        sid: str,
        behavior: str,
        *,
        source: str = "manual_command_ui",
        request_id: Optional[str] = None,
    ) -> Tuple[Dict[str, Any], int]:
        behavior = str(behavior or "").strip()
        if not behavior:
            return {"ok": False, "error": "Missing 'behavior'."}, 400
        _touch_activity()
        _log_dm_event(
            sid=sid,
            level="info",
            category="nao",
            event="nao_behavior_stop",
            source=source,
            message="Behavior stop requested.",
            request_id=request_id,
            data={"behavior": behavior},
        )
        runtime_cfg = _get_runtime_cfg(sid)
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
            return {"ok": False, "error": "NAO endpoint ontbreekt."}, 400

        last_error = None
        walk_behavior_stop = "walkwithme" in behavior.strip().lower()
        for endpoint_base_url, mode in candidates:
            try:
                url = endpoint_base_url + "/nao/stop_behavior" if mode == "behavior" else endpoint_base_url + "/stop_behavior"
                resp = requests.post(url, json={"behavior": behavior}, timeout=5.0)
                data = resp.json() if resp.ok else {}
            except Exception as exc:
                last_error = str(exc)
                continue
            if isinstance(data, dict) and data.get("status") not in (None, "ok"):
                last_error = data.get("error") or "behavior failed"
                continue
            if walk_behavior_stop:
                try:
                    delay_s = _post_stop_standup_delay_s(runtime_cfg)
                    if delay_s > 0.0:
                        time.sleep(delay_s)
                    do_behavior_url = endpoint_base_url + "/nao/do_behavior" if mode == "behavior" else endpoint_base_url + "/do_behavior"
                    requests.post(do_behavior_url, json={"behavior": "basic/standup"}, timeout=5.0)
                except Exception:
                    pass
            _release_custom_life_lock(sid, runtime_cfg)
            _touch_activity()
            _log_dm_event(
                sid=sid,
                level="info",
                category="nao",
                event="nao_behavior_stop_result",
                source=source,
                message="Behavior stop succeeded.",
                request_id=request_id,
                data={"ok": True, "behavior": behavior, "mode": mode},
            )
            return {"ok": True}, 200
        _log_dm_event(
            sid=sid,
            level="error",
            category="nao",
            event="nao_behavior_stop_result",
            source=source,
            message="Behavior stop failed.",
            request_id=request_id,
            data={"ok": False, "behavior": behavior, "error": last_error or "behavior failed"},
        )
        return {"ok": False, "error": last_error or "behavior failed"}, 400

    @app.post("/api/command_execute")
    def api_command_execute():
        sid = _get_sid()
        payload = request.get_json(force=True, silent=True) or {}
        source = _infer_event_source(
            endpoint="/api/command_execute",
            explicit_source=payload.get("source"),
        )
        request_id = _get_request_id()
        out, status_code = _command_execute_impl(sid, payload, source=source, request_id=request_id)
        resp = jsonify(out)
        resp.status_code = status_code
        resp.headers["X-Request-Id"] = request_id
        resp.set_cookie("sid", sid, max_age=60 * 60 * 24 * 7, samesite="Lax")
        return resp

    @app.get("/api/script/capabilities")
    def api_script_capabilities():
        return jsonify(
            {
                "ok": True,
                "supports": {
                    "say": True,
                    "do_modes": [
                        "command",
                        "behavior_start",
                        "behavior_stop",
                        "dance",
                        "summary_capture_start",
                        "summary_capture_stop_and_draft",
                        "summary_publish",
                        "summary_cancel",
                    ],
                    "dance_catalog": True,
                },
            }
        )

    @app.post("/api/script/say")
    def api_script_say():
        payload = request.get_json(force=True, silent=True) or {}
        text = str(payload.get("text") or "").strip()
        if not text:
            return jsonify({"ok": False, "error": "Missing 'text'."}), 400
        sid = _get_sid()
        source = _infer_event_source(endpoint="/api/script/say", explicit_source=payload.get("source"))
        request_id = _get_request_id()
        runtime_cfg = _get_runtime_cfg(sid)
        pipeline = _get_pipeline(sid)
        _emit_followup_text(
            pipeline=pipeline,
            emit_used="pipeline",
            runtime_cfg=runtime_cfg,
            text=text,
        )
        _touch_activity()
        _log_dm_event(
            sid=sid,
            level="info",
            category="dialog",
            event="dialog_reply",
            source=source,
            message="Script API say emitted.",
            request_id=request_id,
            data={"reply": text, "emit_used": "pipeline", "action": "say"},
        )
        resp = jsonify({"ok": True, "status": "accepted", "action": "say"})
        resp.headers["X-Request-Id"] = request_id
        resp.set_cookie("sid", sid, max_age=60 * 60 * 24 * 7, samesite="Lax")
        return resp

    def _summary_transcript_text(lines: List[str]) -> str:
        cleaned: List[str] = []
        for item in lines:
            text = str(item or "").strip()
            if text:
                cleaned.append(text)
        return "\n".join(cleaned).strip()

    def _summary_default_system_prompt_text() -> str:
        try:
            path = os.path.join(_master_prompts_dir(), "workshop_summary_system.txt")
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read().strip()
            if text:
                return text
        except Exception:
            pass
        return _SUMMARY_DEFAULT_SYSTEM_PROMPT_FALLBACK

    def _summary_prompt_from_file(raw_name: Any) -> Tuple[Optional[str], Optional[str], Optional[str]]:
        display_name = _normalize_master_prompt_name(raw_name)
        if not display_name:
            return None, None, "invalid_system_prompt_file"
        abs_path = _master_prompt_abs_path(display_name)
        if not os.path.exists(abs_path):
            return None, display_name, "system_prompt_file_not_found"
        try:
            with open(abs_path, "r", encoding="utf-8", errors="replace") as fh:
                content = fh.read().strip()
        except OSError as exc:
            return None, display_name, str(exc)
        if not content:
            return None, display_name, "system_prompt_file_empty"
        return content, display_name, None

    def _summary_resolve_system_prompt(payload_obj: Dict[str, Any]) -> Tuple[Optional[str], Dict[str, Any], Optional[str]]:
        inline = str(payload_obj.get("system_prompt") or "").strip()
        if inline:
            return inline, {"source": "inline", "name": None}, None

        raw_file = payload_obj.get("system_prompt_file")
        if raw_file not in (None, ""):
            text, display_name, err = _summary_prompt_from_file(raw_file)
            if err:
                return None, {"source": "file", "name": display_name}, err
            return text, {"source": "file", "name": display_name}, None

        default_name = "master_prompts/workshop_summary_system.txt"
        return _summary_default_system_prompt_text(), {"source": "default", "name": default_name}, None

    def _summary_capture_start_impl(sid: str) -> Tuple[Dict[str, Any], int]:
        state = _get_summary_state(sid)
        with summary_state_lock:
            capture_thread = state.get("capture_thread")
            stt_thread = state.get("stt_thread")
            running = bool(
                (capture_thread is not None and hasattr(capture_thread, "is_alive") and capture_thread.is_alive())
                or (stt_thread is not None and hasattr(stt_thread, "is_alive") and stt_thread.is_alive())
            )
        if running:
            with summary_state_lock:
                transcript_list = [str(item or "").strip() for item in (state.get("transcripts") or [])]
                transcript_list = [item for item in transcript_list if item]
                capture_stats = copy.deepcopy(state.get("capture_stats") or {})
                last_error = str(state.get("last_error") or "").strip() or None
            transcript_count = len(transcript_list)
            return {
                "ok": True,
                "status": "accepted",
                "action": "do",
                "mode": "summary_capture_start",
                "phase": "capturing",
                "transcript_count": transcript_count,
                "transcript": transcript_list[-_SUMMARY_LIVE_TRANSCRIPT_MAX_ITEMS:],
                "capture_stats": capture_stats,
                "last_error": last_error,
                "already_running": True,
            }, 200

        runtime_cfg = _get_runtime_cfg(sid)
        merged = _apply_runtime_overrides(base_cfg, runtime_cfg)
        capture_cfg = _summary_capture_mic_config(merged)
        input_cfg = capture_cfg.get("input", {}) or {}
        if (input_cfg.get("type") or "audio").lower() != "audio":
            return {"ok": False, "error": "input_not_audio"}, 400
        try:
            mic = _make_mic_from_config(capture_cfg, mode="continuous")
        except Exception as exc:
            return {"ok": False, "error": "mic_unavailable", "detail": str(exc)}, 400
        timeout_s = _resolve_start_timeout(
            (input_cfg.get("params") or {}),
            default=_SUMMARY_CAPTURE_TIMEOUT_DEFAULT_S,
        )
        timeout_s = max(0.25, min(timeout_s, _SUMMARY_CAPTURE_TIMEOUT_MAX_S))
        stop_event = threading.Event()
        audio_queue: "queue.Queue[Any]" = queue.Queue(maxsize=_SUMMARY_STT_QUEUE_MAX_ITEMS)
        started_at = time.time()

        def _update_queue_size() -> None:
            try:
                qsize = int(audio_queue.qsize())
            except Exception:
                qsize = 0
            with summary_state_lock:
                stats = state.get("capture_stats")
                if isinstance(stats, dict):
                    stats["stt_queue_size"] = max(0, qsize)

        with summary_state_lock:
            state["phase"] = "capturing"
            state["transcripts"] = []
            state["draft_text"] = None
            state["last_prompt_meta"] = None
            state["last_error"] = None
            state["capture_stats"] = {
                "loops": 0,
                "timeouts": 0,
                "audio_chunks": 0,
                "dropped_audio_chunks": 0,
                "stt_calls": 0,
                "transcript_count": 0,
                "stt_queue_max": _SUMMARY_STT_QUEUE_MAX_ITEMS,
                "stt_queue_size": 0,
                "started_at": started_at,
                "last_audio_at": None,
                "last_transcript_at": None,
            }
            state["stop_event"] = stop_event
            state["audio_queue"] = audio_queue
            state["capture_thread"] = None
            state["stt_thread"] = None

        def _capture_worker() -> None:
            try:
                while not stop_event.is_set():
                    with summary_state_lock:
                        stats = state.get("capture_stats")
                        if isinstance(stats, dict):
                            stats["loops"] = int(stats.get("loops") or 0) + 1
                    try:
                        audio = mic.capture_utterance(timeout_s=timeout_s)
                    except TimeoutError:
                        with summary_state_lock:
                            stats = state.get("capture_stats")
                            if isinstance(stats, dict):
                                stats["timeouts"] = int(stats.get("timeouts") or 0) + 1
                        continue
                    except Exception as exc:
                        if stop_event.is_set():
                            break
                        with summary_state_lock:
                            state["last_error"] = "mic: " + str(exc)
                        continue
                    if stop_event.is_set():
                        break
                    with summary_state_lock:
                        stats = state.get("capture_stats")
                        if isinstance(stats, dict):
                            stats["audio_chunks"] = int(stats.get("audio_chunks") or 0) + 1
                            stats["last_audio_at"] = time.time()

                    enqueued = False
                    while not stop_event.is_set():
                        try:
                            audio_queue.put_nowait(audio)
                            enqueued = True
                            break
                        except queue.Full:
                            dropped = False
                            try:
                                audio_queue.get_nowait()
                                dropped = True
                            except queue.Empty:
                                pass
                            if dropped:
                                with summary_state_lock:
                                    stats = state.get("capture_stats")
                                    if isinstance(stats, dict):
                                        stats["dropped_audio_chunks"] = int(stats.get("dropped_audio_chunks") or 0) + 1
                            else:
                                break
                    if enqueued:
                        _update_queue_size()
            finally:
                with summary_state_lock:
                    current = state.get("capture_thread")
                    if current is threading.current_thread():
                        state["capture_thread"] = None

        def _stt_worker() -> None:
            try:
                while True:
                    if stop_event.is_set() and audio_queue.empty():
                        break
                    try:
                        queued_audio = audio_queue.get(timeout=0.1)
                    except queue.Empty:
                        continue

                    _update_queue_size()
                    try:
                        with summary_state_lock:
                            stats = state.get("capture_stats")
                            if isinstance(stats, dict):
                                stats["stt_calls"] = int(stats.get("stt_calls") or 0) + 1
                        stt_res = _get_stt(sid).transcribe(queued_audio)
                        text = str(getattr(stt_res, "text", "") or "").strip()
                    except Exception as exc:
                        with summary_state_lock:
                            state["last_error"] = "stt: " + str(exc)
                        continue
                    if text:
                        with summary_state_lock:
                            transcripts = state.get("transcripts")
                            if not isinstance(transcripts, list):
                                transcripts = []
                                state["transcripts"] = transcripts
                            transcripts.append(text)
                            stats = state.get("capture_stats")
                            if isinstance(stats, dict):
                                stats["transcript_count"] = len(transcripts)
                                stats["last_transcript_at"] = time.time()
            finally:
                with summary_state_lock:
                    current = state.get("stt_thread")
                    if current is threading.current_thread():
                        state["stt_thread"] = None
                _update_queue_size()

        capture_thread_obj = threading.Thread(target=_capture_worker, daemon=True)
        stt_thread_obj = threading.Thread(target=_stt_worker, daemon=True)
        with summary_state_lock:
            state["capture_thread"] = capture_thread_obj
            state["stt_thread"] = stt_thread_obj
        stt_thread_obj.start()
        capture_thread_obj.start()
        with summary_state_lock:
            start_stats = copy.deepcopy(state.get("capture_stats") or {})
        return {
            "ok": True,
            "status": "accepted",
            "action": "do",
            "mode": "summary_capture_start",
            "phase": "capturing",
            "transcript_count": 0,
            "transcript": [],
            "capture_stats": start_stats,
            "last_error": None,
            "capture_timeout_s": timeout_s,
            "mic_mode": "continuous",
        }, 200

    def _summary_capture_stop_and_draft_impl(sid: str, payload_obj: Dict[str, Any]) -> Tuple[Dict[str, Any], int]:
        input_prompt_template = str(payload_obj.get("input_prompt_template") or "").strip()
        if not input_prompt_template:
            return {"ok": False, "error": "missing_input_prompt_template"}, 400

        state = _get_summary_state(sid)
        with summary_state_lock:
            phase = str(state.get("phase") or "idle").strip().lower()
        if phase != "capturing":
            return {"ok": False, "error": "invalid_summary_state", "phase": phase}, 400

        if not _summary_stop_capture(state):
            return {"ok": False, "error": "summary_capture_stop_timeout"}, 409

        with summary_state_lock:
            transcripts = [str(item or "").strip() for item in (state.get("transcripts") or [])]
            transcripts = [item for item in transcripts if item]
            last_error = str(state.get("last_error") or "").strip()
            capture_stats = copy.deepcopy(state.get("capture_stats") or {})
        if not transcripts:
            with summary_state_lock:
                state["phase"] = "idle"
            out: Dict[str, Any] = {
                "ok": False,
                "error": "summary_transcript_empty",
                "phase": "idle",
                "transcript_count": 0,
            }
            if last_error:
                out["detail"] = last_error
            if isinstance(capture_stats, dict) and capture_stats:
                out["capture_stats"] = capture_stats
            return out, 400

        system_prompt, system_meta, prompt_err = _summary_resolve_system_prompt(payload_obj)
        if prompt_err:
            with summary_state_lock:
                state["phase"] = "idle"
            return {"ok": False, "error": prompt_err, "system_prompt_meta": system_meta}, 400

        instruction = str(payload_obj.get("instruction") or "").strip()
        transcript_text = _summary_transcript_text(transcripts)
        rendered = input_prompt_template.replace("{transcript}", transcript_text).replace("{instruction}", instruction)
        if "{transcript}" not in input_prompt_template:
            rendered = (rendered + "\n\nTranscript:\n" + transcript_text).strip()
        if "{instruction}" not in input_prompt_template and instruction:
            rendered = (rendered + "\n\nOpdracht:\n" + instruction).strip()

        messages: List[Dict[str, str]] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": rendered})

        pipeline = _get_pipeline(sid)
        try:
            llm_res = pipeline.llm.generate(messages)
        except Exception as exc:
            with summary_state_lock:
                state["phase"] = "idle"
                state["last_error"] = "llm: " + str(exc)
            return {"ok": False, "error": "summary_generation_failed", "detail": str(exc)}, 400
        draft_text = str(getattr(llm_res, "reply", "") or "").strip()
        if not draft_text:
            with summary_state_lock:
                state["phase"] = "idle"
                state["last_error"] = "llm: empty reply"
            return {"ok": False, "error": "summary_generation_empty"}, 400

        prompt_meta = {
            "system_prompt_source": system_meta.get("source"),
            "system_prompt_name": system_meta.get("name"),
            "instruction": instruction,
            "input_prompt_template": input_prompt_template,
        }
        with summary_state_lock:
            state["phase"] = "draft_ready"
            state["draft_text"] = draft_text
            state["last_prompt_meta"] = dict(prompt_meta)
            state["last_error"] = None
        return {
            "ok": True,
            "status": "accepted",
            "action": "do",
            "mode": "summary_capture_stop_and_draft",
            "phase": "draft_ready",
            "draft": draft_text,
            "transcript": transcripts,
            "transcript_count": len(transcripts),
            "prompt_meta": prompt_meta,
        }, 200

    def _summary_publish_impl(sid: str) -> Tuple[Dict[str, Any], int]:
        state = _get_summary_state(sid)
        with summary_state_lock:
            phase = str(state.get("phase") or "idle").strip().lower()
            draft_text = str(state.get("draft_text") or "").strip()
        if phase != "draft_ready" or not draft_text:
            return {"ok": False, "error": "summary_draft_not_ready", "phase": phase}, 400

        runtime_cfg = _get_runtime_cfg(sid)
        pipeline = _get_pipeline(sid)
        _emit_followup_text(
            pipeline=pipeline,
            emit_used="pipeline",
            runtime_cfg=runtime_cfg,
            text=draft_text,
        )
        with summary_state_lock:
            _summary_clear_state(state)
        return {
            "ok": True,
            "status": "accepted",
            "action": "do",
            "mode": "summary_publish",
            "phase": "idle",
            "published_text": draft_text,
        }, 200

    def _summary_cancel_impl(sid: str) -> Tuple[Dict[str, Any], int]:
        state = _get_summary_state(sid)
        _summary_stop_capture(state)
        with summary_state_lock:
            _summary_clear_state(state)
        return {
            "ok": True,
            "status": "accepted",
            "action": "do",
            "mode": "summary_cancel",
            "phase": "idle",
        }, 200

    @app.post("/api/script/do")
    def api_script_do():
        payload = request.get_json(force=True, silent=True) or {}
        mode = str(payload.get("mode") or "").strip().lower()
        sid = _get_sid()
        source = _infer_event_source(endpoint="/api/script/do", explicit_source=payload.get("source"))
        request_id = _get_request_id()
        if not mode:
            return jsonify({"ok": False, "error": "Missing 'mode'."}), 400
        result_payload: Dict[str, Any] = {"ok": True, "status": "accepted", "action": "do", "mode": mode}

        if mode == "command":
            label = str(payload.get("label") or "").strip()
            resolved = payload.get("resolved") if isinstance(payload.get("resolved"), dict) else None
            command_payload: Dict[str, Any] = {"label": label}
            if resolved is not None:
                command_payload["resolved"] = resolved
            out, status_code = _command_execute_impl(
                sid,
                command_payload,
                source=source,
                request_id=request_id,
            )
            if status_code != 200:
                resp = jsonify(out)
                resp.status_code = status_code
                resp.headers["X-Request-Id"] = request_id
                resp.set_cookie("sid", sid, max_age=60 * 60 * 24 * 7, samesite="Lax")
                return resp
        elif mode == "dance":
            dance_key = str(payload.get("dance_key") or "").strip()
            if not dance_key:
                return jsonify({"ok": False, "error": "missing_dance_key"}), 400
            pipeline = _get_pipeline(sid)
            runtime_cfg = _get_runtime_cfg(sid)
            _, cmdrec, _ = _pipeline_props(pipeline)
            if cmdrec is None:
                return jsonify({"ok": False, "error": "cmdrec_unavailable"}), 400
            resolved, err = _resolve_dance_by_key(cmdrec, dance_key, runtime_cfg=runtime_cfg)
            if err:
                return jsonify({"ok": False, "error": err, "dance_key": dance_key}), 400
            out, status_code = _command_execute_impl(
                sid,
                {"label": "DANCE", "resolved": resolved},
                source=source,
                request_id=request_id,
            )
            if status_code != 200:
                resp = jsonify(out)
                resp.status_code = status_code
                resp.headers["X-Request-Id"] = request_id
                resp.set_cookie("sid", sid, max_age=60 * 60 * 24 * 7, samesite="Lax")
                return resp
        elif mode == "behavior_start":
            behavior = str(payload.get("behavior") or "").strip()
            out, status_code = _nao_behavior_start_impl(
                sid,
                behavior,
                source=source,
                request_id=request_id,
            )
            if status_code != 200:
                resp = jsonify(out)
                resp.status_code = status_code
                resp.headers["X-Request-Id"] = request_id
                resp.set_cookie("sid", sid, max_age=60 * 60 * 24 * 7, samesite="Lax")
                return resp
        elif mode == "behavior_stop":
            behavior = str(payload.get("behavior") or "").strip()
            out, status_code = _nao_behavior_stop_impl(
                sid,
                behavior,
                source=source,
                request_id=request_id,
            )
            if status_code != 200:
                resp = jsonify(out)
                resp.status_code = status_code
                resp.headers["X-Request-Id"] = request_id
                resp.set_cookie("sid", sid, max_age=60 * 60 * 24 * 7, samesite="Lax")
                return resp
        elif mode == "summary_capture_start":
            out, status_code = _summary_capture_start_impl(sid)
            if status_code != 200:
                resp = jsonify(out)
                resp.status_code = status_code
                resp.headers["X-Request-Id"] = request_id
                resp.set_cookie("sid", sid, max_age=60 * 60 * 24 * 7, samesite="Lax")
                return resp
            result_payload = out
        elif mode == "summary_capture_stop_and_draft":
            out, status_code = _summary_capture_stop_and_draft_impl(sid, payload)
            if status_code != 200:
                resp = jsonify(out)
                resp.status_code = status_code
                resp.headers["X-Request-Id"] = request_id
                resp.set_cookie("sid", sid, max_age=60 * 60 * 24 * 7, samesite="Lax")
                return resp
            result_payload = out
        elif mode == "summary_publish":
            out, status_code = _summary_publish_impl(sid)
            if status_code != 200:
                resp = jsonify(out)
                resp.status_code = status_code
                resp.headers["X-Request-Id"] = request_id
                resp.set_cookie("sid", sid, max_age=60 * 60 * 24 * 7, samesite="Lax")
                return resp
            result_payload = out
        elif mode == "summary_cancel":
            out, status_code = _summary_cancel_impl(sid)
            if status_code != 200:
                resp = jsonify(out)
                resp.status_code = status_code
                resp.headers["X-Request-Id"] = request_id
                resp.set_cookie("sid", sid, max_age=60 * 60 * 24 * 7, samesite="Lax")
                return resp
            result_payload = out
        else:
            return jsonify({"ok": False, "error": "invalid_mode", "mode": mode}), 400

        _touch_activity()
        event_data: Dict[str, Any] = {"action": "do", "mode": mode}
        if "phase" in result_payload:
            event_data["phase"] = result_payload.get("phase")
        if "transcript_count" in result_payload:
            event_data["transcript_count"] = result_payload.get("transcript_count")
        _log_dm_event(
            sid=sid,
            level="info",
            category="dialog",
            event="dialog_reply",
            source=source,
            message="Script API action accepted.",
            request_id=request_id,
            data=event_data,
        )
        resp = jsonify(result_payload)
        resp.headers["X-Request-Id"] = request_id
        resp.set_cookie("sid", sid, max_age=60 * 60 * 24 * 7, samesite="Lax")
        return resp

    @app.get("/api/nao_behaviors")
    def api_nao_behaviors():
        runtime_cfg = _get_runtime_cfg(_get_sid())
        base_url, mode = _resolve_nao_behavior_endpoint(runtime_cfg, require_ping=True)
        if not base_url or mode not in ("behavior", "base"):
            return jsonify({"ok": False, "error": "Base/behavior manager is down."}), 400
        behaviors = _available_nao_behaviors(runtime_cfg)
        if behaviors is None:
            return jsonify({"ok": False, "error": "Kon behaviors niet ophalen."}), 400
        return jsonify({"ok": True, "behaviors": sorted(behaviors), "mode": mode})

    @app.post("/api/nao_behavior_start")
    def api_nao_behavior_start():
        payload = request.get_json(force=True, silent=True) or {}
        sid = _get_sid()
        behavior = (payload.get("behavior") or "").strip()
        source = _infer_event_source(endpoint="/api/nao_behavior_start", explicit_source=payload.get("source"))
        request_id = _get_request_id()
        out, status_code = _nao_behavior_start_impl(
            sid,
            behavior,
            source=source,
            request_id=request_id,
        )
        resp = jsonify(out)
        resp.status_code = status_code
        resp.headers["X-Request-Id"] = request_id
        resp.set_cookie("sid", sid, max_age=60 * 60 * 24 * 7, samesite="Lax")
        return resp

    @app.post("/api/nao_behavior_stop")
    def api_nao_behavior_stop():
        payload = request.get_json(force=True, silent=True) or {}
        sid = _get_sid()
        behavior = (payload.get("behavior") or "").strip()
        source = _infer_event_source(endpoint="/api/nao_behavior_stop", explicit_source=payload.get("source"))
        request_id = _get_request_id()
        out, status_code = _nao_behavior_stop_impl(
            sid,
            behavior,
            source=source,
            request_id=request_id,
        )
        resp = jsonify(out)
        resp.status_code = status_code
        resp.headers["X-Request-Id"] = request_id
        resp.set_cookie("sid", sid, max_age=60 * 60 * 24 * 7, samesite="Lax")
        return resp

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
            _clear_proc_log_files(name)
        return jsonify({"ok": True, "name": name})

    @app.get("/api/dm_events")
    def api_dm_events():
        raw_limit = request.args.get("limit", None)
        try:
            limit = int(raw_limit) if raw_limit is not None else 400
        except Exception:
            limit = 400
        limit = max(1, min(2000, limit))
        level_filter = str(request.args.get("level") or "").strip().lower()
        event_filter = str(request.args.get("event") or "").strip()
        source_filter = str(request.args.get("source") or "").strip().lower()
        q_filter = str(request.args.get("q") or "").strip().casefold()

        with dm_event_lock:
            events = list(dm_events)
            dropped_count = int(dm_event_dropped_writes)

        def _matches(item: Dict[str, Any]) -> bool:
            if level_filter and str(item.get("level") or "").strip().lower() != level_filter:
                return False
            if event_filter and str(item.get("event") or "").strip() != event_filter:
                return False
            if source_filter and str(item.get("source") or "").strip().lower() != source_filter:
                return False
            if q_filter:
                message_text = str(item.get("message") or "")
                try:
                    data_text = json.dumps(item.get("data") or {}, ensure_ascii=False, sort_keys=True)
                except Exception:
                    data_text = str(item.get("data") or "")
                hay = (message_text + " " + data_text).casefold()
                if q_filter not in hay:
                    return False
            return True

        filtered = [entry for entry in events if _matches(entry)]
        if limit > 0:
            filtered = filtered[-limit:]
        available_events = sorted({str(entry.get("event") or "").strip() for entry in events if entry.get("event")})
        return jsonify(
            {
                "ok": True,
                "events": filtered,
                "meta": {
                    "available_levels": list(_DM_EVENT_LEVELS),
                    "available_events": available_events,
                    "available_sources": list(_DM_EVENT_SOURCES),
                    "dropped_writes": dropped_count,
                },
            }
        )

    @app.post("/api/dm_events_clear")
    def api_dm_events_clear():
        nonlocal dm_event_dropped_writes
        with dm_event_lock:
            dm_events.clear()
            dm_event_pending_writes.clear()
            dm_event_dropped_writes = 0
        _clear_dm_event_files()
        return jsonify({"ok": True})

    @app.post("/api/process_start")
    def api_process_start():
        payload = request.get_json(force=True, silent=True) or {}
        name = (payload.get("name") or "").strip().lower()
        if name not in ("base", "behavior"):
            return jsonify({"ok": False, "error": "Unknown process name."}), 400
        sid = _get_sid()
        source = _infer_event_source(endpoint="/api/process_start", explicit_source=payload.get("source"))
        request_id = _get_request_id()

        payload_nao_base_url = _normalize_url(payload.get("nao_base_url"))
        payload_behavior_url = _normalize_url(payload.get("behavior_manager_url"))

        if name == "base":
            runtime_cfg = _get_runtime_cfg(sid)
            before_cfg = copy.deepcopy(runtime_cfg)
            payload_nao_ip = (payload.get("nao_ip") or "").strip()
            nao_ip = payload_nao_ip or (runtime_cfg.get("nao_ip") or "").strip()
            nao_ip_enabled = bool(payload.get("nao_ip_enabled", runtime_cfg.get("nao_ip_enabled", False)))
            effective_nao_base_url = payload_nao_base_url or _normalize_url(runtime_cfg.get("nao_base_url"))
            if _url_conflicts_with_webapp(effective_nao_base_url, request.host):
                _web_host, web_port = _host_header_host_port(request.host)
                return jsonify(
                    {
                        "ok": False,
                        "error": (
                            f"nao_base_url wijst naar de dialog manager zelf op poort {web_port}. "
                            "Kies een andere poort voor de base controller."
                        ),
                        "field": "nao_base_url",
                        "web_port": web_port,
                    }
                ), 400
            changed = False
            if payload_nao_ip:
                runtime_cfg["nao_ip"] = payload_nao_ip
                changed = True
            if "nao_ip_enabled" in payload:
                runtime_cfg["nao_ip_enabled"] = nao_ip_enabled
                changed = True
            if payload_nao_base_url:
                runtime_cfg["nao_base_url"] = payload_nao_base_url
                changed = True
            if changed:
                _save_runtime_cfg_state()
                _log_runtime_cfg_change(
                    sid=sid,
                    before=before_cfg,
                    after=runtime_cfg,
                    source=source,
                    request_id=request_id,
                    reason="process_start_base",
                )
            if not nao_ip or not nao_ip_enabled:
                return jsonify({"ok": False, "error": "NAO IP ontbreekt of is disabled."}), 400
            base_port = _url_port(runtime_cfg.get("nao_base_url"), default_port=5000)
            cmd, cwd, err = _base_controller_cmd(nao_ip, base_port)
        else:
            runtime_cfg = _get_runtime_cfg(sid)
            before_cfg = copy.deepcopy(runtime_cfg)
            effective_nao_base_url = payload_nao_base_url or _normalize_url(runtime_cfg.get("nao_base_url"))
            effective_behavior_url = payload_behavior_url or _normalize_url(runtime_cfg.get("behavior_manager_url"))
            if _url_conflicts_with_webapp(effective_nao_base_url, request.host):
                _web_host, web_port = _host_header_host_port(request.host)
                return jsonify(
                    {
                        "ok": False,
                        "error": (
                            f"nao_base_url wijst naar de dialog manager zelf op poort {web_port}. "
                            "Kies een andere poort voor de base controller."
                        ),
                        "field": "nao_base_url",
                        "web_port": web_port,
                    }
                ), 400
            if _url_conflicts_with_webapp(effective_behavior_url, request.host):
                _web_host, web_port = _host_header_host_port(request.host)
                return jsonify(
                    {
                        "ok": False,
                        "error": (
                            f"behavior_manager_url wijst naar de dialog manager zelf op poort {web_port}. "
                            "Kies een andere poort voor de behavior manager."
                        ),
                        "field": "behavior_manager_url",
                        "web_port": web_port,
                    }
                ), 400
            changed = False
            if payload_nao_base_url:
                runtime_cfg["nao_base_url"] = payload_nao_base_url
                changed = True
            if payload_behavior_url:
                runtime_cfg["behavior_manager_url"] = payload_behavior_url
                changed = True
            if changed:
                _save_runtime_cfg_state()
                _log_runtime_cfg_change(
                    sid=sid,
                    before=before_cfg,
                    after=runtime_cfg,
                    source=source,
                    request_id=request_id,
                    reason="process_start_behavior",
                )
            behavior_port = _url_port(runtime_cfg.get("behavior_manager_url"), default_port=5001)
            py2_api_url = _normalize_url(runtime_cfg.get("nao_base_url")) or "http://127.0.0.1:5000"
            cmd, cwd, err = _behavior_manager_cmd(behavior_port, py2_api_url)

        if err:
            return jsonify({"ok": False, "error": err}), 400
        result = _start_process(name, cmd, cwd)
        resp = jsonify(result)
        resp.headers["X-Request-Id"] = request_id
        return resp

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
    def _resolve_web_port(instance_id: Optional[str], explicit_port: Optional[int]) -> int:
        if explicit_port is not None:
            return int(explicit_port)
        defaults = {
            "alex": 5301,
            "renee": 5302,
        }
        key = str(instance_id or "").strip().casefold()
        return int(defaults.get(key, 8080))

    ap = argparse.ArgumentParser()
    ap.add_argument("--config", help="Pad naar configs/<file>.json")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument(
        "--instance-id",
        help="Unieke runtime instance id (alex->5301, renee->5302, anders default: port_<port>).",
    )
    args = ap.parse_args()

    web_port = _resolve_web_port(args.instance_id, args.port)
    config_path = args.config or DEFAULT_CONFIG_PATH
    cfg = _load_json(config_path)
    instance_id = str(args.instance_id or f"port_{web_port}")
    app, _, _ = create_app(cfg=cfg, config_path=os.path.abspath(config_path), instance_id=instance_id)
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
    #app.run(host=args.host, port=web_port, debug=False, threaded=True, ssl_context="adhoc")
    app.run(host=args.host, port=web_port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
