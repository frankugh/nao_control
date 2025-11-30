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
import configparser

from nao_actions import NaoActions

DEFAULT_PY3_WEB_HOST = "0.0.0.0"
DEFAULT_PY3_WEB_PORT = 5001
DEFAULT_PY2_API_URL  = "http://127.0.0.1:5000"


def load_config():
    """
    Leest config.ini één map hoger.
    """
    base_dir = os.path.dirname(os.path.abspath(__file__))
    root_dir = os.path.dirname(base_dir)
    ini_path = os.path.join(root_dir, "config.ini")

    cfg = {
        "WEB_HOST":        DEFAULT_PY3_WEB_HOST,
        "WEB_PORT":        DEFAULT_PY3_WEB_PORT,
        "PY2_NAO_API_URL": DEFAULT_PY2_API_URL,
    }

    if os.path.exists(ini_path):
        parser = configparser.ConfigParser()
        parser.read(ini_path)

        if parser.has_section("py3_server"):
            if parser.has_option("py3_server", "WEB_HOST"):
                cfg["WEB_HOST"] = parser.get("py3_server", "WEB_HOST")
            if parser.has_option("py3_server", "WEB_PORT"):
                cfg["WEB_PORT"] = parser.getint("py3_server", "WEB_PORT")
            if parser.has_option("py3_server", "PY2_NAO_API_URL"):
                cfg["PY2_NAO_API_URL"] = parser.get("py3_server", "PY2_NAO_API_URL")

    return cfg

def create_app(py2_api_url=None):
    if py2_api_url is None:
        cfg = load_config()
        py2_api_url = cfg["PY2_NAO_API_URL"]

    app = Flask(__name__)
    nao_actions = NaoActions(py2_api_url)

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
    cfg = load_config()
    app = create_app(cfg["PY2_NAO_API_URL"])

    host = cfg["WEB_HOST"]
    port = cfg["WEB_PORT"]

    print("Py3 NAO API beschikbaar op: http://%s:%s" % (host, port))
    print("Proxy naar Py2 NAO API op:", cfg["PY2_NAO_API_URL"])

    app.run(host=host, port=port, debug=False, use_reloader=False)
