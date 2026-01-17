# py3_nao_behavior_manager/dialog/interfaces.py
from __future__ import annotations

from dataclasses import dataclass
from typing import (
    Any,
    Dict,
    List,
    Optional,
    Protocol,
    Tuple,
    TypedDict,
    Literal,
    runtime_checkable,
)


# ====== Basis types ======

ChatRole = Literal["system", "user", "assistant"]
CmdRecBundle = Literal["none", "latest"] | str
ConfirmMethod = Literal["none", "head", "enter", "popup"]


class ChatMessage(TypedDict):
    """Één bericht in de LLM-chatgeschiedenis."""
    role: ChatRole
    content: str


History = List[ChatMessage]


def parse_cmdrec_bundle(value: Any) -> CmdRecBundle:
    if value is None:
        return "none"
    if not isinstance(value, str):
        raise ValueError("cmdrec moet een string zijn (none, latest, v<nummer>).")
    v = value.strip()
    v_lower = v.lower()
    if v_lower in ("none", "latest"):
        return v_lower
    if v_lower.startswith("v") and v_lower[1:].isdigit():
        return f"v{int(v_lower[1:])}"
    raise ValueError("cmdrec moet 'none', 'latest' of 'v<nummer>' zijn.")


def parse_confirm_method(value: Any) -> ConfirmMethod:
    if value is None:
        return "none"
    if not isinstance(value, str):
        raise ValueError("confirm_method moet een string zijn (none/head/enter/popup).")
    v = value.strip().lower()
    if v in ("none", "head", "enter", "popup"):
        return v  # type: ignore[return-value]
    raise ValueError("confirm_method moet 'none', 'head', 'enter' of 'popup' zijn.")


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


@dataclass
class CommandDecision:
    label: str
    confidence: float
    raw_text: str
    resolved: Optional[Dict[str, str]] = None


@dataclass
class RouteDecision:
    is_command: bool
    command: Optional[CommandDecision] = None
    reason: Optional[str] = None
    top3: Optional[List[Tuple[str, float]]] = None


@dataclass
class ConfirmRequest:
    prompt: str
    method: ConfirmMethod
    timeout_s: float
    command: CommandDecision


@runtime_checkable
class ICommandRecognizer(Protocol):
    def route(self, text: str, mode: str, active_behavior: str | None) -> RouteDecision:
        ...


@runtime_checkable
class IConfirmer(Protocol):
    def confirm(self, req: ConfirmRequest) -> bool:
        ...


@runtime_checkable
class ICommandExecutor(Protocol):
    def execute(self, cmd: CommandDecision) -> None:
        ...
