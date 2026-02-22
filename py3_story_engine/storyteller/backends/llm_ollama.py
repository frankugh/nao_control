from __future__ import annotations

from typing import Any, Dict, Optional

from ollama import Client as OllamaHttpClient

from storyteller.interfaces import LLMBackend, LLMResult, History


class OllamaClient:
    """Thin wrapper around official Ollama Python client."""

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
        self.model = str(model or "").strip()
        self.options = dict(options or {})

    def chat(self, messages: History) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"messages": messages, "stream": False}
        if self.options:
            payload["options"] = self.options
        return self._client.chat(self.model, **payload)


class OllamaLLMBackend(LLMBackend):
    def __init__(self, client: OllamaClient) -> None:
        self.client = client

    def generate(self, messages: History) -> LLMResult:
        resp = self.client.chat(messages)
        msg = resp.get("message", {})
        reply = str(msg.get("content") or "").strip()
        new_history: History = list(messages) + [{"role": "assistant", "content": reply}]
        return LLMResult(reply=reply, messages=new_history)
