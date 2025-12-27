# py3_nao_behavior_manager/dialog/backends/output_none.py
from dialog.interfaces import OutputBackend


class NoOpOutputBackend(OutputBackend):
    def emit(self, text: str) -> None:
        return
