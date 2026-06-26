from __future__ import annotations

import os

from dialog.backends import llm_ollama
from dialog.backends.llm_ollama import OllamaClient, normalize_ollama_api_key


class SpyHttpClient:
    def __init__(self) -> None:
        self.calls = []

    def chat(self, model, **kwargs):
        self.calls.append((model, kwargs))
        return {"message": {"content": "ok"}}


def test_normalize_ollama_api_key_strips_shell_profile_whitespace():
    assert normalize_ollama_api_key("\nsecret-token\n") == "secret-token"
    assert normalize_ollama_api_key("   ") is None


def test_ollama_client_strips_api_key_before_authorization_header(monkeypatch):
    seen = {}

    class SpyOllamaHttpClient:
        def __init__(self, *, host, headers=None):
            seen["host"] = host
            seen["headers"] = headers

    monkeypatch.setattr(llm_ollama, "OllamaHttpClient", SpyOllamaHttpClient)

    OllamaClient(model="mistral-large-3:675b-cloud", host="https://ollama.com", api_key="\nsecret-token\n")

    assert seen["host"] == "https://ollama.com"
    assert seen["headers"] == {"Authorization": "Bearer secret-token"}


def test_ollama_local_client_can_ignore_env_api_key(monkeypatch):
    seen = {}

    class SpyOllamaHttpClient:
        def __init__(self, *, host, headers=None):
            seen["host"] = host
            seen["headers"] = headers
            seen["env_api_key_during_init"] = os.environ.get("OLLAMA_API_KEY")

    monkeypatch.setenv("OLLAMA_API_KEY", "\nsecret-token\n")
    monkeypatch.setattr(llm_ollama, "OllamaHttpClient", SpyOllamaHttpClient)

    OllamaClient(
        model="qwen3:30b",
        host="http://localhost:11434",
        api_key=None,
        use_env_api_key=False,
    )

    assert seen["host"] == "http://localhost:11434"
    assert seen["headers"] is None
    assert seen["env_api_key_during_init"] is None
    assert os.environ.get("OLLAMA_API_KEY") == "\nsecret-token\n"


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
