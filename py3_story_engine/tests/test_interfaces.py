from __future__ import annotations

import storyteller.interfaces as interfaces


def test_interfaces_exposes_only_story_llm_basics():
    assert hasattr(interfaces, "ChatMessage")
    assert hasattr(interfaces, "History")
    assert hasattr(interfaces, "LLMResult")
    assert hasattr(interfaces, "LLMBackend")


def test_interfaces_no_longer_exposes_dm_command_types():
    for name in [
        "CmdRecBundle",
        "ConfirmMethod",
        "CommandDecision",
        "RouteDecision",
        "ConfirmRequest",
        "ICommandRecognizer",
        "IConfirmer",
        "ICommandExecutor",
        "UtteranceAudio",
        "STTResult",
        "UserInput",
    ]:
        assert not hasattr(interfaces, name)
