# py3_nao_behavior_manager/dialog/backends/llm_none.py
from dialog.interfaces import LLMBackend, LLMResult, History


class NoOpLLMBackend(LLMBackend):
    def generate(self, messages: History) -> LLMResult:
        return LLMResult(reply="", messages=messages)
