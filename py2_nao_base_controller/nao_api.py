# -*- coding: utf-8 -*-
import argparse
import os
import socket
import sys
import traceback
import json
import unicodedata
import threading
import signal
import time
import struct
from flask import Flask, request, jsonify

# Py2 unicode-alias
unicode_type = unicode

# Simple replace mapper for pronunciation tweaks.
# Add items like (u"AI", u"A Ie") to improve NAO TTS.
DEFAULT_TTS_REPLACE_MAP = [
    (u"AI", u"A Ie"),
    (u"bias", u"beias"),
]
TTS_REPLACE_MAP = list(DEFAULT_TTS_REPLACE_MAP)

# People-detection runtime state for subscribe/unsubscribe based modules.
_people_lock = threading.Lock()
_people_subscribed = {}

_PEOPLE_API_SPECS = [
    {
        "id": "people_perception",
        "label": "People perception",
        "module": "ALPeoplePerception",
        "sample_methods": [
            ("getMaximumDetectionRange", []),
            ("getTimeBeforePersonDisappears", []),
        ],
        "memory_keys": [
            "PeoplePerception/PeopleList",
            "PeoplePerception/VisiblePeopleList",
            "PeoplePerception/Population",
        ],
    },
    {
        "id": "gaze_analysis",
        "label": "Gaze analysis",
        "module": "ALGazeAnalysis",
        "sample_methods": [
            ("getTolerance", []),
        ],
        "memory_keys": [
            "GazeAnalysis/PeopleLookingAtRobot",
            "GazeAnalysis/PersonStartsLookingAtRobot",
            "GazeAnalysis/PersonStopsLookingAtRobot",
        ],
    },
    {
        "id": "face_detection",
        "label": "Face detection",
        "module": "ALFaceDetection",
        "sample_methods": [],
        "memory_keys": [
            "FaceDetected",
        ],
    },
]

def _setup_naoqi_paths():
    # Als PyInstaller draait: _MEIPASS, anders gewone dir
    base_dir = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    sdk_root = os.path.join(base_dir, "naoqi-sdk")
    lib_dir = os.path.join(sdk_root, "lib")
    bin_dir = os.path.join(sdk_root, "bin")

    if os.path.isdir(lib_dir) and lib_dir not in sys.path:
        sys.path.insert(0, lib_dir)

    # DLL-zoekpad uitbreiden
    os.environ["PATH"] = bin_dir + os.pathsep + os.environ.get("PATH", "")

_setup_naoqi_paths()

from naoqi import ALProxy
from nao_utils import NaoUtils, set_eye_color, group_behaviors, DEFAULT_REMOTE_AUDIO_DIR
import ConfigParser

# ====== Defaults ======
DEFAULT_WEB_HOST = "0.0.0.0"
DEFAULT_WEB_PORT = 5000
DEFAULT_NAO_IP   = "192.168.0.102"
DEFAULT_NAO_PORT = 9559
DEFAULT_SSH_USER = "nao"
DEFAULT_SSH_PASS = "nao"
DEFAULT_SSH_PORT = 22

def load_config():
    """
    Leest config.ini één map hoger dan nao_api.py.
    Alleen defaults voor host/port/NAO_IP/NAO_PORT worden hieruit gehaald.
    CLI-args en env-vars blijven alles overrulen.
    """
    base_dir = os.path.dirname(os.path.abspath(__file__))
    root_dir = os.path.dirname(base_dir)
    ini_path = os.path.join(root_dir, "config.ini")

    cfg = {
        "WEB_HOST": DEFAULT_WEB_HOST,
        "WEB_PORT": DEFAULT_WEB_PORT,
        "NAO_IP":   DEFAULT_NAO_IP,
        "NAO_PORT": DEFAULT_NAO_PORT,
        "AUTO_REST_AFTER_S": 0,
        "TTS_REPLACE_MAP_FILE": None,
    }

    if os.path.exists(ini_path):
        parser = ConfigParser.ConfigParser()
        parser.read(ini_path)

        if parser.has_section("nao_controller"):
            if parser.has_option("nao_controller", "NAO_IP"):
                cfg["NAO_IP"] = parser.get("nao_controller", "NAO_IP")
            if parser.has_option("nao_controller", "NAO_PORT"):
                cfg["NAO_PORT"] = parser.getint("nao_controller", "NAO_PORT")

        if parser.has_section("py2_server"):
            if parser.has_option("py2_server", "WEB_HOST"):
                cfg["WEB_HOST"] = parser.get("py2_server", "WEB_HOST")
            if parser.has_option("py2_server", "WEB_PORT"):
                cfg["WEB_PORT"] = parser.getint("py2_server", "WEB_PORT")
            if parser.has_option("py2_server", "AUTO_REST_AFTER_S"):
                cfg["AUTO_REST_AFTER_S"] = parser.getint("py2_server", "AUTO_REST_AFTER_S")
            if parser.has_option("py2_server", "TTS_REPLACE_MAP_FILE"):
                cfg["TTS_REPLACE_MAP_FILE"] = parser.get("py2_server", "TTS_REPLACE_MAP_FILE")

    env_auto_rest = os.environ.get("NAO_AUTO_REST_AFTER_S")
    if env_auto_rest:
        try:
            cfg["AUTO_REST_AFTER_S"] = int(env_auto_rest)
        except Exception:
            pass

    return cfg


