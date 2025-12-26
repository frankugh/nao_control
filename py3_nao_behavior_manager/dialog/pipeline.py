# app/dialog/pipeline.py
import os
from dataclasses import dataclass
from typing import Optional, Dict, Any

from dialog.interfaces import (
    DialogPipeline,
    DialogTurn,
    History,
    ChatMessage,
    LLMResult,
)

# Mic backends
from dialog.backends.mic_nao_ssh import NaoSshMic
from dialog.backends.mic_laptop import LaptopMic

# STT backends
from dialog.backends.stt_whisper import WhisperSTTBackend

# (optioneel) Vosk backend – zie verderop
try:
    from dialog.backends.stt_vosk import VoskSTTBackend
except Exception:  # pragma: no cover
    VoskSTTBackend = None  # type: ignore


# ===== Pipelines =====

class SttPrintPipeline(DialogPipeline):
    """Mic -> STT -> print. (Geen LLM/TTS)"""

    def __init__(self, mic, stt) -> None:
        self.mic = mic
        self.stt = stt

    def run_once(self, history: Optional[History] = None) -> DialogTurn:
        audio = self.mic.capture_utterance()
        stt_res = self.stt.transcribe(audio)

        text = stt_res.text.strip()
        print(f"STT: {text}" if text else "STT: <leeg>")

        # Dummy LLMResult om DialogTurn te vullen
        messages: History = list(history or [])
        if text:
            messages.append(ChatMessage(role="user", content=text))  # type: ignore[arg-type]

        llm_res = LLMResult(reply="", messages=messages)
        return DialogTurn(user_audio=audio, stt=stt_res, llm=llm_res)


# ===== Config / builder =====

def _env_int(name: str, default: int) -> int:
    v = os.environ.get(name)
    return default if v is None or v == "" else int(v)

def _env_float(name: str, default: float) -> float:
    v = os.environ.get(name)
    return default if v is None or v == "" else float(v)

def _env_str(name: str, default: str) -> str:
    v = os.environ.get(name)
    return default if v is None or v == "" else v


def _make_mic(kind: str, cfg: Dict[str, Any]):
    kind = kind.lower()

    if kind == "laptop":
        return LaptopMic(
            sample_rate=_env_int("MIC_SR", cfg.get("mic_sr", 16000)),
            start_threshold_rms=_env_int("MIC_VAD_THRESHOLD", cfg.get("mic_vad_threshold", 500)),
            stop_silence_ms=_env_int("MIC_VAD_SILENCE_MS", cfg.get("mic_vad_silence_ms", 1000)),
            pre_roll_ms=_env_int("MIC_VAD_PREROLL_MS", cfg.get("mic_vad_preroll_ms", 200)),
            max_utterance_s=_env_float("MIC_MAX_S", cfg.get("mic_max_s", 12.0)),
            input_device=cfg.get("mic_device", None),
            block_ms=_env_int("MIC_BLOCK_MS", cfg.get("mic_block_ms", 20)),
        )

    if kind == "nao_ssh":
        nao_host = _env_str("NAO_HOST", cfg.get("nao_host", "192.168.0.101"))
        nao_user = _env_str("NAO_SSH_USER", cfg.get("nao_user", "nao"))
        nao_pass = _env_str("NAO_SSH_PASS", cfg.get("nao_pass", "nao"))
        return NaoSshMic(host=nao_host, username=nao_user, password=nao_pass)

    raise ValueError(f"Onbekende mic kind: {kind!r}")


def _make_stt(kind: str, cfg: Dict[str, Any]):
    kind = kind.lower()

    if kind == "whisper":
        model = _env_str("WHISPER_MODEL", cfg.get("whisper_model", "small"))
        lang = _env_str("WHISPER_LANG", cfg.get("whisper_lang", "nl"))
        return WhisperSTTBackend(model_name=model, language=lang)

    if kind == "vosk":
        if VoskSTTBackend is None:
            raise RuntimeError("VoskSTTBackend niet beschikbaar. Voeg dialog/backends/stt_vosk.py toe en installeer vosk.")
        model_path = _env_str("VOSK_MODEL_PATH", cfg.get("vosk_model_path", ""))
        lang = _env_str("VOSK_LANG", cfg.get("vosk_lang", "nl"))
        return VoskSTTBackend(model_path=model_path, language=lang)

    raise ValueError(f"Onbekende stt kind: {kind!r}")


# Simpele “profielen” (kan je uitbreiden)
_PROFILES: Dict[str, Dict[str, Any]] = {
    # STT-only tests
    "laptop_whisper_print": {"mic": "laptop", "stt": "whisper"},
    "nao_whisper_print": {"mic": "nao_ssh", "stt": "whisper"},
    "laptop_vosk_print": {"mic": "laptop", "stt": "vosk"},

    # bestaande (dialoog) kan je hier later weer toevoegen/uitbreiden
    # "nao_whisper_ollama_cloud": {...}
}


def build_pipeline(profile_name: str = "laptop_whisper_print", **overrides) -> DialogPipeline:
    """
    Gebruik:
      build_pipeline("laptop_whisper_print")
      build_pipeline("laptop_whisper_print", whisper_model="medium", mic_vad_silence_ms=1200)

    Env overrides werken ook:
      WHISPER_MODEL=medium MIC_VAD_SILENCE_MS=1200 ...
    """
    if profile_name not in _PROFILES:
        raise ValueError(f"Onbekend profiel: {profile_name!r}. Beschikbaar: {list(_PROFILES)}")

    cfg = dict(_PROFILES[profile_name])
    cfg.update(overrides)

    mic = _make_mic(cfg["mic"], cfg)
    stt = _make_stt(cfg["stt"], cfg)

    return SttPrintPipeline(mic=mic, stt=stt)
