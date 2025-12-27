# py3_nao_behavior_manager/dialog/backends/output_nao.py
from __future__ import annotations

from dialog.interfaces import OutputBackend
from dialog.backends.tts_nao import Py2NaoTTSBackend


class NaoTTSOutputBackend(OutputBackend):
    """
    Wrapper zodat we Py2NaoTTSBackend als OutputBackend kunnen gebruiken.
    """

    def __init__(self, **kwargs) -> None:
        self._tts = Py2NaoTTSBackend(**kwargs)

    def emit(self, text: str) -> None:
        self._tts.speak(text)