def _to_unicode(value):
    if isinstance(value, unicode_type):
        return value
    try:
        return value.decode("utf-8")
    except Exception:
        try:
            return unicode_type(value)
        except Exception:
            return u""


def load_tts_replace_map(path):
    if not path:
        return list(DEFAULT_TTS_REPLACE_MAP)
    if not os.path.exists(path):
        sys.stdout.write("[TTS] word map not found: %s\n" % path)
        return list(DEFAULT_TTS_REPLACE_MAP)
    try:
        with open(path, "rb") as f:
            data = json.load(f)
    except Exception as e:
        sys.stdout.write("[TTS] word map load failed (%s): %s\n" % (path, repr(e)))
        return list(DEFAULT_TTS_REPLACE_MAP)

    mappings = []
    if isinstance(data, dict):
        # Dict order is not guaranteed in Py2; prefer list in JSON.
        items = data.items()
        for src, dst in items:
            mappings.append((_to_unicode(src), _to_unicode(dst)))
    elif isinstance(data, list):
        for entry in data:
            if isinstance(entry, (list, tuple)) and len(entry) == 2:
                mappings.append((_to_unicode(entry[0]), _to_unicode(entry[1])))
            elif isinstance(entry, dict):
                if "src" in entry and "dst" in entry:
                    mappings.append((_to_unicode(entry["src"]), _to_unicode(entry["dst"])))
                elif "from" in entry and "to" in entry:
                    mappings.append((_to_unicode(entry["from"]), _to_unicode(entry["to"])))
    if not mappings:
        return list(DEFAULT_TTS_REPLACE_MAP)
    return mappings


# ====== Flask app ======
app = Flask(__name__)
app.config.setdefault("AUTO_REST_AFTER_S", 0)

_last_activity = {"ts": time.time()}
_activity_lock = threading.Lock()


def _touch_activity():
    with _activity_lock:
        _last_activity["ts"] = time.time()


def _auto_rest_loop():
    while True:
        time.sleep(2.0)
        timeout_s = app.config.get("AUTO_REST_AFTER_S", 0) or 0
        try:
            timeout_s = float(timeout_s)
        except Exception:
            timeout_s = 0
        if timeout_s <= 0:
            continue
        with _activity_lock:
            last_ts = _last_activity["ts"]
        if time.time() - last_ts < timeout_s:
            continue
        try:
            motion = get_proxy("ALMotion")
            if is_awake():
                motion.rest()
        except Exception:
            pass
        _touch_activity()


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


@app.route("/camera_snapshot", methods=["GET"])
def camera_snapshot():
    """
    Return 1 camera frame as BMP.
    Query params:
      camera=0|1 (default 0)
      resolution=0|1|2 (default 1)
      fps=1..30 (default 8)
    """
    try:
        camera = int(request.args.get("camera", 0))
    except Exception:
        camera = 0
    try:
        resolution = int(request.args.get("resolution", 1))
    except Exception:
        resolution = 1
    try:
        fps = int(request.args.get("fps", 8))
    except Exception:
        fps = 8

    if camera not in (0, 1):
        camera = 0
    if resolution not in (0, 1, 2):
        resolution = 1
    fps = max(1, min(30, fps))

    try:
        width, height, rgb = _camera_capture_rgb(camera, resolution, 11, fps)
        payload = _rgb24_to_bmp(rgb, width, height)
        resp = app.response_class(payload, status=200, mimetype="image/bmp")
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        return resp
    except Exception as e:
        app.logger.error(traceback.format_exc())
        err = (
            "Camera snapshot mislukt voor camera=%s, resolution=%s, fps=%s. "
            "Gebruik camera 0 (Top) of 1 (Bottom). "
            "Resolution: 0=QQVGA, 1=QVGA, 2=VGA. Fout: %s"
        ) % (camera, resolution, fps, repr(e))
        return make_response(
            status="error",
            error=err,
            data={
                "camera": camera,
                "resolution": resolution,
                "fps": fps,
                "allowed_camera_ids": [0, 1],
                "allowed_resolutions": [0, 1, 2],
            },
        ), 500


@app.route("/is_awake", methods=["GET"])
def is_awake_ep():
    try:
        return make_response(data={"is_awake": bool(is_awake())})
    except Exception as e:
        return make_response(status="error", error=repr(e))


@app.route("/wake_up", methods=["POST"])
def wake_up():
    try:
        _touch_activity()
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


@app.route("/autonomous_life", methods=["GET"])
def autonomous_life_get():
    try:
        life = get_proxy("ALAutonomousLife")
        state = life.getState()
        enabled = str(state).lower() != "disabled"
        return make_response(data={"state": state, "enabled": enabled})
    except Exception as e:
        return make_response(status="error", error=repr(e))


