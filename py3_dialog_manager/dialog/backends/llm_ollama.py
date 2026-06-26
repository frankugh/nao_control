# app/dialog/backends/llm_ollama.py

import os
from typing import Dict, Any, Optional

from ollama import Client as OllamaHttpClient

from dialog.interfaces import LLMBackend, LLMResult, History, ChatMessage


def normalize_ollama_api_key(api_key: Optional[str]) -> Optional[str]:
    value = str(api_key or "").strip()
    return value or None


def _make_ollama_http_client(host: str, headers: Dict[str, str], *, use_env_api_key: bool) -> OllamaHttpClient:
    if use_env_api_key or headers:
        return OllamaHttpClient(host=host, headers=headers or None)

    previous = os.environ.pop("OLLAMA_API_KEY", None)
    try:
        return OllamaHttpClient(host=host, headers=None)
    finally:
        if previous is not None:
            os.environ["OLLAMA_API_KEY"] = previous


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
        think: Optional[Any] = None,
        use_env_api_key: bool = True,
    ) -> None:
        headers: Dict[str, str] = {}
        clean_api_key = normalize_ollama_api_key(api_key)
        if clean_api_key:
            headers["Authorization"] = f"Bearer {clean_api_key}"
        self._client = _make_ollama_http_client(host, headers, use_env_api_key=use_env_api_key)
        self.model = model
        self.options = dict(options) if isinstance(options, dict) and options else None
        self.think = think

    def chat(self, messages: History) -> Dict[str, Any]:
        kwargs: Dict[str, Any] = {"messages": messages, "stream": False}
        if self.think is not None:
            kwargs["think"] = self.think
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
