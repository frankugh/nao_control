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
from typing import Any, Dict, List, Optional, Tuple

from flask import Flask, jsonify, request, send_from_directory

try:
    from dialog.pipeline import InputLLMOutputPipeline
    from dialog.pipeline_builder import build_pipeline_from_config, make_stt_backend_from_config
    from dialog.backends.input_fixed_text import FixedTextInputBackend
    from dialog.backends.output_none import NoOpOutputBackend
    from dialog.interfaces import UtteranceAudio
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

    def _get_sid() -> str:
        sid = request.cookies.get("sid")
        if not sid:
            sid = secrets.token_urlsafe(16)
        return sid

    def _get_history(sid: str) -> History:
        return sessions.setdefault(sid, [])

    def _system_prompt() -> str:
        sp = getattr(base_pipeline, "system_prompt", None)
        return (sp or "").strip()

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
        resp = jsonify({"ok": True, "history": history, "system_prompt": _system_prompt()})
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

        emit_req = (payload.get("emit") or "none")
        emit_used = "pipeline" if str(emit_req).lower() == "pipeline" else "none"

        sid = _get_sid()
        if payload.get("reset") is True:
            sessions[sid] = []

        history = _get_history(sid)

        # Default is UI-only: no console status and no robot/TTS output unless requested.
        output_backend = base_pipeline.output if emit_used == "pipeline" else NoOpOutputBackend()
        status_to_console = base_pipeline.status_to_console if emit_used == "pipeline" else False

        pipeline = InputLLMOutputPipeline(
            input_backend=FixedTextInputBackend(text),
            llm=base_pipeline.llm,
            output_backend=output_backend,
            status_to_console=status_to_console,
            system_prompt=base_pipeline.system_prompt,
            log_messages_path=base_pipeline.log_messages_path,
            log_meta=base_pipeline.log_meta,
            max_history_turns=base_pipeline.max_history_turns,
        )

        turn = pipeline.run_once(history=history)
        reply = (turn.llm.reply or "").strip()

        sessions[sid] = _append_turn(history, text, reply)

        resp = jsonify(
            {
                "ok": True,
                "reply": reply,
                "history": sessions[sid],
                "system_prompt": _system_prompt(),
                "emit_used": emit_used,
            }
        )
        resp.set_cookie("sid", sid, max_age=60 * 60 * 24 * 7, samesite="Lax")
        return resp

    @app.post("/api/reset")
    def api_reset():
        sid = _get_sid()
        sessions[sid] = []
        resp = jsonify({"ok": True, "history": [], "system_prompt": _system_prompt()})
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