@app.route("/autonomous_life", methods=["POST"])
def autonomous_life_set():
    try:
        _touch_activity()
        payload = request.get_json(force=True) or {}
        state = payload.get("state", None)
        enabled = payload.get("enabled", None)
        if state is None:
            if enabled is None:
                return make_response(status="error", error="Missing 'state' or 'enabled'")
            state = "solitary" if bool(enabled) else "disabled"
        life = get_proxy("ALAutonomousLife")
        life.setState(state)
        state_now = life.getState()
        enabled_now = str(state_now).lower() != "disabled"
        return make_response(data={"state": state_now, "enabled": enabled_now})
    except Exception as e:
        return make_response(status="error", error=repr(e))


def _as_bool(value):
    try:
        if isinstance(value, bool):
            return value
        v = str(value).strip().lower()
        return v in ("1", "true", "yes", "on")
    except Exception:
        return False


@app.route("/autonomous_motion_pause", methods=["POST"])
def autonomous_motion_pause():
    try:
        _touch_activity()
        state = {}

        try:
            awareness = get_proxy("ALBasicAwareness")
            enabled = bool(awareness.isEnabled())
            state["basic_awareness"] = enabled
            if enabled:
                awareness.setEnabled(False)
        except Exception as e:
            state["basic_awareness_error"] = repr(e)

        try:
            bg = get_proxy("ALBackgroundMovement")
            enabled = bool(bg.isEnabled())
            state["background_movement"] = enabled
            if enabled:
                bg.setEnabled(False)
        except Exception as e:
            state["background_movement_error"] = repr(e)

        try:
            sm = get_proxy("ALSpeakingMovement")
            enabled = bool(sm.isEnabled())
            state["speaking_movement"] = enabled
            if enabled:
                sm.setEnabled(False)
        except Exception as e:
            state["speaking_movement_error"] = repr(e)

        try:
            motion = get_proxy("ALMotion")
            enabled = bool(motion.getBreathEnabled("Body"))
            state["breath_body"] = enabled
            if enabled:
                motion.setBreathEnabled("Body", False)
        except Exception as e:
            state["breath_body_error"] = repr(e)

        return make_response(data=state)
    except Exception as e:
        return make_response(status="error", error=repr(e))


@app.route("/autonomous_motion_restore", methods=["POST"])
def autonomous_motion_restore():
    try:
        _touch_activity()
        payload = request.get_json(force=True) or {}
        state = payload.get("state") or {}
        if not isinstance(state, dict):
            state = {}
        errors = {}

        try:
            if "basic_awareness" in state:
                awareness = get_proxy("ALBasicAwareness")
                awareness.setEnabled(_as_bool(state.get("basic_awareness")))
        except Exception as e:
            errors["basic_awareness"] = repr(e)

        try:
            if "background_movement" in state:
                bg = get_proxy("ALBackgroundMovement")
                bg.setEnabled(_as_bool(state.get("background_movement")))
        except Exception as e:
            errors["background_movement"] = repr(e)

        try:
            if "speaking_movement" in state:
                sm = get_proxy("ALSpeakingMovement")
                sm.setEnabled(_as_bool(state.get("speaking_movement")))
        except Exception as e:
            errors["speaking_movement"] = repr(e)

        try:
            if "breath_body" in state:
                motion = get_proxy("ALMotion")
                motion.setBreathEnabled("Body", _as_bool(state.get("breath_body")))
        except Exception as e:
            errors["breath_body"] = repr(e)

        resp = {"restored": True}
        if errors:
            resp["errors"] = errors
        return make_response(data=resp)
    except Exception as e:
        return make_response(status="error", error=repr(e))


def _custom_life_state():
    state = {}
    abilities = {}
    try:
        life = get_proxy("ALAutonomousLife")
        if hasattr(life, "getAutonomousAbilityEnabled"):
            try:
                abilities["basic_awareness"] = bool(life.getAutonomousAbilityEnabled("BasicAwareness"))
            except Exception as e:
                abilities["basic_awareness_error"] = repr(e)
            try:
                abilities["background_movement"] = bool(life.getAutonomousAbilityEnabled("BackgroundMovement"))
            except Exception as e:
                abilities["background_movement_error"] = repr(e)
            try:
                abilities["speaking_movement"] = bool(life.getAutonomousAbilityEnabled("SpeakingMovement"))
            except Exception as e:
                abilities["speaking_movement_error"] = repr(e)
    except Exception as e:
        abilities["life_error"] = repr(e)
    try:
        awareness = get_proxy("ALBasicAwareness")
        if hasattr(awareness, "isAwarenessRunning"):
            state["basic_awareness"] = bool(awareness.isAwarenessRunning())
        elif hasattr(awareness, "isEnabled"):
            state["basic_awareness"] = bool(awareness.isEnabled())
        else:
            raise AttributeError("ALBasicAwareness has no isAwarenessRunning/isEnabled")
    except Exception as e:
        state["basic_awareness_error"] = repr(e)
    try:
        moves = get_proxy("ALAutonomousMoves")
        if hasattr(moves, "getExpressiveListeningEnabled"):
            state["background_movement"] = bool(moves.getExpressiveListeningEnabled())
        elif hasattr(moves, "isExpressiveListeningEnabled"):
            state["background_movement"] = bool(moves.isExpressiveListeningEnabled())
        else:
            raise AttributeError("ALAutonomousMoves has no get/isExpressiveListeningEnabled")
    except Exception as e:
        state["background_movement_error"] = repr(e)
    try:
        motion = get_proxy("ALMotion")
        state["breathing"] = bool(motion.getBreathEnabled("Body"))
    except Exception as e:
        state["breathing_error"] = repr(e)
    if abilities:
        state["abilities"] = abilities
    return state


