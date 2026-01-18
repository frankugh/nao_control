# py3_dialog_manager/dialog/backends/stt_vosk.py
import io
import json
import os
import wave
from typing import Optional, Tuple

import numpy as np
from vosk import KaldiRecognizer, Model, SetLogLevel

from dialog.interfaces import STTBackend, UtteranceAudio, STTResult


def _wav_bytes_to_pcm16_mono(wav_bytes: bytes) -> Tuple[bytes, int]:
    with io.BytesIO(wav_bytes) as bio:
        with wave.open(bio, "rb") as wf:
            sample_rate = wf.getframerate()
            num_channels = wf.getnchannels()
            sample_width = wf.getsampwidth()
            frames = wf.readframes(wf.getnframes())

    if sample_width != 2:
        raise ValueError("Verwacht 16-bit audio (sample_width=2)")

    if num_channels == 1:
        return frames, sample_rate

    audio = np.frombuffer(frames, dtype=np.int16)
    audio = audio.reshape(-1, num_channels).mean(axis=1).astype(np.int16)
    return audio.tobytes(), sample_rate


class VoskSTTBackend(STTBackend):
    """
    STT-backend op basis van Vosk.

    Config (via JSON -> params):
      - model_path: str                    (required)
      - language: str                      (default: nl)
      - sample_rate: int | null            (optional, check-only)
      - max_alternatives: int              (default: 0)
      - enable_words: bool                 (default: False)
      - grammar: list[str] | str | null    (optional)
      - log_level: int                     (default: -1)
    """

    def __init__(
        self,
        model_path: str,
        language: str = "nl",
        *,
        sample_rate: Optional[int] = None,
        max_alternatives: int = 0,
        enable_words: bool = False,
        grammar: Optional[object] = None,
        log_level: int = -1,
    ) -> None:
        if not isinstance(model_path, str) or not model_path.strip():
            raise ValueError("model_path is verplicht voor Vosk.")

        self.model_path = model_path
        self.language = language
        self.sample_rate = int(sample_rate) if sample_rate is not None else None
        self.max_alternatives = int(max_alternatives)
        self.enable_words = bool(enable_words)
        self.grammar = grammar

        SetLogLevel(int(log_level))

        self._model: Optional[Model] = None

    def _get_model(self) -> Model:
        if self._model is None:
            if not os.path.isdir(self.model_path):
                raise FileNotFoundError(f"Vosk model_path bestaat niet: {self.model_path}")
            self._model = Model(self.model_path)
        return self._model

    def _make_recognizer(self, sample_rate: int) -> KaldiRecognizer:
        model = self._get_model()

        grammar_json = None
        if self.grammar is not None:
            if isinstance(self.grammar, (list, tuple)):
                grammar_json = json.dumps(list(self.grammar))
            elif isinstance(self.grammar, str):
                grammar_json = self.grammar
            else:
                raise ValueError("grammar moet een lijst of string zijn.")

        if grammar_json:
            rec = KaldiRecognizer(model, sample_rate, grammar_json)
        else:
            rec = KaldiRecognizer(model, sample_rate)

        rec.SetMaxAlternatives(self.max_alternatives)
        rec.SetWords(self.enable_words)
        return rec

    def transcribe(self, audio: UtteranceAudio) -> STTResult:
        pcm_bytes, wav_sr = _wav_bytes_to_pcm16_mono(audio.pcm)
        if self.sample_rate is not None and self.sample_rate != wav_sr:
            raise ValueError(
                f"Vosk sample_rate mismatch: wav={wav_sr}, expected={self.sample_rate}. "
                "Zorg dat input audio matcht of laat sample_rate weg."
            )

        rec = self._make_recognizer(wav_sr)
        rec.AcceptWaveform(pcm_bytes)
        result = json.loads(rec.FinalResult() or "{}")

        text = (result.get("text") or "").strip()
        return STTResult(text=text, language=self.language, confidence=None)
