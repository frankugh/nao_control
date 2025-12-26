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
        Audiobytes als WAV (RIFF/WAVE) zodat STT backends 
        (zoals WhisperSTTBackend) het direct kunnen parsen.
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
class LLMResult:
    """Resultaat van de LLM-call."""
    reply: str
    messages: History
    tokens_in: Optional[int] = None
    tokens_out: Optional[int] = None


@dataclass
class DialogTurn:
    """
    Volledige data van één dialoogronde:
    audio-in, STT-resultaat en LLM-resultaat.
    """
    user_audio: UtteranceAudio
    stt: STTResult
    llm: LLMResult


# ====== Interfaces / Protocols ======

@runtime_checkable
class MicBackend(Protocol):
    """
    Microfoon / audio-input.

    Implementaties mogen intern VAD gebruiken, zolang er maar
    precies één utterance wordt teruggegeven.
    """

    def capture_utterance(self, timeout_s: float = 10.0) -> UtteranceAudio:
        """
        Luister tot er een utterance is (of tot timeout) en geef daarvan
        de audio terug.
        """
        ...


@runtime_checkable
class STTBackend(Protocol):
    """Spraak-naar-tekst backend."""

    def transcribe(self, audio: UtteranceAudio) -> STTResult:
        """
        Converteer de gegeven utterance naar tekst.
        """
        ...


@runtime_checkable
class LLMBackend(Protocol):
    """LLM-backend (local of cloud)."""

    def generate(self, messages: History) -> LLMResult:
        """
        Genereer een antwoord op basis van de chatgeschiedenis.
        De laatste user-boodschap zit in `messages` aan het eind.
        """
        ...


@runtime_checkable
class TTSBackend(Protocol):
    """Tekst-naar-spraak backend (bijv. NAO TTS)."""

    def speak(self, text: str) -> None:
        """
        Spreek de gegeven tekst uit. Blocking of non-blocking is
        aan de implementatie, maar moet duidelijk gedocumenteerd worden.
        """
        ...


class DialogPipeline(Protocol):
    """
    Combinatie van mic + stt + llm + tts voor één dialoogronde.
    Stateless: geen eigen geheugen; history komt van buiten.
    """

    def run_once(self, history: Optional[History] = None) -> DialogTurn:
        """
        1) mic.capture_utterance()
        2) stt.transcribe()
        3) history + user-bericht -> llm.generate()
        4) tts.speak()

        Retourneert een DialogTurn met alle tussenresultaten.
        """
        ...
