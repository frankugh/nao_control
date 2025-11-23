# server.py
import os
from flask import Flask, request, jsonify
from nao_actions import NaoActions
import requests

def create_app():
    app = Flask(__name__)

    # Py2-NAO-API basis-URL; pas aan via env:
    #   set PY2_NAO_API_URL=http://192.168.0.110:5000
    py2_base_url = os.environ.get("PY2_NAO_API_URL", "http://192.168.0.110:5000")
    nao_actions = NaoActions(py2_base_url)

    @app.route("/ping", methods=["GET"])
    def ping():
        return jsonify({"status": "ok", "data": "pong"})

    @app.route("/nao/tts", methods=["POST"])
    def nao_tts():
        data = request.get_json(force=True) or {}
        text = data.get("text", "").strip()
        if not text:
            return jsonify({"status": "error", "error": "Missing 'text'"}), 400

        try:
            py2_response = nao_actions.say_native(text)
            # We wrap de Py2-response zodat je aan de buitenkant één uniforme Py3-API hebt.
            return jsonify({
                "status": "ok",
                "data": {
                    "text": text,
                    "py2": py2_response
                }
            })
        except requests.RequestException as e:
            return jsonify({
                "status": "error",
                "error": "Py2 NAO API request failed",
                "details": str(e)
            }), 502

    return app


if __name__ == "__main__":
    app = create_app()
    host = os.environ.get("PY3_WEB_HOST", "0.0.0.0")
    port = int(os.environ.get("PY3_WEB_PORT", "5001"))
    print("Py3 NAO API beschikbaar op: http://%s:%s" % (host, port))
    print("Proxy naar Py2 NAO API op:", os.environ.get("PY2_NAO_API_URL", "http://127.0.0.1:5000"))
    app.run(host=host, port=port, debug=False)
