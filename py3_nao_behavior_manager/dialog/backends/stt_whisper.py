# app/dialog/backends/stt_whisper.py

import io
import wave
from typing import Optional, Tuple

import numpy as np
from faster_whisper import WhisperModel

from dialog.interfaces import STTBackend, UtteranceAudio, STTResult


def _wav_bytes_to_float32(wav_bytes: bytes) -> Tuple[np.ndarray, int]:
    with io.BytesIO(wav_bytes) as bio:
        with wave.open(bio, "rb") as wf:
            sample_rate = wf.getframerate()
            num_channels = wf.getnchannels()
            sample_width = wf.getsampwidth()
            frames = wf.readframes(wf.getnframes())

    if sample_width != 2:
        raise ValueError("Verwacht 16-bit audio (sample_width=2)")

    audio = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0

    if num_channels > 1:
        audio = audio.reshape(-1, num_channels).mean(axis=1)

    return audio, sample_rate


def _select_backend() -> Tuple[str, str]:
    try:
        import torch  # type: ignore

        use_gpu = torch.cuda.is_available()
    except Exception:
        use_gpu = False

    device = "cuda" if use_gpu else "cpu"
    compute_type = "float16" if use_gpu else "int8"
    return device, compute_type


class WhisperSTTBackend(STTBackend):
    """
    STT-backend op basis van faster-whisper.

    Laadt het model lazy bij de eerste call en hergebruikt het daarna.
    """

    def __init__(
        self,
        model_name: str = "small",
        language: str = "nl",
    ) -> None:
        self.model_name = model_name
        self.language = language
        self._model: Optional[WhisperModel] = None

    def _get_model(self) -> WhisperModel:
        if self._model is None:
            device, compute_type = _select_backend()
            self._model = WhisperModel(self.model_name, device=device, compute_type=compute_type)
        return self._model

    def transcribe(self, audio: UtteranceAudio) -> STTResult:
        wav_bytes = audio.pcm  # hier zitten de WAV-bytes in
        float_audio, sample_rate = _wav_bytes_to_float32(wav_bytes)

        model = self._get_model()
        segments, info = model.transcribe(
            float_audio,
            language=self.language,
            vad_filter=True,
            vad_parameters=dict(min_silence_duration_ms=800),
        )

        text = "".join(seg.text for seg in segments).strip()
        return STTResult(text=text, language=self.language, confidence=None)
