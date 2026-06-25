from __future__ import annotations

from dialog.backends.llm_ollama import OllamaClient


class SpyHttpClient:
    def __init__(self) -> None:
        self.calls = []

    def chat(self, model, **kwargs):
        self.calls.append((model, kwargs))
        return {"message": {"content": "ok"}}


def test_ollama_client_chat_omits_think_when_default():
    client = OllamaClient(model="gpt-oss:120b-cloud", host="https://ollama.com")
    spy = SpyHttpClient()
    client._client = spy

    client.chat([])

    assert len(spy.calls) == 1
    model, kwargs = spy.calls[0]
    assert model == "gpt-oss:120b-cloud"
    assert kwargs["stream"] is False
    assert "think" not in kwargs


def test_ollama_client_chat_passes_boolean_think():
    client = OllamaClient(model="gemini-3-flash-preview", host="https://ollama.com", think=True)
    spy = SpyHttpClient()
    client._client = spy

    client.chat([])

    _, kwargs = spy.calls[0]
    assert kwargs["think"] is True


def test_ollama_client_chat_passes_level_think():
    client = OllamaClient(model="gpt-oss:120b-cloud", host="https://ollama.com", think="low")
    spy = SpyHttpClient()
    client._client = spy

    client.chat([])

    _, kwargs = spy.calls[0]
    assert kwargs["think"] == "low"
