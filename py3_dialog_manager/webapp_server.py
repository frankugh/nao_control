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
import json
import os
import secrets
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

from flask import Flask, jsonify, request, send_from_directory

try:
    from dialog.pipeline import InputLLMOutputPipeline
    from dialog.pipeline_builder import build_pipeline_from_config, make_stt_backend_from_config
    from dialog.backends.input_fixed_text import FixedTextInputBackend
    from dialog.backends.output_none import NoOpOutputBackend
    from dialog.interfaces import UtteranceAudio, parse_confirm_method
except Exception as e:  # pragma: no cover
    raise RuntimeError(
        "Kan projectmodules niet importeren. Run dit script vanaf de repo root, "
        "of zorg dat PYTHONPATH de repo root bevat. Originele fout: "
        + repr(e)
    )

JsonLike = Dict[str, Any]
History = List[Dict[str, str]]


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

    base_pipeline = build_pipeline_from_config(cfg, config_path=config_path)
    stt = make_stt_backend_from_config(cfg)

    # In-memory session histories for intranet testing
    sessions: Dict[str, History] = {}
    pending_confirms: Dict[str, Dict[str, Any]] = {}
    cmdrec_state: Dict[str, Dict[str, Any]] = {}
    runtime_context_state: Dict[str, Dict[str, Any]] = {}

    confirm_method = parse_confirm_method(cfg.get("confirm_method", "web"))
    confirm_timeout_s = float(cfg.get("confirm_timeout_s", 10.0))
    debug_cmdrec = bool(getattr(base_pipeline, "_debug_cmdrec", False))
    cmdrec = getattr(base_pipeline, "_cmdrec", None)
    behavior_executor = getattr(base_pipeline, "_behavior_executor", None)

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

    def _set_last_action(sid: str, summary: Optional[str]) -> None:
        _get_runtime_state(sid)["last_action"] = summary

    def _format_action_summary(prefix: str, label: str, resolved: Optional[Dict[str, Any]]) -> str:
        if resolved:
            items = ", ".join([f"{k}={v}" for k, v in sorted(resolved.items())])
            return f"{prefix}: {label}; resolved: {items}"
        return f"{prefix}: {label}"

    def _system_prompt_for_sid(sid: str) -> str:
        last_action = _get_runtime_state(sid).get("last_action")
        if hasattr(base_pipeline, "get_system_prompt"):
            sp = base_pipeline.get_system_prompt(last_action=last_action)
            return (sp or "").strip()
        sp = getattr(base_pipeline, "system_prompt", None)
        return (sp or "").strip()

    def _append_assistant(history: History, text: str) -> None:
        history.append({"role": "assistant", "content": text})

    def _append_user(history: History, text: str) -> None:
        history.append({"role": "user", "content": text})

    def _append_confirm_card(history: History, payload: Dict[str, Any]) -> None:
        history.append(payload)

    def _make_debug(decision) -> Optional[Dict[str, Any]]:
        if not debug_cmdrec:
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
        if "audio" in request.files:
            wav_bytes = request.files["audio"].read()
        else:
            wav_bytes = request.get_data()

        if not wav_bytes:
            return jsonify({"ok": False, "error": "Geen audio ontvangen."}), 400

        audio = UtteranceAudio(pcm=wav_bytes, sample_rate=16000, channels=1, sample_width=2)
        res = stt.transcribe(audio)
        return jsonify({"ok": True, "transcript": res.text, "language": getattr(res, "language", "")})

    @app.post("/api/send")
    def api_send():
        payload = request.get_json(force=True, silent=True) or {}
        text = (payload.get("text") or "").strip()
        if not text:
            return jsonify({"ok": False, "error": "Lege tekst."}), 400

        emit_req = (payload.get("emit") or "pipeline")
        emit_used = "pipeline" if str(emit_req).lower() == "pipeline" else "none"

        sid = _get_sid()
        if payload.get("reset") is True:
            sessions[sid] = []
            _set_last_action(sid, None)

        history = _get_history(sid)
        _expire_pending_if_needed(sid, history)

        pending = pending_confirms.get(sid)
        if pending and cmdrec is not None:
            state = _get_cmdrec_state(sid)
            decision = cmdrec.route(text, state["mode"], state["active_behavior"])
            debug = _make_debug(decision)

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
                _append_assistant(history, "✅ Uitgevoerd: STOP")
                resp = jsonify(
                    {
                        "ok": True,
                        "reply": "✅ Uitgevoerd: STOP",
                        "history": history,
                        "system_prompt": _system_prompt_for_sid(sid),
                        "emit_used": emit_used,
                        **({"debug": debug} if debug else {}),
                    }
                )
                resp.set_cookie("sid", sid, max_age=60 * 60 * 24 * 7, samesite="Lax")
                return resp

            _append_user(history, text)
            _append_assistant(history, "Bevestig of annuleer eerst.")
            resp = jsonify(
                {
                    "ok": True,
                    "reply": "Bevestig of annuleer eerst.",
                    "history": history,
                    "system_prompt": _system_prompt_for_sid(sid),
                    "emit_used": emit_used,
                    **({"debug": debug} if debug else {}),
                }
            )
            resp.set_cookie("sid", sid, max_age=60 * 60 * 24 * 7, samesite="Lax")
            return resp

        debug_for_response = None
        if cmdrec is not None:
            state = _get_cmdrec_state(sid)
            decision = cmdrec.route(text, state["mode"], state["active_behavior"])
            debug = _make_debug(decision)
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
                    _append_assistant(history, "✅ Uitgevoerd: STOP")
                    payload = {
                        "ok": True,
                        "reply": "✅ Uitgevoerd: STOP",
                        "history": history,
                        "system_prompt": _system_prompt_for_sid(sid),
                        "emit_used": emit_used,
                    }
                    if debug:
                        payload["debug"] = debug
                    resp = jsonify(payload)
                    resp.set_cookie("sid", sid, max_age=60 * 60 * 24 * 7, samesite="Lax")
                    return resp

                if cmdrec.is_guarded(decision.command.label) and confirm_method != "none":
                    _append_user(history, text)
                    confirmation_id = uuid.uuid4().hex
                    pending_confirms[sid] = {
                        "confirmation_id": confirmation_id,
                        "command": decision.command,
                        "created_at": time.time(),
                        "timeout_s": confirm_timeout_s,
                    }
                    confirm_payload = {
                        "type": "confirm_required",
                        "role": "assistant",
                        "confirmation_id": confirmation_id,
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
                    resp = jsonify(payload)
                    resp.set_cookie("sid", sid, max_age=60 * 60 * 24 * 7, samesite="Lax")
                    return resp

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
                _append_assistant(history, f"✅ Uitgevoerd: {decision.command.label}")
                payload = {
                    "ok": True,
                    "reply": f"✅ Uitgevoerd: {decision.command.label}",
                    "history": history,
                    "system_prompt": _system_prompt_for_sid(sid),
                    "emit_used": emit_used,
                }
                if debug:
                    payload["debug"] = debug
                resp = jsonify(payload)
                resp.set_cookie("sid", sid, max_age=60 * 60 * 24 * 7, samesite="Lax")
                return resp

        # Default is pipeline output; UI can opt out by sending emit="none".
        output_backend = base_pipeline.output if emit_used == "pipeline" else NoOpOutputBackend()
        status_to_console = base_pipeline.status_to_console if emit_used == "pipeline" else False

        base_prompt = getattr(base_pipeline, "system_prompt_base", None)
        if base_prompt is None:
            base_prompt = getattr(base_pipeline, "system_prompt", None)
        runtime_context_enabled = bool(
            getattr(base_pipeline, "runtime_context_enabled", False)
            or getattr(base_pipeline, "_runtime_context_enabled", False)
        )
        runtime_context_static = getattr(base_pipeline, "runtime_context_static", None)
        if runtime_context_static is None:
            runtime_context_static = getattr(base_pipeline, "_runtime_context_static", None)
        last_action = _get_runtime_state(sid).get("last_action")

        pipeline = InputLLMOutputPipeline(
            input_backend=FixedTextInputBackend(text),
            llm=base_pipeline.llm,
            output_backend=output_backend,
            status_to_console=status_to_console,
            system_prompt=base_prompt,
            log_messages_path=base_pipeline.log_messages_path,
            log_meta=base_pipeline.log_meta,
            max_history_turns=base_pipeline.max_history_turns,
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
        debug = debug_for_response

        payload = {
            "ok": True,
            "reply": reply,
            "history": sessions[sid],
            "system_prompt": _system_prompt_for_sid(sid),
            "emit_used": emit_used,
        }
        if debug:
            payload["debug"] = debug
        resp = jsonify(payload)
        resp.set_cookie("sid", sid, max_age=60 * 60 * 24 * 7, samesite="Lax")
        return resp

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

        if confirmed:
            if behavior_executor:
                behavior_executor.execute(cmd)
            _set_last_action(sid, _format_action_summary("Uitgevoerd", cmd.label, cmd.resolved))
            _append_assistant(history, f"✅ Uitgevoerd: {cmd.label}")
        else:
            _set_last_action(sid, _format_action_summary("Geannuleerd", cmd.label, cmd.resolved))
            _append_assistant(history, "❌ Geannuleerd")

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

    return app, stt, base_pipeline


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True, help="Pad naar configs/<file>.json")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8080)
    args = ap.parse_args()

    cfg = _load_json(args.config)
    app, _, _ = create_app(cfg=cfg, config_path=os.path.abspath(args.config))
    #app.run(host=args.host, port=args.port, debug=False, threaded=True, ssl_context="adhoc")
    app.run(host=args.host, port=args.port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