def _apply_custom_life_state(state):
    if not isinstance(state, dict):
        return
    abilities = state.get("abilities") if isinstance(state.get("abilities"), dict) else None
    def _get_setting(key):
        if abilities is not None and key in abilities:
            return abilities.get(key)
        if key in state:
            return state.get(key)
        return None

    try:
        life = get_proxy("ALAutonomousLife")
        if hasattr(life, "setAutonomousAbilityEnabled"):
            basic = _get_setting("basic_awareness")
            if basic is not None:
                life.setAutonomousAbilityEnabled("BasicAwareness", _as_bool(basic))
            background = _get_setting("background_movement")
            if background is not None:
                life.setAutonomousAbilityEnabled("BackgroundMovement", _as_bool(background))
    except Exception:
        pass
    try:
        basic = _get_setting("basic_awareness")
        if basic is not None:
            awareness = get_proxy("ALBasicAwareness")
            enabled = _as_bool(basic)
            if enabled and hasattr(awareness, "startAwareness"):
                awareness.startAwareness()
            elif (not enabled) and hasattr(awareness, "stopAwareness"):
                awareness.stopAwareness()
            elif hasattr(awareness, "setEnabled"):
                awareness.setEnabled(enabled)
    except Exception:
        pass
    try:
        background = _get_setting("background_movement")
        if background is not None:
            moves = get_proxy("ALAutonomousMoves")
            if hasattr(moves, "setExpressiveListeningEnabled"):
                moves.setExpressiveListeningEnabled(_as_bool(background))
    except Exception:
        pass
    try:
        breathing = _get_setting("breathing")
        if breathing is not None:
            motion = get_proxy("ALMotion")
            motion.setBreathEnabled("Body", _as_bool(breathing))
    except Exception:
        pass


def _people_method_list(proxy):
    try:
        methods = proxy.getMethodList()
        if isinstance(methods, list):
            return set([str(m) for m in methods])
    except Exception:
        pass
    return set()


def _people_call(proxy, methods, method_name, args=None):
    if args is None:
        args = []
    if method_name not in methods:
        raise AttributeError("Method not available: %s" % method_name)
    fn = getattr(proxy, method_name)
    return fn(*args)


def _people_read_memory(keys):
    out = {}
    try:
        memory = get_proxy("ALMemory")
    except Exception:
        return out
    for key in keys:
        try:
            out[key] = memory.getData(key)
        except Exception:
            out[key] = None
    return out


def _people_toggle_supported(methods):
    if ("setEnabled" in methods) and ("isEnabled" in methods or "getEnabled" in methods):
        return True
    if ("startAwareness" in methods) and ("stopAwareness" in methods):
        return True
    if ("subscribe" in methods) and ("unsubscribe" in methods):
        return True
    return False


def _people_enabled_value(module_name, proxy, methods):
    # Some modules expose isRunning(task_id) which is not an enabled flag.
    # We only use explicit no-arg "enabled/running" style methods.
    for method_name in ("isEnabled", "getEnabled", "isAwarenessRunning"):
        if method_name in methods:
            try:
                return bool(_people_call(proxy, methods, method_name))
            except TypeError:
                pass
            except Exception:
                pass
    if ("subscribe" in methods) and ("unsubscribe" in methods):
        with _people_lock:
            return bool(_people_subscribed.get(module_name))
    return None


def _people_subscribe(proxy, methods, sub_name):
    if "unsubscribe" in methods:
        try:
            _people_call(proxy, methods, "unsubscribe", [sub_name])
        except Exception:
            pass
    errors = []
    for args in ([sub_name], [sub_name, 500], [sub_name, 500, 0.0]):
        try:
            handle = _people_call(proxy, methods, "subscribe", args)
            if handle is None:
                handle = sub_name
            return handle
        except Exception as e:
            errors.append(repr(e))
    raise RuntimeError("subscribe failed: " + " | ".join(errors[-2:]))


def _people_set_enabled(module_name, proxy, methods, enabled):
    enabled = bool(enabled)
    if ("setEnabled" in methods) and ("isEnabled" in methods or "getEnabled" in methods):
        _people_call(proxy, methods, "setEnabled", [enabled])
        return
    if ("startAwareness" in methods) and ("stopAwareness" in methods):
        if enabled:
            _people_call(proxy, methods, "startAwareness")
        else:
            _people_call(proxy, methods, "stopAwareness")
        return
    if ("subscribe" in methods) and ("unsubscribe" in methods):
        sub_name = "nao_api_%s" % module_name.lower()
        if enabled:
            handle = _people_subscribe(proxy, methods, sub_name)
            with _people_lock:
                _people_subscribed[module_name] = handle
        else:
            with _people_lock:
                handle = _people_subscribed.get(module_name)
            candidates = []
            if handle:
                candidates.append(handle)
            if sub_name not in candidates:
                candidates.append(sub_name)
            try:
                for candidate in candidates:
                    try:
                        _people_call(proxy, methods, "unsubscribe", [candidate])
                    except Exception:
                        pass
            except Exception:
                pass
            with _people_lock:
                _people_subscribed.pop(module_name, None)
        return
    raise RuntimeError("No supported toggle methods on %s" % module_name)


