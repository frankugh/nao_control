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
import subprocess
import socket
from typing import Any, Dict, List, Optional, Tuple

from flask import Flask, jsonify, request, send_from_directory
import requests

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
    from dialog.interfaces import UtteranceAudio, parse_confirm_method
except Exception as e:  # pragma: no cover
    raise RuntimeError(
        "Kan projectmodules niet importeren. Run dit script vanaf de repo root, "
        "of zorg dat PYTHONPATH de repo root bevat. Originele fout: "
        + repr(e)
    )

JsonLike = Dict[str, Any]
History = List[Dict[str, str]]

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
    mic_params = (mic_cfg.get("params", {}) or {})
    nao_ip = mic_params.get("host", "") if mic_cfg.get("type") == "nao_ssh" else ""
    if not nao_ip:
        nao_ip = cfg_src.get("nao_ip", "") or ""
    stt_cfg = (input_cfg.get("stt", {}) or {})

    llm_cfg = (cfg_src.get("llm", {}) or {})
    llm_params = (llm_cfg.get("params", {}) or {})
    master_prompt_file = llm_params.get("system_prompt_file", None)

    output_cfg = (cfg_src.get("output", {}) or {})
    output_type = (output_cfg.get("type") or "none").lower()

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
        "input_device": input_device,
        "stt_type": (stt_cfg.get("type") or "vosk"),
        "cmdrec": cfg_src.get("cmdrec", "latest"),
        "llm_type": llm_cfg.get("type", "echo"),
        "llm_model": llm_params.get("model", ""),
        "master_prompt_file": master_prompt_file,
        "output_nao_tts": output_type in ("nao_tts", "nao_py2", "nao"),
    }


def _stt_params_for_type(stt_type: str, cfg_src: JsonLike) -> JsonLike:
    stt_cfg = (cfg_src.get("input", {}) or {}).get("stt", {}) or {}
    if (stt_cfg.get("type") or "").lower() == stt_type.lower():
        return copy.deepcopy(stt_cfg.get("params", {}) or {})
    if stt_type.lower() == "vosk":
        return copy.deepcopy(DEFAULT_VOSK_PARAMS)
    return copy.deepcopy(DEFAULT_WHISPER_PARAMS)


def _apply_runtime_overrides(cfg_src: JsonLike, runtime_cfg: JsonLike) -> JsonLike:
    cfg_out = copy.deepcopy(cfg_src)
    input_cfg = cfg_out.setdefault("input", {})
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

    return cfg_out


def _make_mic_from_config(cfg: JsonLike):
    input_cfg = cfg.get("input", {}) or {}
    mic_cfg = input_cfg.get("mic", {}) or {}
    mic_type = (mic_cfg.get("type") or "").lower()
    mic_params = (mic_cfg.get("params") or {})
    if mic_type == "laptop":
        return LaptopMic(**mic_params)
    if mic_type == "nao_ssh":
        return NaoSshMic(**mic_params)
    raise ValueError(f"Onbekende mic.type: {mic_type!r}")


def _load_json(path: str) -> JsonLike:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _strip_system(history: History) -> History:
    if history and history[0].get("role") == "system":
        return history[1:]
    return history


def _append_turn(history: History, user_text: str, assistant_text: str) -> History:
    history = _strip_system(list(history))
    history.append({"role": "user", "content": user_text})
    history.append({"role": "assistant", "content": assistant_text})
    return history


