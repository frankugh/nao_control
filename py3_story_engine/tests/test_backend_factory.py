from __future__ import annotations

import os

import pytest

from storyteller.interfaces import LLMResult
import storyteller.story_engine as story_engine


class DummyClient:
    def __init__(self, model, host, api_key=None, options=None):
        self.model = model
        self.host = host
        self.api_key = api_key
        self.options = dict(options or {})

    def chat(self, messages):
        return {"message": {"content": "ok"}}


class DummyBackend:
    def __init__(self, client):
        self.client = client

    def generate(self, messages):
        return LLMResult(reply="ok", messages=list(messages))


def test_backend_factory_accepts_ollama_local(monkeypatch):
    monkeypatch.setattr(story_engine, "OllamaClient", DummyClient)
    monkeypatch.setattr(story_engine, "OllamaLLMBackend", DummyBackend)

    backend = story_engine.build_story_llm_backend(
        llm_type="ollama_local",
        llm_model="llama3.1:8b",
        base_params={"host": "http://localhost:11434", "temperature": 0.4, "top_p": 0.8, "top_k": 32},
    )

    assert isinstance(backend, DummyBackend)
    assert backend.client.model == "llama3.1:8b"
    assert backend.client.host == "http://localhost:11434"
    assert backend.client.options == {"temperature": 0.4, "top_p": 0.8, "top_k": 32}


def test_backend_factory_accepts_ollama_cloud(monkeypatch):
    monkeypatch.setattr(story_engine, "OllamaClient", DummyClient)
    monkeypatch.setattr(story_engine, "OllamaLLMBackend", DummyBackend)

    monkeypatch.setenv("OLLAMA_API_KEY", "from_env")
    monkeypatch.setenv("OLLAMA_HOST", "https://ollama.example")
    monkeypatch.setenv("OLLAMA_MODEL", "mistral-large-675b-cloud")

    backend = story_engine.build_story_llm_backend(
        llm_type="ollama_cloud",
        llm_model="",
        base_params={"top_p": 0.95},
    )

    assert isinstance(backend, DummyBackend)
    assert backend.client.api_key == "from_env"
    assert backend.client.host == "https://ollama.example"
    assert backend.client.model == "mistral-large-675b-cloud"
    assert backend.client.options == {"top_p": 0.95}


def test_backend_factory_rejects_removed_types():
    with pytest.raises(ValueError):
        story_engine.build_story_llm_backend(llm_type="echo", llm_model="")
    with pytest.raises(ValueError):
        story_engine.build_story_llm_backend(llm_type="none", llm_model="")
    with pytest.raises(ValueError):
        story_engine.build_story_llm_backend(llm_type="wat", llm_model="")


def test_backend_factory_cloud_requires_api_key(monkeypatch):
    monkeypatch.setattr(story_engine, "OllamaClient", DummyClient)
    monkeypatch.setattr(story_engine, "OllamaLLMBackend", DummyBackend)
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)

    with pytest.raises(RuntimeError):
        story_engine.build_story_llm_backend(
            llm_type="ollama_cloud",
            llm_model="mistral-large-675b-cloud",
            base_params={"host": "https://ollama.com", "api_key": None},
        )