def _people_detection_state():
    state = {"modules": []}
    for spec in _PEOPLE_API_SPECS:
        module_name = spec["module"]
        item = {
            "id": spec["id"],
            "label": spec["label"],
            "module": module_name,
            "available": False,
            "toggle_supported": False,
            "enabled": None,
            "samples": {},
            "memory": {},
            "errors": [],
        }
        try:
            proxy = get_proxy(module_name)
            methods = _people_method_list(proxy)
            item["available"] = True
            item["toggle_supported"] = _people_toggle_supported(methods)
            try:
                item["enabled"] = _people_enabled_value(module_name, proxy, methods)
            except Exception as e:
                item["errors"].append("enabled: %s" % repr(e))

            for method_name, args in spec.get("sample_methods", []):
                if method_name not in methods:
                    continue
                try:
                    item["samples"][method_name] = _people_call(proxy, methods, method_name, args)
                except Exception as e:
                    item["samples"][method_name] = "error: %s" % repr(e)

            item["memory"] = _people_read_memory(spec.get("memory_keys", []))
            if ("subscribe" in methods) and ("unsubscribe" in methods):
                item["toggle_mode"] = "subscribe"
            elif ("setEnabled" in methods):
                item["toggle_mode"] = "setEnabled"
            else:
                item["toggle_mode"] = "none"
        except Exception as e:
            item["error"] = repr(e)
        state["modules"].append(item)
    return state


@app.route("/people_detection_state", methods=["GET"])
def people_detection_state():
    try:
        _touch_activity()
        return make_response(data=_people_detection_state())
    except Exception as e:
        return make_response(status="error", error=repr(e))


@app.route("/people_detection_set", methods=["POST"])
def people_detection_set():
    try:
        _touch_activity()
        payload = request.get_json(force=True, silent=True) or {}
        api_id = (payload.get("api") or "").strip()
        enabled = payload.get("enabled", None)
        if not api_id:
            return make_response(status="error", error="Missing 'api'")
        if enabled is None:
            return make_response(status="error", error="Missing 'enabled'")

        spec = None
        for it in _PEOPLE_API_SPECS:
            if it["id"] == api_id:
                spec = it
                break
        if spec is None:
            return make_response(status="error", error="Unknown api: %s" % api_id)

        proxy = get_proxy(spec["module"])
        methods = _people_method_list(proxy)
        _people_set_enabled(spec["module"], proxy, methods, _as_bool(enabled))
        state = _people_detection_state()
        return make_response(data={"api": api_id, "enabled": _as_bool(enabled), "state": state})
    except Exception as e:
        return make_response(status="error", error=repr(e))


@app.route("/custom_life_apply", methods=["POST"])
def custom_life_apply():
    try:
        _touch_activity()
        payload = request.get_json(force=True) or {}
        settings = payload.get("settings") or {}
        if not isinstance(settings, dict):
            settings = {}
        prev_state = {"modules": _custom_life_state()}
        try:
            life = get_proxy("ALAutonomousLife")
            prev_state["life_state"] = life.getState()
        except Exception:
            pass
        _apply_custom_life_state(settings)
        return make_response(data={"prev_state": prev_state, "applied": settings})
    except Exception as e:
        return make_response(status="error", error=repr(e))


def _camera_capture_rgb(camera_id, resolution, color_space, fps):
    """
    Capture 1 frame via ALVideoDevice and return (width, height, rgb_bytes).
    color_space=11 verwacht RGB 24-bit.
    """
    video = get_proxy("ALVideoDevice")
    sub_name = "nao_api_cam_%d" % int(time.time() * 1000)
    handle = None
    try:
        if hasattr(video, "subscribeCamera"):
            handle = video.subscribeCamera(
                sub_name, int(camera_id), int(resolution), int(color_space), int(fps)
            )
        else:
            handle = video.subscribe(sub_name, int(resolution), int(color_space), int(fps))
        image = video.getImageRemote(handle)
    finally:
        try:
            if handle:
                video.unsubscribe(handle)
        except Exception:
            pass

    if not image or len(image) < 7:
        raise RuntimeError("ALVideoDevice returned no image data.")

    width = int(image[0])
    height = int(image[1])
    data = image[6]
    if isinstance(data, list):
        data = "".join([chr(int(v) & 0xFF) for v in data])
    elif not isinstance(data, str):
        data = str(data)

    expected = width * height * 3
    if len(data) < expected:
        raise RuntimeError("Unexpected frame size: %s < %s" % (len(data), expected))
    if len(data) > expected:
        data = data[:expected]
    return width, height, data


