# py3_nao_behavior_manager/dialog/backends/stt_whisper.py
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


def _cuda_available_via_torch() -> bool:
    try:
        import torch  # type: ignore
        return bool(torch.cuda.is_available())
    except Exception:
        return False


def _select_backend(
    mode: str,
    *,
    model_name: str,
    model_cpu: Optional[str],
    model_gpu: Optional[str],
    compute_type_cpu: str,
    compute_type_gpu: str,
) -> Tuple[str, str, str]:
    """
    Returns: (device, compute_type, chosen_model_name)
    """
    m = (mode or "AUTO").upper()
    if m not in ("AUTO", "GPU", "CPU"):
        raise ValueError("mode moet AUTO, GPU of CPU zijn")

    if m == "GPU":
        use_gpu = True
    elif m == "CPU":
        use_gpu = False
    else:
        use_gpu = _cuda_available_via_torch()

    if use_gpu:
        return "cuda", compute_type_gpu, (model_gpu or model_name)
    else:
        return "cpu", compute_type_cpu, (model_cpu or model_name)


class WhisperSTTBackend(STTBackend):
    """
    STT-backend op basis van faster-whisper.
    Laadt het model lazy bij de eerste call en hergebruikt het daarna.

    Config (via JSON -> params):
      - mode: "AUTO" | "GPU" | "CPU"        (default: AUTO)
      - model_name: str                     (default: small)  (backward compatible)
      - model_cpu: str | null               (optional)
      - model_gpu: str | null               (optional)
      - compute_type_cpu: str               (default: int8)
      - compute_type_gpu: str               (default: float16)
      - language: str                       (default: nl)
      - vad_filter: bool                    (default: True)
      - min_silence_duration_ms: int        (default: 800)
    """

    def __init__(
        self,
        model_name: str = "small",
        language: str = "nl",
        *,
        mode: str = "AUTO",
        model_cpu: Optional[str] = None,
        model_gpu: Optional[str] = None,
        compute_type_cpu: str = "int8",
        compute_type_gpu: str = "float16",
        vad_filter: bool = True,
        min_silence_duration_ms: int = 800,
    ) -> None:
        self.mode = mode
        self.model_name = model_name
        self.model_cpu = model_cpu
        self.model_gpu = model_gpu
        self.compute_type_cpu = compute_type_cpu
        self.compute_type_gpu = compute_type_gpu

        self.language = language
        self.vad_filter = bool(vad_filter)
        self.min_silence_duration_ms = int(min_silence_duration_ms)

        self._model: Optional[WhisperModel] = None
        self._device: Optional[str] = None
        self._compute_type: Optional[str] = None
        self._chosen_model_name: Optional[str] = None

    def _get_model(self) -> WhisperModel:
        if self._model is None:
            device, compute_type, chosen_model = _select_backend(
                self.mode,
                model_name=self.model_name,
                model_cpu=self.model_cpu,
                model_gpu=self.model_gpu,
                compute_type_cpu=self.compute_type_cpu,
                compute_type_gpu=self.compute_type_gpu,
            )

            # Probeer CUDA echt te initialiseren; als dat faalt (driver/ct2), val terug naar CPU.
            try:
                self._model = WhisperModel(chosen_model, device=device, compute_type=compute_type)
                self._device = device
                self._compute_type = compute_type
                self._chosen_model_name = chosen_model
            except Exception:
                # Fallback naar CPU
                cpu_model = self.model_cpu or self.model_name
                self._model = WhisperModel(cpu_model, device="cpu", compute_type=self.compute_type_cpu)
                self._device = "cpu"
                self._compute_type = self.compute_type_cpu
                self._chosen_model_name = cpu_model

        return self._model

    def transcribe(self, audio: UtteranceAudio) -> STTResult:
        wav_bytes = audio.pcm  # WAV-bytes
        float_audio, _sr = _wav_bytes_to_float32(wav_bytes)

        model = self._get_model()

        kwargs = {"language": self.language}
        if self.vad_filter:
            kwargs["vad_filter"] = True
            kwargs["vad_parameters"] = dict(min_silence_duration_ms=self.min_silence_duration_ms)

        segments, _info = model.transcribe(float_audio, **kwargs)

        text = "".join(seg.text for seg in segments).strip()
        return STTResult(text=text, language=self.language, confidence=None)
