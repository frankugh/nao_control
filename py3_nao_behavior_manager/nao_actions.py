# nao_actions.py
"""
Py3-client voor de legacy Py2 NAO Flask-API.

Deze klasse kapselt alle relevante NAO-acties in, zodat de Py3-weblaag
een stabiel contract heeft richting scripts (QT/NAO-only runners),
terwijl de onderliggende Py2-implementatie later vervangen kan worden.
"""

import requests


class NaoActions(object):
    def __init__(self, base_url, timeout=5):
        """
        :param base_url: Basis-URL van de Py2-NAO-API, bijv. "http://127.0.0.1:5000"
        :param timeout:  default timeout (seconden) voor HTTP-requests
        """
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    # ===== interne helpers =====

    def _get(self, path, **kwargs):
        """Doe een GET naar Py2 en retourneer de raw Response."""
        timeout = kwargs.get("timeout", self.timeout)
        return requests.get(self.base_url + path, timeout=timeout)

    def _post_json(self, path, payload=None, **kwargs):
        """Doe een POST met JSON-payload naar Py2 en retourneer de raw Response."""
        timeout = kwargs.get("timeout", self.timeout)
        return requests.post(self.base_url + path, json=payload or {}, timeout=timeout)

    def _post_multipart(self, path, files, data=None, **kwargs):
        """Doe een multipart/form-data POST (voor file-upload/audio)."""
        timeout = kwargs.get("timeout", self.timeout)
        return requests.post(self.base_url + path, files=files, data=data or {}, timeout=timeout)

    def _post_stream(self, path, data, headers=None, **kwargs):
        """Stuur raw bytes (audio-stream) door naar Py2."""
        timeout = kwargs.get("timeout", self.timeout)
        return requests.post(self.base_url + path, data=data, headers=headers or {}, timeout=timeout)

    # ===== eenvoudige NAO-acties (JSON) =====

    def ping(self, timeout=None):
        """Healthcheck van de Py2-NAO-API."""
        resp = self._get("/ping", timeout=timeout or self.timeout)
        resp.raise_for_status()
        return resp.json()

    def wake_up(self, timeout=None):
        """Roep /wake_up aan op de Py2-API."""
        resp = self._post_json("/wake_up", {}, timeout=timeout or self.timeout)
        resp.raise_for_status()
        return resp.json()

    def rest(self, timeout=None):
        """Roep /rest aan op de Py2-API."""
        resp = self._post_json("/rest", {}, timeout=timeout or self.timeout)
        resp.raise_for_status()
        return resp.json()

    def say_native(self, text, timeout=None):
        """
        Roep de Py2 /tts-endpoint aan met JSON {"text": ...}.
        Verwacht dezelfde JSON-structuur terug als nao_api.py.
        """
        payload = {"text": text}
        resp = self._post_json("/tts", payload, timeout=timeout or self.timeout)
        resp.raise_for_status()
        return resp.json()

    def list_behaviors(self, timeout=None):
        """Vraag alle geïnstalleerde behaviors op (/list_behaviors)."""
        resp = self._get("/list_behaviors", timeout=timeout or self.timeout)
        resp.raise_for_status()
        return resp.json()

    def do_behavior(self, behavior_name, timeout=None):
        """Start een behavior via /do_behavior."""
        payload = {"behavior": behavior_name}
        resp = self._post_json("/do_behavior", payload, timeout=timeout or self.timeout)
        resp.raise_for_status()
        return resp.json()

    def set_tts_speed(self, speed, timeout=None):
        """
        Zet TTS-snelheid via /tts_speed.
        :param speed: typisch bereik 50–100
        """
        payload = {"speed": speed}
        resp = self._post_json("/tts_speed", payload, timeout=timeout or self.timeout)
        resp.raise_for_status()
        return resp.json()

    def set_volume(self, volume, timeout=None):
        """
        Zet outputvolume via /set_volume.
        :param volume: 0–100
        """
        payload = {"volume": volume}
        resp = self._post_json("/set_volume", payload, timeout=timeout or self.timeout)
        resp.raise_for_status()
        return resp.json()

    def set_eye_color(self, color, duration=0.5, timeout=None):
        """
        Zet oogkleur via /set_eye_color.
        :param color: string "#RRGGBB"
        :param duration: fade-duur in seconden
        """
        payload = {"color": color, "duration": duration}
        resp = self._post_json("/set_eye_color", payload, timeout=timeout or self.timeout)
        resp.raise_for_status()
        return resp.json()

    def naoqi_call(self, module, method, args=None, kwargs=None, timeout=None):
        """
        Flexibele NAOqi-call via /naoqi/call.
        :param module: NAOqi-modulenaam, bijv. "ALTextToSpeech"
        :param method: NAOqi-methodenaam, bijv. "say"
        :param args:   lijst met positionele args
        :param kwargs: dict met keyword args
        """
        payload = {
            "module": module,
            "method": method,
            "args": args or [],
            "kwargs": kwargs or {},
        }
        resp = self._post_json("/naoqi/call", payload, timeout=timeout or self.timeout)
        resp.raise_for_status()
        return resp.json()

    # ===== file/audio gerelateerde endpoints (legacy, via Py2) =====

    def upload_only(self, file_storage, filename=None, remote_dir=None, timeout=None):
        """
        Proxy naar /upload_only (DEPRECATED aan Py2-zijde, maar nog bruikbaar).
        :param file_storage: werkzeug FileStorage-achtig object met .stream en .filename
        :param filename:     optionele doelbestandsnaam op de robot
        :param remote_dir:   optionele doelmap, default wordt aan Py2-kant bepaald
        """
        # Bepaal bestandsnaam en mimetype
        orig_name = getattr(file_storage, "filename", None) or "upload.bin"
        filename = filename or orig_name
        mimetype = getattr(file_storage, "mimetype", None) or "application/octet-stream"

        files = {
            "file": (orig_name, file_storage.stream, mimetype),
        }
        data = {"filename": filename}
        if remote_dir:
            data["remote_dir"] = remote_dir

        resp = self._post_multipart("/upload_only", files=files, data=data, timeout=timeout or self.timeout)
        resp.raise_for_status()
        return resp.json()

    def play_audio(self, file_storage, filename=None, remote_dir=None, timeout=None):
        """
        Proxy naar /play_audio (DEPRECATED file-based audio).
        :param file_storage: werkzeug FileStorage-achtig object met .stream en .filename
        :param filename:     optionele doelbestandsnaam op de robot
        :param remote_dir:   optionele doelmap
        """
        orig_name = getattr(file_storage, "filename", None) or "audio.wav"
        filename = filename or orig_name
        mimetype = getattr(file_storage, "mimetype", None) or "audio/wav"

        files = {
            "file": (orig_name, file_storage.stream, mimetype),
        }
        data = {"filename": filename}
        if remote_dir:
            data["remote_dir"] = remote_dir

        resp = self._post_multipart("/play_audio", files=files, data=data, timeout=timeout or self.timeout)
        resp.raise_for_status()
        return resp.json()

    def play_stream(self, audio_bytes, content_type="application/octet-stream", timeout=None):
        """
        Proxy naar /play_stream voor raw PCM-audio.
        :param audio_bytes: bytes-object met audio-data
        :param content_type: Content-Type header (default application/octet-stream)
        """
        headers = {"Content-Type": content_type}
        resp = self._post_stream("/play_stream", data=audio_bytes, headers=headers, timeout=timeout or self.timeout)
        resp.raise_for_status()
        return resp.json()
