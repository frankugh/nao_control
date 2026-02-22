# app/dialog/backends/llm_ollama.py

from typing import Dict, Any, Optional

from ollama import Client as OllamaHttpClient

from dialog.interfaces import LLMBackend, LLMResult, History, ChatMessage


class OllamaClient:
    """
    Dunne wrapper om de officiële Ollama Python client.
    """

    def __init__(
        self,
        model: str,
        host: str,
        api_key: Optional[str] = None,
        options: Optional[Dict[str, Any]] = None,
    ) -> None:
        headers: Dict[str, str] = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        self._client = OllamaHttpClient(host=host, headers=headers or None)
        self.model = model
        self.options = dict(options) if isinstance(options, dict) and options else None

    def chat(self, messages: History) -> Dict[str, Any]:
        kwargs: Dict[str, Any] = {"messages": messages, "stream": False}
        if self.options:
            kwargs["options"] = self.options
        return self._client.chat(self.model, **kwargs)


class OllamaLLMBackend(LLMBackend):
    """
    LLM-backend via Ollama (cloud of local, afhankelijk van host/api_key).
    """

    def __init__(self, client: OllamaClient) -> None:
        self.client = client

    def generate(self, messages: History) -> LLMResult:
        resp = self.client.chat(messages)
        msg = resp.get("message", {})
        reply = (msg.get("content") or "").strip()

        new_history: History = list(messages) + [
            ChatMessage(role="assistant", content=reply)  # type: ignore[arg-type]
        ]

        return LLMResult(
            reply=reply,
            messages=new_history,
            tokens_in=None,
            tokens_out=None,
        )
