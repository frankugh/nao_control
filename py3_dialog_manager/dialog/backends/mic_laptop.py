# app/dialog/backends/mic_laptop.py
from __future__ import annotations

import queue
import sys
from typing import Optional

import numpy as np
import sounddevice as sd

from dialog.interfaces import MicBackend, UtteranceAudio
from dialog.backends.vad_segmenter import (
    RmsVadConfig,
    RmsVadUtteranceCapturer,
    int16_to_wav_bytes,
)


class LaptopMic(MicBackend):
    """
    Laptop microfoon backend met utterance-VAD (RMS):
    - wacht op spraak (boven threshold)
    - neemt door tot er N ms stilte is
    - retourneert precies één utterance als WAV-bytes in UtteranceAudio.pcm
    """

    def __init__(
        self,
        sample_rate: int = 16000,
        start_threshold_rms: int = 500,  # hoger = minder gevoelig
        stop_silence_ms: int = 1000,     # stilte om utterance te beëindigen
        pre_roll_ms: int = 200,          # beetje audio vóór start bewaren
        max_utterance_s: float = 12.0,   # safety cap
        input_device: Optional[int] = None,  # None = default mic
        block_ms: int = 20,              # callback blokgrootte
    ) -> None:
        self.cfg = RmsVadConfig(
            sample_rate=sample_rate,
            start_threshold_rms=start_threshold_rms,
            stop_silence_ms=stop_silence_ms,
            pre_roll_ms=pre_roll_ms,
            max_utterance_s=max_utterance_s,
            block_ms=block_ms,
        )
        self.input_device = input_device

        self._q: "queue.Queue[np.ndarray]" = queue.Queue()

    def _cb(self, indata, frames, t, status):
        if status:
            print(status, file=sys.stderr)
        # indata: shape (frames, channels), dtype int16
        self._q.put(indata[:, 0].copy())

    def capture_utterance(self, timeout_s: float = 10.0) -> UtteranceAudio:
        blocksize = int(self.cfg.sample_rate * (self.cfg.block_ms / 1000.0))
        vad = RmsVadUtteranceCapturer(self.cfg)

        def get_block(timeout: float) -> Optional[np.ndarray]:
            try:
                return self._q.get(timeout=timeout)
            except queue.Empty:
                return None

        with sd.InputStream(
            samplerate=self.cfg.sample_rate,
            channels=1,
            dtype="int16",
            callback=self._cb,
            blocksize=blocksize,
            device=self.input_device,
        ):
            audio_int16 = vad.capture(get_block=get_block, timeout_s=timeout_s)

        wav_bytes = int16_to_wav_bytes(audio_int16, self.cfg.sample_rate)

        return UtteranceAudio(
            pcm=wav_bytes,          # WAV-bytes, matcht WhisperSTTBackend
            sample_rate=self.cfg.sample_rate,
            channels=1,
            sample_width=2,
        )