def _rgb24_to_bmp(rgb_bytes, width, height):
    """
    Convert RGB24 bytes to a BMP payload (24-bit, uncompressed).
    """
    row_raw = int(width) * 3
    row_pad = (4 - (row_raw % 4)) % 4
    pixel_data = bytearray()

    for y in range(int(height) - 1, -1, -1):
        row = rgb_bytes[y * row_raw : (y + 1) * row_raw]
        for x in range(0, row_raw, 3):
            r = ord(row[x])
            g = ord(row[x + 1])
            b = ord(row[x + 2])
            pixel_data.extend([b, g, r])  # BMP expects BGR order
        if row_pad:
            pixel_data.extend("\x00" * row_pad)

    pixel_size = len(pixel_data)
    file_size = 54 + pixel_size
    file_header = struct.pack("<2sIHHI", "BM", file_size, 0, 0, 54)
    dib_header = struct.pack(
        "<IIIHHIIIIII",
        40,         # DIB header size
        int(width),
        int(height),
        1,          # color planes
        24,         # bits per pixel
        0,          # compression (BI_RGB)
        pixel_size,
        2835,       # X ppm
        2835,       # Y ppm
        0,          # colors in palette
        0,          # important colors
    )
    # Py2 bytearray has no .tostring(); convert to raw bytes explicitly.
    return file_header + dib_header + "".join(chr(v) for v in pixel_data)


@app.route("/custom_life_restore", methods=["POST"])
def custom_life_restore():
    try:
        _touch_activity()
        payload = request.get_json(force=True) or {}
        state = payload.get("state") or {}
        if not isinstance(state, dict):
            state = {}
        life_state = state.get("life_state", None)
        if life_state is not None:
            try:
                life = get_proxy("ALAutonomousLife")
                life.setState(life_state)
            except Exception:
                pass
        modules = state.get("modules") or {}
        _apply_custom_life_state(modules)
        return make_response(data={"restored": True})
    except Exception as e:
        return make_response(status="error", error=repr(e))


@app.route("/custom_life_pause", methods=["POST"])
def custom_life_pause():
    try:
        _touch_activity()
        state = _custom_life_state()
        _apply_custom_life_state(
            {
                "basic_awareness": False,
                "background_movement": False,
                "breathing": False,
            }
        )
        return make_response(data=state)
    except Exception as e:
        return make_response(status="error", error=repr(e))


@app.route("/custom_life_state", methods=["GET"])
def custom_life_state():
    try:
        _touch_activity()
        data = {"modules": _custom_life_state()}
        try:
            life = get_proxy("ALAutonomousLife")
            state = life.getState()
            data["life_state"] = state
            data["life_enabled"] = str(state).lower() != "disabled"
        except Exception as e:
            data["life_error"] = repr(e)
        try:
            data["is_awake"] = bool(is_awake())
        except Exception:
            pass
        return make_response(data=data)
    except Exception as e:
        return make_response(status="error", error=repr(e))


@app.route("/custom_life_resume", methods=["POST"])
def custom_life_resume():
    try:
        _touch_activity()
        payload = request.get_json(force=True) or {}
        state = payload.get("state") or {}
        _apply_custom_life_state(state)
        return make_response(data={"restored": True})
    except Exception as e:
        return make_response(status="error", error=repr(e))


@app.route("/tts", methods=["POST"])
def tts_say():
    try:
        _touch_activity()
        payload = request.get_json(force=True) or {}
        text = payload.get("text", u"")

        # Zorg dat het UNICODE wordt, niet bytes
        if not isinstance(text, unicode_type):
            # als het bytes is -> decode, anders cast
            if isinstance(text, str):
                text = text.decode("utf-8")
            else:
                text = unicode_type(text)

        text_for_response = text

        # Normalize to ASCII-safe bytes for NAOqi.
        text_u = text
        for src, dst in TTS_REPLACE_MAP:
            text_u = text_u.replace(src, dst)
        # Strip simple markdown emphasis markers for TTS.
        text_u = text_u.replace(u"**", u"").replace(u"*", u"")
        text_u = text_u.replace(u"\u2019", u"'").replace(u"\u2018", u"'")
        text_u = text_u.replace(u"\u201c", u"\"").replace(u"\u201d", u"\"")
        text_u = unicodedata.normalize("NFKD", text_u)
        text_tts = text_u.encode("ascii", "ignore")

        tts = get_proxy("ALTextToSpeech")
        tts.say(text_tts)
        return make_response(data={"text": text_for_response})
    except Exception as e:
        return make_response(status="error", error=repr(e))


