# py3_nao_behavior_manager/dialog/backends/output_console.py
from __future__ import annotations

from dialog.interfaces import OutputBackend


class ConsoleOutputBackend(OutputBackend):
    def __init__(self, prefix: str = "BOT") -> None:
        self.prefix = prefix

    def emit(self, text: str) -> None:
        t = (text or "").strip()
        print(f"{self.prefix}: {t}" if t else f"{self.prefix}: <leeg>")
