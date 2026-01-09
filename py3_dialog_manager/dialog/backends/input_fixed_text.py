# py3_nao_behavior_manager/dialog/backends/input_fixed_text.py
from __future__ import annotations

from dialog.interfaces import UserInput


class FixedTextInputBackend:
    """
    Input backend that returns one fixed text, then empty input.

    Used by the web UI to inject edited transcript text into the existing
    Input->LLM->Output pipeline without duplicating pipeline code.
    """

    def __init__(self, text: str) -> None:
        self._text = text
        self._used = False

    def get_input(self) -> UserInput:
        if self._used:
            return UserInput(raw_text="", text="", audio=None, stt=None)
        self._used = True
        return UserInput(raw_text=self._text, text=self._text, audio=None, stt=None)