@app.route("/stop_audio", methods=["POST"])
def stop_audio():
    """
    Best effort stop van lopende spraak/audio:
    - ALTextToSpeech.stopAll (indien beschikbaar)
    - ALAudioPlayer.stopAll (indien beschikbaar)
    - kill van actieve stream-playback (`aplay`) via SSH
    """
    _touch_activity()
    actions = {
        "tts_stop_called": False,
        "audio_player_stop_called": False,
        "stream_stop_issued": False,
        "stream_killed": False,
    }
    errors = {}

    try:
        tts = get_proxy("ALTextToSpeech")
        if hasattr(tts, "stopAll"):
            tts.stopAll()
            actions["tts_stop_called"] = True
        elif hasattr(tts, "stop"):
            tts.stop()
            actions["tts_stop_called"] = True
    except Exception as e:
        errors["tts"] = repr(e)

    try:
        audio = get_proxy("ALAudioPlayer")
        if hasattr(audio, "stopAll"):
            audio.stopAll()
            actions["audio_player_stop_called"] = True
        elif hasattr(audio, "stop"):
            audio.stop()
            actions["audio_player_stop_called"] = True
    except Exception as e:
        errors["audio_player"] = repr(e)

    try:
        utils = _utils()
        stream_res = utils.stop_stream_playback()
        if isinstance(stream_res, dict):
            actions["stream_stop_issued"] = bool(stream_res.get("issued"))
            actions["stream_killed"] = bool(stream_res.get("killed"))
    except Exception as e:
        errors["stream"] = repr(e)

    any_action = any([
        actions.get("tts_stop_called"),
        actions.get("audio_player_stop_called"),
        actions.get("stream_stop_issued"),
    ])
    if errors and not any_action:
        return make_response(status="error", error="stop_audio failed", data={"actions": actions, "errors": errors})
    if errors:
        return make_response(status="warning", data={"actions": actions, "errors": errors})
    return make_response(data={"actions": actions})


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
        _touch_activity()
        payload = request.get_json(force=True) or {}
        bname = payload.get("behavior")

        if not bname:
            return make_response(status="error", error="Missing 'behavior'")
        # Normalize for logging and NAOqi (expects byte-string in Py2).
        if isinstance(bname, unicode_type):
            bname_u = bname
        elif isinstance(bname, str):
            try:
                bname_u = bname.decode("utf-8")
            except Exception:
                bname_u = unicode_type(bname)
        else:
            bname_u = unicode_type(bname)
        bname_qi = bname_u.encode("utf-8")

        sys.stderr.write("[NAO] do_behavior request: %s\n" % bname_u.encode("utf-8"))
        sys.stderr.flush()

        behavior = get_proxy("ALBehaviorManager")

        installed = behavior.isBehaviorInstalled(bname_qi)
        if not installed:
            sys.stderr.write("[NAO] Behavior not installed: %s\n" % bname_u.encode("utf-8"))
            sys.stderr.flush()
            return make_response(
                status="error",
                error="Behavior not installed: " + bname_u,
                data={"behavior": bname_u, "installed": False},
            )

        if not is_awake():
            if bname_u == u"basic/standup":
                try:
                    motion = get_proxy("ALMotion")
                    motion.wakeUp()
                except Exception:
                    pass
            else:
                sys.stderr.write("[NAO] Robot is resting; behavior may not run: %s\n" % bname_u.encode("utf-8"))
                sys.stderr.flush()
                return make_response(
                    status="warning",
                    data={"behavior": bname_u, "is_awake": False},
                )

        sys.stderr.write("[NAO] runBehavior start: %s\n" % bname_u.encode("utf-8"))
        sys.stderr.flush()
        try:
            behavior.runBehavior(bname_qi)
        except Exception:
            sys.stderr.write("[NAO] runBehavior failed: %s\n" % bname_u.encode("utf-8"))
            sys.stderr.write(traceback.format_exc())
            sys.stderr.flush()
            raise
        sys.stderr.write("[NAO] runBehavior done: %s\n" % bname_u.encode("utf-8"))
        sys.stderr.flush()
        return make_response(data={"behavior": bname_u, "ran": True})
    except Exception as e:
        try:
            bname = payload.get("behavior")
        except Exception:
            bname = None
        try:
            bname_u = bname if isinstance(bname, unicode_type) else unicode_type(bname)
            bname_log = bname_u.encode("utf-8")
        except Exception:
            bname_log = repr(bname)
        sys.stderr.write("[NAO] do_behavior exception for %s\n" % bname_log)
        sys.stderr.write(traceback.format_exc())
        sys.stderr.flush()
        return make_response(status="error", error=repr(e), data={"behavior": bname})


@app.route("/stop_behavior", methods=["POST"])
def stop_behavior():
    try:
        payload = request.get_json(force=True) or {}
        bname = payload.get("behavior")
        if not bname:
            return make_response(status="error", error="Missing 'behavior'")

        if not isinstance(bname, unicode_type):
            if isinstance(bname, str):
                bname = bname.decode("utf-8")
            else:
                bname = unicode_type(bname)

        behavior = get_proxy("ALBehaviorManager")
        if not behavior.isBehaviorInstalled(bname):
            return make_response(status="error", error="Behavior not installed: " + bname)

        if hasattr(behavior, "isBehaviorRunning") and not behavior.isBehaviorRunning(bname):
            return make_response(status="warning", data="Behavior not running: " + bname)

        behavior.stopBehavior(bname)
        return make_response(data="Stopped behavior: " + bname)
    except Exception as e:
        return make_response(status="error", error=repr(e))


@app.route("/stop_all_behaviors", methods=["POST"])
def stop_all_behaviors():
    try:
        behavior = get_proxy("ALBehaviorManager")
        behavior.stopAllBehaviors()
        return make_response(data="Stopped all behaviors")
    except Exception as e:
        return make_response(status="error", error=repr(e))


@app.route("/stop_move", methods=["POST"])
def stop_move():
    try:
        motion = get_proxy("ALMotion")
        motion.stopMove()
        return make_response(data="Stopped motion")
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

