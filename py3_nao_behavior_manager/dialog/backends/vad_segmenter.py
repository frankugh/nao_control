# app/dialog/backends/vad_segmenter.py
from __future__ import annotations

import io
import time
import wave
from dataclasses import dataclass
from typing import Callable, Optional

import numpy as np


def int16_to_wav_bytes(int16_mono: np.ndarray, sample_rate: int) -> bytes:
    """Encodeer int16 mono PCM naar WAV-bytes (RIFF/WAVE)."""
    if int16_mono.dtype != np.int16:
        int16_mono = int16_mono.astype(np.int16)

    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)  # int16
        wf.setframerate(sample_rate)
        wf.writeframes(int16_mono.tobytes())
    return buf.getvalue()


@dataclass(frozen=True)
class RmsVadConfig:
    sample_rate: int = 16000
    start_threshold_rms: int = 500
    stop_silence_ms: int = 1000
    pre_roll_ms: int = 200
    max_utterance_s: float = 12.0
    block_ms: int = 20


class RmsVadUtteranceCapturer:
    """
    Utterance-segmentatie op simpele RMS-energie:
    - wacht op spraak (rms >= threshold)
    - neemt door tot er 'stop_silence_ms' stilte is
    - bewaart 'pre_roll_ms' audio vóór start (handig voor eerste woord)
    """

    def __init__(self, cfg: RmsVadConfig) -> None:
        self.cfg = cfg

        self._block_n = int(cfg.sample_rate * (cfg.block_ms / 1000.0))
        self._pre_roll_n = int(cfg.sample_rate * (cfg.pre_roll_ms / 1000.0))
        self._stop_sil_n = int(cfg.sample_rate * (cfg.stop_silence_ms / 1000.0))
        self._max_n = int(cfg.sample_rate * cfg.max_utterance_s)

    @staticmethod
    def _rms(block: np.ndarray) -> int:
        # block: int16 mono
        if block.size == 0:
            return 0
        x = block.astype(np.float32)
        return int(np.sqrt(np.mean(x * x)) + 0.5)

    def capture(
        self,
        get_block: Callable[[float], Optional[np.ndarray]],
        timeout_s: float = 10.0,
    ) -> np.ndarray:
        """
        get_block(timeout) -> np.ndarray[int16] (mono) of None als er geen block beschikbaar is.

        timeout_s:
            Alleen relevant vóór start van spraak (hoe lang wachten tot er überhaupt spraak is).
        """
        started = False
        silence_run = 0  # in samples

        pre_roll = np.zeros((0,), dtype=np.int16)
        captured: list[np.ndarray] = []

        t0 = time.time()

        while True:
            if (not started) and (time.time() - t0 > timeout_s):
                raise TimeoutError("Geen spraak gedetecteerd binnen timeout.")

            block = get_block(0.1)
            if block is None:
                continue

            # zorg: 1D int16 mono
            if block.ndim != 1:
                block = block.reshape(-1)
            if block.dtype != np.int16:
                block = block.astype(np.int16)

            rms = self._rms(block)

            if not started:
                # bouw pre-roll buffer op
                if self._pre_roll_n > 0:
                    pre_roll = np.concatenate([pre_roll, block])
                    if pre_roll.size > self._pre_roll_n:
                        pre_roll = pre_roll[-self._pre_roll_n :]

                if rms >= self.cfg.start_threshold_rms:
                    started = True
                    if pre_roll.size:
                        captured.append(pre_roll.copy())
                    captured.append(block)
                    silence_run = 0
                continue

            # started
            captured.append(block)

            if rms < self.cfg.start_threshold_rms:
                silence_run += block.size
            else:
                silence_run = 0

            total_n = sum(x.size for x in captured)
            if silence_run >= self._stop_sil_n:
                break
            if total_n >= self._max_n:
                break

        if not captured:
            raise TimeoutError("Geen bruikbare audio gecaptured.")

        return np.concatenate(captured).astype(np.int16)
