# -*- coding: utf-8 -*-
import argparse
import os
from flask import Flask, request, jsonify
from naoqi import ALProxy

# ====== Default configuratie ======
# Let op!! Dit wordt overschreven door params in main
# bv als je start via de bash scripts. 
DEFAULT_WEB_HOST = "0.0.0.0"
DEFAULT_WEB_PORT = 5000
DEFAULT_NAO_IP   = "192.168.0.101"
DEFAULT_NAO_PORT = 9559

# ====== Flask app ======
app = Flask(__name__)

# ====== Helpers ======
def make_response(status="ok", data=None, error=None):
    resp = {"status": status}
    if data is not None:
        resp["data"] = data
    if error is not None:
        resp["error"] = error
    return jsonify(resp)

def get_proxy(name):
    """Maak steeds een nieuwe proxy naar NAO"""
    return ALProxy(name, app.config["NAO_IP"], app.config["NAO_PORT"])

def is_awake():
    try:
        return get_proxy("ALMotion").robotIsWakeUp()
    except Exception:
        return False

def group_behaviors(behaviors):
    grouped = {}
    for b in behaviors:
        folder = os.path.dirname(b)
        name = os.path.basename(b)
        if folder not in grouped:
            grouped[folder] = []
        grouped[folder].append(name)
    for k in grouped:
        grouped[k].sort()
    return grouped

# ====== API endpoints ======
@app.route("/ping", methods=["GET"])
def ping():
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
        text = request.json.get("text", u"")
        if isinstance(text, unicode):
            text = text.encode("utf-8")
        tts = get_proxy("ALTextToSpeech")
        tts.say(text)
        return make_response(data="Said: " + text)
    except Exception as e:
        return make_response(status="error", error=repr(e))

@app.route("/list_behaviors", methods=["GET"])
def list_behaviors():
    try:
        behavior = get_proxy("ALBehaviorManager")
        behaviors = behavior.getInstalledBehaviors()
        grouped = group_behaviors(behaviors)
        return make_response(data=grouped)
    except Exception as e:
        return make_response(status="error", error=repr(e))

@app.route("/do_behavior", methods=["POST"])
def do_behavior():
    try:
        bname = request.json.get("behavior", "")
            
        if not bname:
            return make_response(status="error", error="No behavior provided")
            
        if isinstance(bname, unicode):
            bname = bname.encode("utf-8")

        behavior = get_proxy("ALBehaviorManager")
        if not behavior.isBehaviorInstalled(bname):
            return make_response(status="error", error="Behavior not installed: " + bname)

        if not is_awake():
            return make_response(status="warning",
                                 data="Robot is resting, some behaviors may not run correctly")

        behavior.runBehavior(bname)
        return make_response(data="Ran behavior: " + bname)
    except Exception as e:
        return make_response(status="error", error=repr(e))

# ====== Main ======
if __name__ == "__main__":
    import socket
    import sys

    def get_local_ip():
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            # hoeft niet echt te connecten; bepaalt alleen de lokale IP
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
        except Exception:
            ip = "127.0.0.1"
        finally:
            s.close()
        return ip

    parser = argparse.ArgumentParser(description="NAO Flask API")
    parser.add_argument("--host", default=DEFAULT_WEB_HOST, help="Web host (default 0.0.0.0)")
    parser.add_argument("--port", type=int, default=DEFAULT_WEB_PORT, help="Web port (default 5000)")
    parser.add_argument("--nao_ip", default=DEFAULT_NAO_IP, help="NAO IP (default 192.168.68.66)")
    parser.add_argument("--nao_port", type=int, default=DEFAULT_NAO_PORT, help="NAO port (default 9559)")
    args = parser.parse_args()

    # config opslaan in Flask app
    app.config["NAO_IP"] = args.nao_ip
    app.config["NAO_PORT"] = args.nao_port

    local_ip = get_local_ip()
    # Python 2-compatibele output (geen f-strings)
    sys.stdout.write("Flask app beschikbaar op: http://%s:%s\n" % (local_ip, args.port))

    app.run(host=args.host, port=args.port)