@app.route("/tts_speed", methods=["GET"])
def get_tts_speed():
    """
    Haal TTS-snelheid op.
    """
    try:
        tts = get_proxy("ALTextToSpeech")
        speed = tts.getParameter("speed")
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

@app.route("/volume", methods=["GET"])
def get_volume():
    """
    Haal outputvolume op.
    """
    try:
        audio_dev = get_proxy("ALAudioDevice")
        volume = audio_dev.getOutputVolume()
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


@app.route("/posture", methods=["GET"])
def posture():
    """
    Geef huidige houding terug via ALRobotPosture.
    """
    try:
        posture_proxy = get_proxy("ALRobotPosture")
        posture = posture_proxy.getPosture()
        # Posture names are typically: Stand, StandInit, StandZero, Sit, SitRelax, Crouch, etc.
        posture_s = posture.decode("utf-8") if isinstance(posture, str) else unicode_type(posture)
        posture_l = posture_s.lower()
        is_sitting = posture_l.startswith("sit")
        is_standing = posture_l.startswith("stand")
        return jsonify({
            "status": "ok",
            "data": {
                "posture": posture_s,
                "is_sitting": bool(is_sitting),
                "is_standing": bool(is_standing),
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

    # 1) module/method als bytes/str
    if isinstance(module_name, unicode_type):
        module_name = module_name.encode("utf-8")
    if isinstance(method_name, unicode_type):
        method_name = method_name.encode("utf-8")

    proxy = get_proxy(module_name)
    method = getattr(proxy, method_name)

    # 2) args/kwargs recursief converteren naar NAOqi-veilige types.
    # NAOqi 2.1 verwacht bytes (std::string) in nested ALValue structs.
    def ensure_naoqi_arg(x):
        if isinstance(x, unicode_type):
            return x.encode("utf-8")  # unicode -> bytes
        if isinstance(x, list):
            return [ensure_naoqi_arg(v) for v in x]
        if isinstance(x, tuple):
            return tuple(ensure_naoqi_arg(v) for v in x)
        if isinstance(x, dict):
            out = {}
            for k, v in x.items():
                kk = k.encode("utf-8") if isinstance(k, unicode_type) else k
                out[kk] = ensure_naoqi_arg(v)
            return out
        return x  # str/bytes/nummers etc. laten staan

    args = [ensure_naoqi_arg(a) for a in args]
    kwargs = {ensure_naoqi_arg(k): ensure_naoqi_arg(v) for (k, v) in kwargs.items()}

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
        sr = request.args.get("sample_rate") or request.headers.get("X-Sample-Rate")
        try:
            sample_rate = int(sr) if sr else 22050
        except Exception:
            sample_rate = 22050
        utils = _utils()
        utils.stream_and_play(audio_bytes, sample_rate=sample_rate)
        return jsonify({"status": "playing (streamed)", "sample_rate": int(sample_rate)})
    except Exception as e:
        app.logger.error(traceback.format_exc())
        return make_response(status="error", error=repr(e))


# ====== Main ======
if __name__ == "__main__":
    ini_cfg = load_config()

    parser = argparse.ArgumentParser(description="NAO Flask API")
    parser.add_argument("--host", default=ini_cfg["WEB_HOST"], help="Web host (default 0.0.0.0)")
    parser.add_argument("--port", type=int, default=ini_cfg["WEB_PORT"], help="Web port (default 5000)")
    parser.add_argument("--nao_ip", default=ini_cfg["NAO_IP"], help="NAO IP")
    parser.add_argument("--nao_port", type=int, default=ini_cfg["NAO_PORT"], help="NAO port (default 9559)")
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
    app.config["AUTO_REST_AFTER_S"] = ini_cfg.get("AUTO_REST_AFTER_S", 0)

    map_path = ini_cfg.get("TTS_REPLACE_MAP_FILE") or os.environ.get("TTS_REPLACE_MAP_FILE")
    if not map_path:
        base_dir = os.path.dirname(os.path.abspath(__file__))
        root_dir = os.path.dirname(base_dir)
        map_path = os.path.join(root_dir, "tts_word_map.json")
    try:
        global TTS_REPLACE_MAP
        TTS_REPLACE_MAP = load_tts_replace_map(map_path)
    except Exception as e:
        sys.stdout.write("[TTS] word map init failed: %s\n" % repr(e))
        TTS_REPLACE_MAP = list(DEFAULT_TTS_REPLACE_MAP)

    local_ip = _get_local_ip()
    sys.stdout.write("Flask app beschikbaar op: http://%s:%s\n" % (local_ip, args.port))
    if app.config.get("AUTO_REST_AFTER_S", 0):
        t = threading.Thread(target=_auto_rest_loop)
        t.daemon = True
        t.start()
    def _sigint_handler(signum, frame):
        try:
            motion = get_proxy("ALMotion")
            if is_awake():
                motion.rest()
        except Exception:
            pass
        raise SystemExit(0)

    try:
        signal.signal(signal.SIGINT, _sigint_handler)
        signal.signal(signal.SIGTERM, _sigint_handler)
    except Exception:
        pass
    app.run(host=args.host, port=args.port)
