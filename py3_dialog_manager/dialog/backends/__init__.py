# py3_nao_behavior_manager/dialog/backends/__init__.py

# Input backends
from dialog.backends.input_audio import AudioInputBackend
from dialog.backends.input_console import ConsoleInputBackend

# Mic backends
from dialog.backends.mic_laptop import LaptopMic
from dialog.backends.mic_nao_ssh import NaoSshMic

# STT backends
from dialog.backends.stt_whisper import WhisperSTTBackend

# LLM backends
from dialog.backends.llm_echo import EchoLLMBackend
from dialog.backends.llm_none import NoOpLLMBackend
from dialog.backends.llm_ollama import OllamaClient, OllamaLLMBackend

# Output backends
from dialog.backends.output_console import ConsoleOutputBackend
from dialog.backends.output_none import NoOpOutputBackend
from dialog.backends.output_nao import NaoTTSOutputBackend

__all__ = [
    # input
    "AudioInputBackend",
    "ConsoleInputBackend",
    # mic
    "LaptopMic",
    "NaoSshMic",
    # stt
    "WhisperSTTBackend",
    # llm
    "EchoLLMBackend",
    "NoOpLLMBackend",
    "OllamaClient",
    "OllamaLLMBackend",
    # output
    "ConsoleOutputBackend",
    "NoOpOutputBackend",
    "NaoTTSOutputBackend",
]
