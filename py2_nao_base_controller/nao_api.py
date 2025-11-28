# -*- coding: utf-8 -*-
import argparse
import os
import socket
import sys
import traceback
import json
from flask import Flask, request, jsonify
from naoqi import ALProxy
from nao_utils import NaoUtils, set_eye_color, group_behaviors, DEFAULT_REMOTE_AUDIO_DIR

# ====== Defaults ======
DEFAULT_WEB_HOST = "0.0.0.0"
DEFAULT_WEB_PORT = 5000
DEFAULT_NAO_IP   = "192.168.0.101"
DEFAULT_NAO_PORT = 9559
DEFAULT_SSH_USER = "nao"
DEFAULT_SSH_PASS = "nao"
DEFAULT_SSH_PORT = 22

# ====== Flask app ======
app = Flask(__name__)


# ====== Helpers ======

def make_response(status="ok", data=None, error=None):
    """
    Uniform JSON-response.
    status: "ok" | "error" | "warning"
    data  : payload (alles wat je wilt)
    error : string met foutmelding (optioneel)
    """
    payload = {"status": status}
    if data is not None:
        payload["data"] = data
    if error is not None:
        payload["error"] = error
    return jsonify(payload)


def get_proxy(name):
    """
    Haal een ALProxy met de NAO_IP/NAO_PORT uit de app-config.
    """
    ip = app.config["NAO_IP"]
    port = app.config["NAO_PORT"]
    return ALProxy(name, ip, port)


def is_awake():
    """
    Checkt of de robot 'wakker' is via ALMotion.robotIsWakeUp.
    """
    motion = get_proxy("ALMotion")
    try:
        return bool(motion.robotIsWakeUp())
    except AttributeError:
        # Oudere NAOqi-versies kunnen dit niet hebben; val terug op isFallManagerEnabled
        try:
            return bool(motion.isFallManagerEnabled())
        except Exception:
            return True


def _utils():
    """
    Maak een NaoUtils instance met de juiste SSH-config uit Flask config.
    """
    return NaoUtils(
        nao_ip=app.config["NAO_IP"],
        nao_port=app.config["NAO_PORT"],
        ssh_user=app.config["NAO_SSH_USER"],
        ssh_pass=app.config["NAO_SSH_PASS"],
        ssh_port=app.config["NAO_SSH_PORT"],
        remote_audio_dir=app.config["NAO_REMOTE_AUDIO_DIR"],
    )


