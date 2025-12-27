# py3_nao_behavior_manager/dialog/backends/llm_echo.py
from dialog.interfaces import LLMBackend, LLMResult, History


class EchoLLMBackend(LLMBackend):
    """'Niets doen' als LLM: geeft de laatste user-tekst terug als reply."""

    def generate(self, messages: History) -> LLMResult:
        user_text = ""
        if messages:
            user_text = messages[-1].get("content", "")
        return LLMResult(reply=user_text, messages=messages)
