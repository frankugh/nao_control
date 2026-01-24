# app/dialog/backends/tts_nao.py

import os
from typing import Optional

import requests

from dialog.interfaces import TTSBackend
from dialog.nao_api_router import NaoApiRouter


class Py2NaoTTSBackend(TTSBackend):
    """
    TTS-backend die direct naar de Py2-NAO-API /tts endpoint post.
    """

    def __init__(
        self,
        base_url: str | None = None,
        timeout: float = 5.0,
        api_router: Optional[NaoApiRouter] = None,
    ) -> None:
        self.api_router = api_router
        if self.api_router is None:
            self.base_url = (base_url or os.environ.get("PY2_NAO_API_URL", "http://192.168.0.110:5000")).rstrip("/")
        else:
            self.base_url = None
        self.timeout = timeout

    def speak(self, text: str) -> None:
        payload = {"text": text}
        if self.api_router is not None:
            try:
                resp = self.api_router.post("/tts", json=payload, timeout=self.timeout)
            except requests.RequestException as exc:
                print("[NAO] TTS request failed: %s" % exc)
                return
        else:
            url = f"{self.base_url}/tts"
            try:
                resp = requests.post(url, json=payload, timeout=self.timeout)
            except requests.RequestException as exc:
                print("[NAO] TTS request failed: %s" % exc)
                return
        if resp.status_code >= 400:
            print("[NAO] TTS returned %s; ignoring to keep dialog running" % resp.status_code)
            return
