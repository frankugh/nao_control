# py3_nao_behavior_manager/dialog/interfaces.py
from __future__ import annotations

from dataclasses import dataclass
from typing import (
    List,
    Optional,
    Protocol,
    TypedDict,
    Literal,
    runtime_checkable,
)


# ====== Basis types ======

ChatRole = Literal["system", "user", "assistant"]


class ChatMessage(TypedDict):
    """Één bericht in de LLM-chatgeschiedenis."""
    role: ChatRole
    content: str


History = List[ChatMessage]


@dataclass
class UtteranceAudio:
    """
    Eén gesproken utterance als audio.

    pcm:
        Audiobytes. In deze codebase wordt dit in de praktijk vaak als WAV-bytes
        (RIFF/WAVE) gebruikt, omdat WhisperSTTBackend wave.open(...) doet.
        (De naam 'pcm' is historisch.)
    sample_rate:
        Sample rate in Hz (bijv. 16000).
    channels:
        Aantal kanalen (1 = mono).
    sample_width:
        Bytes per sample (2 voor 16-bit).
    """
    pcm: bytes
    sample_rate: int
    channels: int = 1
    sample_width: int = 2


@dataclass
class STTResult:
    """Resultaat van spraak-naar-tekst."""
    text: str
    language: str = "nl"
    confidence: Optional[float] = None


@dataclass
class UserInput:
    """
    Genormaliseerde user-input voor de pipeline.

    raw_text:
        De oorspronkelijke tekst (STT-output of getypte input).
    text:
        De uiteindelijke tekst die naar de LLM gaat (na evt. edit/confirm).
    audio:
        Optioneel: de audio-utterance (alleen bij audio input).
    stt:
        Optioneel: STTResult (alleen bij audio input).
    """
    raw_text: str
    text: str
    audio: Optional[UtteranceAudio] = None
    stt: Optional[STTResult] = None


@dataclass
class LLMResult:
    """Resultaat van de LLM-call."""
    reply: str
    messages: History
    tokens_in: Optional[int] = None
    tokens_out: Optional[int] = None


@dataclass
class DialogTurn:
    """
    Data van één ronde:
    input (text/audio), optioneel STT, en LLM-resultaat.
    """
    user_input: UserInput
    llm: LLMResult
    user_audio: Optional[UtteranceAudio] = None
    stt: Optional[STTResult] = None


# ====== Interfaces / Protocols ======

@runtime_checkable
class MicBackend(Protocol):
    """Microfoon / audio-input backend."""

    def capture_utterance(self, timeout_s: float = 10.0) -> UtteranceAudio:
        ...


@runtime_checkable
class STTBackend(Protocol):
    """Spraak-naar-tekst backend."""

    def transcribe(self, audio: UtteranceAudio) -> STTResult:
        ...


@runtime_checkable
class InputBackend(Protocol):
    """
    Input-laag: levert UserInput op (audio+stt of console text).
    Bevat ook de UX-gates (confirm/edit).
    """

    def get_input(self) -> UserInput:
        ...


@runtime_checkable
class LLMBackend(Protocol):
    """LLM-backend (local of cloud)."""

    def generate(self, messages: History) -> LLMResult:
        ...


@runtime_checkable
class OutputBackend(Protocol):
    """
    Output-laag: 'emit' kan console printen of TTS doen.
    """

    def emit(self, text: str) -> None:
        ...


@runtime_checkable
class TTSBackend(Protocol):
    """
    Legacy naam; je kunt dit blijven gebruiken in bestaande code.
    Voor de nieuwe pipeline gebruiken we OutputBackend.emit(...).
    """

    def speak(self, text: str) -> None:
        ...


class DialogPipeline(Protocol):
    """Input → LLM → Output (history komt van buiten)."""

    def run_once(self, history: Optional[History] = None) -> DialogTurn:
        ...
