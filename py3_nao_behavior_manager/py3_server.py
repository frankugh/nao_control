# py3_server.py
"""
Py3-webAPI voor NAO-aansturing.

Deze laag praat:
- aan de buitenkant met QT/NAO-scriptrunners (alleen Py3),
- aan de binnenkant met de legacy Py2-NAO-API (via NaoActions).

Doel:
- Een stabiel, toekomstbestendig contract (/nao/...) bieden.
- Py2-implementatie later vervangbaar maken zonder scripts te breken.
"""

import os

from flask import Flask, request, jsonify
import requests

from nao_actions import NaoActions


def create_app():
    app = Flask(__name__)

    # Py2-NAO-API basis-URL; aanpasbaar via env:
    #   set PY2_NAO_API_URL=http://192.168.0.110:5000
    py2_base_url = os.environ.get("PY2_NAO_API_URL", "http://192.168.68.62:5000")
    nao_actions = NaoActions(py2_base_url)

    # ===== interne helper voor uniform error-handling =====

    def _wrap_py2_call(func, *args, **kwargs):
        """
        Voer een NaoActions-methode uit en vertaal errors naar nette HTTP-responses.
        """
        try:
            result = func(*args, **kwargs)
            # Py2-return is al een JSON-dict in de vorm {"status": "...", "data": ..., "error": ...}
            return jsonify(result)
        except requests.RequestException as e:
            return jsonify({
                "status": "error",
                "error": "Py2 NAO API request failed",
                "details": str(e),
            }), 502

    # ===== generieke healthcheck =====

    @app.route("/ping", methods=["GET"])
    def ping():
        """Healthcheck van de Py3-laag zelf."""
        return jsonify({"status": "ok", "data": "pong"})

    # ===== NAO endpoints (Py3-contract, praten via NaoActions met Py2) =====

    @app.route("/nao/ping", methods=["GET"])
    def nao_ping():
        """Healthcheck van de onderliggende Py2-NAO-API."""
        return _wrap_py2_call(nao_actions.ping)

    @app.route("/nao/wake_up", methods=["POST"])
    def nao_wake_up():
        return _wrap_py2_call(nao_actions.wake_up)

    @app.route("/nao/rest", methods=["POST"])
    def nao_rest():
        return _wrap_py2_call(nao_actions.rest)

    @app.route("/nao/tts", methods=["POST"])
    def nao_tts():
        data = request.get_json(force=True, silent=True) or {}
        text = (data.get("text") or "").strip()
        if not text:
            return jsonify({"status": "error", "error": "Missing 'text'"}), 400
        return _wrap_py2_call(nao_actions.say_native, text)

    @app.route("/nao/tts_speed", methods=["POST"])
    def nao_tts_speed():
        data = request.get_json(force=True, silent=True) or {}
        speed = data.get("speed", None)
        if speed is None:
            return jsonify({"status": "error", "error": "Missing 'speed'"}), 400
        return _wrap_py2_call(nao_actions.set_tts_speed, speed)

    @app.route("/nao/set_volume", methods=["POST"])
    def nao_set_volume():
        data = request.get_json(force=True, silent=True) or {}
        volume = data.get("volume", None)
        if volume is None:
            return jsonify({"status": "error", "error": "Missing 'volume'"}), 400
        return _wrap_py2_call(nao_actions.set_volume, volume)

    @app.route("/nao/list_behaviors", methods=["GET"])
    def nao_list_behaviors():
        return _wrap_py2_call(nao_actions.list_behaviors)

    @app.route("/nao/do_behavior", methods=["POST"])
    def nao_do_behavior():
        data = request.get_json(force=True, silent=True) or {}
        bname = (data.get("behavior") or data.get("name") or "").strip()
        if not bname:
            return jsonify({"status": "error", "error": "Missing 'behavior'"}), 400
        return _wrap_py2_call(nao_actions.do_behavior, bname)

    @app.route("/nao/set_eye_color", methods=["POST"])
    def nao_set_eye_color():
        data = request.get_json(force=True, silent=True) or {}
        color = data.get("color")
        duration = data.get("duration", 0.5)
        if not color:
            return jsonify({"status": "error", "error": "Missing 'color'"}), 400
        return _wrap_py2_call(nao_actions.set_eye_color, color, duration)

    @app.route("/nao/naoqi/call", methods=["POST"])
    def nao_naoqi_call():
        data = request.get_json(force=True, silent=True) or {}
        module = data.get("module")
        method = data.get("method")
        args = data.get("args") or []
        kwargs = data.get("kwargs") or {}
        if not module or not method:
            return jsonify({"status": "error", "error": "Missing 'module' or 'method'"}), 400
        return _wrap_py2_call(nao_actions.naoqi_call, module, method, args, kwargs)

    # ===== file/audio legacy endpoints, nog via Py2 geproxied =====

    @app.route("/nao/upload_only", methods=["POST"])
    def nao_upload_only():
        if "file" not in request.files:
            return jsonify({"status": "error", "error": "No file part"}), 400
        f = request.files["file"]
        if not f or not f.filename:
            return jsonify({"status": "error", "error": "Empty file"}), 400

        filename = request.form.get("filename") or f.filename
        remote_dir = request.form.get("remote_dir") or None

        return _wrap_py2_call(nao_actions.upload_only, f, filename, remote_dir)

    @app.route("/nao/play_audio", methods=["POST"])
    def nao_play_audio():
        if "file" not in request.files:
            return jsonify({"status": "error", "error": "No file part"}), 400
        f = request.files["file"]
        if not f or not f.filename:
            return jsonify({"status": "error", "error": "Empty file"}), 400

        filename = request.form.get("filename") or f.filename
        remote_dir = request.form.get("remote_dir") or None

        return _wrap_py2_call(nao_actions.play_audio, f, filename, remote_dir)

    @app.route("/nao/play_stream", methods=["POST"])
    def nao_play_stream():
        # raw body bevat audio-bytes (PCM S16_LE, mono) of wat Py2 verwacht
        audio_bytes = request.get_data()
        if not audio_bytes:
            return jsonify({"status": "error", "error": "Empty body"}), 400

        # Content-Type laten we doorlopen; als die niet gezet is valt Py2 terug op zijn default
        content_type = request.headers.get("Content-Type", "application/octet-stream")
        return _wrap_py2_call(nao_actions.play_stream, audio_bytes, content_type)

    return app


if __name__ == "__main__":
    app = create_app()
    host = os.environ.get("PY3_WEB_HOST", "0.0.0.0")
    port = int(os.environ.get("PY3_WEB_PORT", "5001"))
    print("Py3 NAO API beschikbaar op: http://%s:%s" % (host, port))
    print("Proxy naar Py2 NAO API op:", os.environ.get("PY2_NAO_API_URL", "http://127.0.0.1:5000"))
    app.run(host=host, port=port, debug=False, use_reloader=False)