def create_app(*, cfg: JsonLike, config_path: str) -> Tuple[Flask, Any, InputLLMOutputPipeline]:
    app = Flask(__name__, static_folder="web", static_url_path="")

    base_cfg = copy.deepcopy(cfg)
    base_config_path = config_path
    base_pipeline = build_pipeline_from_config(cfg, config_path=config_path)
    base_stt = make_stt_backend_from_config(cfg)
    pipeline_lock = threading.Lock()
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    al_dir = os.path.join(repo_root, "py3_command_recognition_train", "data", "al")
    al_lock = threading.Lock()
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
        return cmdrec_state.setdefault(sid, {"mode": "DIALOG", "active_behavior": None})

    def _get_runtime_state(sid: str) -> Dict[str, Any]:
        return runtime_context_state.setdefault(sid, {"last_action": None})

    def _get_continuous_state(sid: str) -> Dict[str, Any]:
        return continuous_state.setdefault(
            sid,
            {"running": False, "stop": None, "thread": None, "last_error": None, "phase": "idle"},
        )

    def _set_last_action(sid: str, summary: Optional[str]) -> None:
        _get_runtime_state(sid)["last_action"] = summary

    # runtime pipeline helpers
    def _get_runtime_cfg(sid: str) -> JsonLike:
        return runtime_cfg_by_sid.setdefault(sid, _extract_runtime_config(base_cfg))

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

    def _system_prompt_for_sid(sid: str) -> str:
        pipeline = _get_pipeline(sid)
        last_action = _get_runtime_state(sid).get("last_action")
        if hasattr(pipeline, "get_system_prompt"):
            sp = pipeline.get_system_prompt(last_action=last_action)
            return (sp or "").strip()
        sp = getattr(pipeline, "system_prompt", None)
        return (sp or "").strip()

    def _append_assistant(history: History, text: str) -> None:
        history.append({"role": "assistant", "content": text})

    def _append_user(history: History, text: str) -> None:
        history.append({"role": "user", "content": text})

    def _append_confirm_card(history: History, payload: Dict[str, Any]) -> None:
        history.append(payload)

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

    def _stop_all_processes() -> None:
        for name in ("behavior", "base"):
            _stop_process(name)

    atexit.register(_stop_all_processes)

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

    def _al_append(filename: str, payload: Dict[str, Any]) -> None:
        os.makedirs(al_dir, exist_ok=True)
        path = os.path.join(al_dir, filename)
        line = json.dumps(payload, ensure_ascii=False)
        with al_lock:
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(line + "\n")

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
        return jsonify({"ok": True, "transcript": res.text, "language": getattr(res, "language", "")})

    @app.post("/api/transcribe_mic")
    def api_transcribe_mic():
        sid = _get_sid()
        runtime_cfg = _get_runtime_cfg(sid)
        merged = _apply_runtime_overrides(base_cfg, runtime_cfg)
        input_cfg = merged.get("input", {}) or {}
        if (input_cfg.get("type") or "audio").lower() != "audio":
            return jsonify({"ok": False, "error": "Input type is geen audio."}), 400

        mic = _make_mic_from_config(merged)
        timeout_s = float((input_cfg.get("params") or {}).get("start_timeout_s") or 10.0)
        audio = mic.capture_utterance(timeout_s=timeout_s)
        res = _get_stt(sid).transcribe(audio)
        return jsonify({"ok": True, "transcript": res.text, "language": getattr(res, "language", "")})


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
        if cmdrec is not None:
            state = _get_cmdrec_state(sid)
            decision = cmdrec.route(text, state["mode"], state["active_behavior"])
            debug = _make_debug(decision, debug_cmdrec)
            debug_for_response = debug
            if decision.is_command and decision.command:
                if decision.command.label == "STOP":
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

                confirm_policy = (runtime_cfg.get("confirm_policy") or base_cfg.get("confirm_policy") or "when_guarded").lower()
                confirm_needed = False
                if confirm_method != "none":
                    if confirm_policy == "always":
                        confirm_needed = True
                    else:
                        confirm_needed = cmdrec.is_guarded(decision.command.label)

                if confirm_needed:
                    _append_user(history, text)
                    confirmation_id = uuid.uuid4().hex
                    pending_confirms[sid] = {
                        "confirmation_id": confirmation_id,
                        "command": decision.command,
                        "created_at": time.time(),
                        "timeout_s": confirm_timeout_s,
                        "input_text": text,
                        "input_meta": {"stt_used": stt_used, "stt_edited": stt_edited},
                        "top3": decision.top3,
                        "confidence": decision.command.confidence,
                        "label": decision.command.label,
                    }
                    confirm_payload = {
                        "type": "confirm_required",
                        "role": "assistant",
                        "confirmation_id": confirmation_id,
                        "created_at": pending_confirms[sid]["created_at"],
                        "timeout_s": pending_confirms[sid]["timeout_s"],
                        "prompt": f"Bevestig uitvoeren: {decision.command.label} ?",
                        "command": {
                            "label": decision.command.label,
                            "confidence": decision.command.confidence,
                            "raw_text": decision.command.raw_text,
                            "resolved": decision.command.resolved,
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
                    behavior_executor.execute(decision.command)
                state["mode"] = "PERFORMING"
                state["active_behavior"] = decision.command.label
                _set_last_action(
                    sid,
                    _format_action_summary(
                        "Uitgevoerd",
                        decision.command.label,
                        decision.command.resolved,
                    ),
                )
                _append_assistant(history, f"OK. Uitgevoerd: {decision.command.label}")
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
                    "reply": f"OK. Uitgevoerd: {decision.command.label}",
                    "history": history,
                    "system_prompt": _system_prompt_for_sid(sid),
                    "emit_used": emit_used,
                }
                if debug:
                    payload["debug"] = debug
                return payload

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

        output_enabled = bool(runtime_cfg.get("base_enabled", True))
        if output_enabled:
            base_url = _normalize_url(runtime_cfg.get("nao_base_url"))
            if not _check_ping(base_url, "/ping"):
                output_enabled = False
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
        state["phase"] = "listening"

        def _worker():
            try:
                while not stop_event.is_set():
                    state["phase"] = "listening"
                    runtime_cfg = _get_runtime_cfg(sid)
                    merged = _apply_runtime_overrides(base_cfg, runtime_cfg)
                    try:
                        mic = _make_mic_from_config(merged)
                    except Exception as exc:
                        state["last_error"] = str(exc)
                        state["phase"] = "idle"
                        time.sleep(1.0)
                        continue

                    timeout_s = float((merged.get("input", {}).get("params") or {}).get("start_timeout_s") or 10.0)
                    try:
                        audio = mic.capture_utterance(timeout_s=timeout_s)
                    except Exception as exc:
                        state["last_error"] = str(exc)
                        state["phase"] = "idle"
                        time.sleep(0.5)
                        continue

                    if stop_event.is_set():
                        break

                    state["phase"] = "transcribing"
                    try:
                        res = _get_stt(sid).transcribe(audio)
                    except Exception as exc:
                        state["last_error"] = str(exc)
                        state["phase"] = "idle"
                        continue

                    text_in = (res.text or "").strip()
                    state["phase"] = "thinking"
                    if not text_in:
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
                        state["phase"] = "listening"
                    except Exception as exc:
                        state["last_error"] = str(exc)
                        state["phase"] = "idle"
                        continue
            finally:
                state["running"] = False
                state["phase"] = "idle"

        t = threading.Thread(target=_worker, daemon=True)
        state["thread"] = t
        t.start()
        return jsonify({"ok": True, "running": True})

    @app.post("/api/continuous_stop")
    def api_continuous_stop():
        sid = _get_sid()
        state = _get_continuous_state(sid)
        stop_event = state.get("stop")
        if stop_event is not None:
            stop_event.set()
        state["running"] = False
        return jsonify({"ok": True, "running": False})

    @app.get("/api/continuous_state")
    def api_continuous_state():
        sid = _get_sid()
        state = _get_continuous_state(sid)
        return jsonify({"ok": True, "running": bool(state.get("running")), "error": state.get("last_error"), "phase": state.get("phase")})

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
                "auto_train.jsonl",
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

    @app.get("/api/audio_devices")
    def api_audio_devices():
        return jsonify({"ok": True, **_list_audio_devices()})

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
        output_enabled = bool(runtime_cfg.get("output_nao_tts", True))
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

    @app.post("/api/process_start")
    def api_process_start():
        payload = request.get_json(force=True, silent=True) or {}
        name = (payload.get("name") or "").strip().lower()
        if name not in ("base", "behavior"):
            return jsonify({"ok": False, "error": "Unknown process name."}), 400

        if name == "base":
            runtime_cfg = _get_runtime_cfg(_get_sid())
            nao_ip = (runtime_cfg.get("nao_ip") or "").strip()
            if not nao_ip or not runtime_cfg.get("nao_ip_enabled", False):
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
    #app.run(host=args.host, port=args.port, debug=False, threaded=True, ssl_context="adhoc")
    app.run(host=args.host, port=args.port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