def _get_local_ip():
    """
    Bepaal een 'beste gok' van het lokale IP om in de console te tonen.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        try:
            s.close()
        except Exception:
            pass


# ====== Routes ======

@app.route("/ping", methods=["GET"])
def ping():
    """
    Healthcheck voor de web-API.
    """
    return make_response(data="pong")


@app.route("/wake_up", methods=["POST"])
def wake_up():
    try:
        motion = get_proxy("ALMotion")
        if not is_awake():
            motion.wakeUp()
            return make_response(data="NAO woken up")
        else:
            return make_response(data="NAO already awake")
    except Exception as e:
        return make_response(status="error", error=repr(e))


@app.route("/rest", methods=["POST"])
def rest():
    try:
        motion = get_proxy("ALMotion")
        if is_awake():
            motion.rest()
            return make_response(data="NAO resting")
        else:
            return make_response(data="NAO already resting")
    except Exception as e:
        return make_response(status="error", error=repr(e))


@app.route("/tts", methods=["POST"])
def tts_say():
    try:
        payload = request.get_json(force=True) or {}
        text = payload.get("text", u"")

        try:
            unicode
        except NameError:
            unicode = str

        # Zorg dat het UNICODE wordt, niet bytes
        if not isinstance(text, unicode):
            # als het bytes is -> decode, anders cast
            if isinstance(text, str):
                text = text.decode("utf-8")
            else:
                text = unicode(text)

        tts = get_proxy("ALTextToSpeech")
        tts.say(text)
        return make_response(data={"text": text})
    except Exception as e:
        return make_response(status="error", error=repr(e))


@app.route("/list_behaviors", methods=["GET"])
def list_behaviors_ep():
    """
    Geef alle geïnstalleerde behaviors gegroepeerd per folder terug.
    """
    try:
        mgr = get_proxy("ALBehaviorManager")
        behaviors = mgr.getInstalledBehaviors()
        grouped = group_behaviors(behaviors)
        return make_response(data=grouped)
    except Exception as e:
        return make_response(status="error", error=repr(e))


@app.route("/do_behavior", methods=["POST"])
def do_behavior():
    try:
        payload = request.get_json(force=True) or {}
        bname = payload.get("behavior")

        if not bname:
            return make_response(status="error", error="Missing 'behavior'")

        try:
            unicode
        except NameError:
            unicode = str

        # Zorg dat het UNICODE is, niet bytes
        if not isinstance(bname, unicode):
            if isinstance(bname, str):
                bname = bname.decode("utf-8")
            else:
                bname = unicode(bname)

        behavior = get_proxy("ALBehaviorManager")

        if not behavior.isBehaviorInstalled(bname):
            return make_response(status="error", error="Behavior not installed: " + bname)

        if not is_awake():
            return make_response(
                status="warning",
                data="Robot is resting, some behaviors may not run correctly"
            )

        behavior.runBehavior(bname)
        return make_response(data="Ran behavior: " + bname)
    except Exception as e:
        return make_response(status="error", error=repr(e))


@app.route("/tts_speed", methods=["POST"])
def tts_speed():
    """
    Zet TTS-snelheid.
    Body: { "speed": 80 }   # typisch bereik 50–100
    """
    try:
        payload = request.get_json(force=True) or {}
        speed = payload.get("speed", None)
        if speed is None:
            return make_response(status="error", error="Missing 'speed'")
        speed = int(speed)

        tts = get_proxy("ALTextToSpeech")
        tts.setParameter("speed", speed)

        return make_response(data={"speed": speed})
    except Exception as e:
        return make_response(status="error", error=repr(e))


@app.route("/set_volume", methods=["POST"])
def set_volume():
    """
    Zet outputvolume.
    Body: { "volume": 30 }   # 0–100
    """
    try:
        payload = request.get_json(force=True) or {}
        volume = payload.get("volume", None)
        if volume is None:
            return make_response(status="error", error="Missing 'volume'")
        volume = int(volume)

        audio_dev = get_proxy("ALAudioDevice")
        audio_dev.setOutputVolume(volume)

        return make_response(data={"volume": volume})
    except Exception as e:
        return make_response(status="error", error=repr(e))


@app.route("/set_eye_color", methods=["POST"])
def set_eye_color_ep():
    """
    Zet de oogkleur (FaceLeds) op een bepaalde kleur.
    Body: { "color": "#RRGGBB", "duration": 0.5 }
    """
    try:
        payload = request.get_json(force=True) or {}
        color = payload.get("color")
        duration = float(payload.get("duration", 0.5))
        if color is None:
            return make_response(status="error", error="Missing 'color'")
        rgb = set_eye_color(app.config["NAO_IP"], app.config["NAO_PORT"], color, duration)
        return make_response(data={"rgb": int(rgb), "duration": duration})
    except Exception as e:
        return make_response(status="error", error=repr(e))
    

@app.route("/naoqi/call", methods=["POST"])
def naoqi_call():
    payload = request.get_json(force=True, silent=True) or {}
    module_name = payload.get("module")
    method_name = payload.get("method")
    args = payload.get("args") or []
    kwargs = payload.get("kwargs") or {}

    if not module_name or not method_name:
        return jsonify({
            "status": "error",
            "error": "Missing 'module' or 'method'"
        })

    try:
        result = naoqi_call_generic(module_name, method_name, args, kwargs)
        # zorg dat resultaat JSON-serialiseerbaar is
        try:
            json.dumps(result)
            safe_result = result
        except TypeError:
            safe_result = repr(result)

        return jsonify({
            "status": "ok",
            "data": {
                "result": safe_result
            }
        })
    except Exception as e:
        return jsonify({
            "status": "error",
            "error": repr(e)
        })
    

def naoqi_call_generic(module_name, method_name, args=None, kwargs=None):
    if args is None:
        args = []
    if kwargs is None:
        kwargs = {}

    try:
        unicode_type = unicode  # Py2
    except NameError:
        unicode_type = str      # Py3 fallback

    # 1) module/method als bytes/str
    if isinstance(module_name, unicode_type):
        module_name = module_name.encode("utf-8")
    if isinstance(method_name, unicode_type):
        method_name = method_name.encode("utf-8")

    proxy = get_proxy(module_name)
    method = getattr(proxy, method_name)

    # 2) args/kwargs als bytes/str voor NAOqi
    def ensure_naoqi_arg(x):
        if isinstance(x, unicode_type):
            return x.encode("utf-8")  # unicode -> bytes
        return x                     # str/bytes/nummers etc. laten staan

    args = [ensure_naoqi_arg(a) for a in args]
    kwargs = {k: ensure_naoqi_arg(v) for (k, v) in kwargs.items()}

    return method(*args, **kwargs)


# === DEPRECATED ===
# File-upload via deze endpoint blijft werken voor bestaande code,
# maar nieuwe functionaliteit moet via de Py3-NAO-transportlaag lopen.
@app.route("/upload_only", methods=["POST"])
def upload_only():
    """
    multipart/form-data:
      file=<upload>  (vereist)
      filename=<optioneel, bestandsnaam op de robot>
      remote_dir=<optioneel, standaard /home/nao/ugh_audio>
    """
    try:
        if 'file' not in request.files:
            return make_response(status="error", error="No file part")
        f = request.files['file']
        if not f or not f.filename:
            return make_response(status="error", error="Empty file")
        filename = request.form.get('filename') or f.filename
        remote_dir = request.form.get('remote_dir') or app.config["NAO_REMOTE_AUDIO_DIR"]

        utils = _utils()
        remote_path = utils.upload_via_temp(f, f.filename, remote_filename=filename, remote_dir=remote_dir)
        return make_response(data={"remote_path": remote_path})
    except Exception as e:
        app.logger.error(traceback.format_exc())
        return make_response(status="error", error=repr(e))


# === DEPRECATED (file-based audio) ===
# Gebruik deze endpoint alleen nog voor legacy-audio die al via deze route geüpload wordt.
# Nieuwe audio-stromen lopen via de Py3-laag (Piper + transport).
@app.route("/play_audio", methods=["POST"])
def play_audio():
    """
    multipart/form-data:
      file=<upload>  (vereist)
      filename=<optioneel, bestandsnaam op de robot>
      remote_dir=<optioneel, standaard /home/nao/ugh_audio>
    """
    try:
        if 'file' not in request.files:
            return make_response(status="error", error="No file part")
        f = request.files['file']
        if not f or not f.filename:
            return make_response(status="error", error="Empty file")

        filename = request.form.get('filename') or f.filename
        remote_dir = request.form.get('remote_dir') or app.config["NAO_REMOTE_AUDIO_DIR"]

        utils = _utils()
        # upload + afspelen via NAO
        remote_path = utils.upload_and_play(f, f.filename, remote_filename=filename, remote_dir=remote_dir)
        return make_response(data={"remote_path": remote_path})
    except Exception as e:
        app.logger.error(traceback.format_exc())
        return make_response(status="error", error=repr(e))


# Streaming-endpoint dat raw PCM (S16_LE, mono) direct naar NAO stuurt.
# Voor nu experimenteel; wordt in Py3 opnieuw ontworpen rondom Piper-live-TTS.
@app.route("/play_stream", methods=["POST"])
def play_stream():
    """
    Body: raw PCM bytes (S16_LE, mono) in de HTTP-body.
    Content-Type: application/octet-stream
    """
    try:
        audio_bytes = request.data
        utils = _utils()
        utils.stream_and_play(audio_bytes)
        return jsonify({"status": "playing (streamed)"})
    except Exception as e:
        app.logger.error(traceback.format_exc())
        return make_response(status="error", error=repr(e))


# ====== Main ======
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="NAO Flask API")
    parser.add_argument("--host", default=DEFAULT_WEB_HOST, help="Web host (default 0.0.0.0)")
    parser.add_argument("--port", type=int, default=DEFAULT_WEB_PORT, help="Web port (default 5000)")
    parser.add_argument("--nao_ip", default=DEFAULT_NAO_IP, help="NAO IP")
    parser.add_argument("--nao_port", type=int, default=DEFAULT_NAO_PORT, help="NAO port (default 9559)")
    parser.add_argument("--nao_ssh_user", default=os.environ.get("NAO_SSH_USER", DEFAULT_SSH_USER))
    parser.add_argument("--nao_ssh_pass", default=os.environ.get("NAO_SSH_PASS", DEFAULT_SSH_PASS))
    parser.add_argument("--nao_ssh_port", type=int, default=int(os.environ.get("NAO_SSH_PORT", DEFAULT_SSH_PORT)))
    parser.add_argument("--nao_remote_audio_dir", default=os.environ.get("NAO_REMOTE_AUDIO_DIR", DEFAULT_REMOTE_AUDIO_DIR))
    args = parser.parse_args()

    app.config["NAO_IP"] = args.nao_ip
    app.config["NAO_PORT"] = args.nao_port
    app.config["NAO_SSH_USER"] = args.nao_ssh_user
    app.config["NAO_SSH_PASS"] = args.nao_ssh_pass
    app.config["NAO_SSH_PORT"] = args.nao_ssh_port
    app.config["NAO_REMOTE_AUDIO_DIR"] = args.nao_remote_audio_dir

    local_ip = _get_local_ip()
    sys.stdout.write("Flask app beschikbaar op: http://%s:%s\n" % (local_ip, args.port))
    app.run(host=args.host, port=args.port)
