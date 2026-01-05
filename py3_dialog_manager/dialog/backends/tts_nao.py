# app/dialog/backends/tts_nao.py

import os
import requests

from dialog.interfaces import TTSBackend


class Py2NaoTTSBackend(TTSBackend):
    """
    TTS-backend die direct naar de Py2-NAO-API /tts endpoint post.
    """

    def __init__(self, base_url: str | None = None, timeout: float = 5.0) -> None:
        self.base_url = (base_url or os.environ.get("PY2_NAO_API_URL", "http://192.168.0.110:5000")).rstrip("/")
        self.timeout = timeout

    def speak(self, text: str) -> None:
        url = f"{self.base_url}/tts"
        payload = {"text": text}
        resp = requests.post(url, json=payload, timeout=self.timeout)
        resp.raise_for_status()
