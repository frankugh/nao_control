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

    def capture_from_buffer(self, audio_int16: np.ndarray) -> np.ndarray:
        """
        Pas dezelfde VAD-logica toe op een bestaand audio-buffer.
        """
        if audio_int16.ndim != 1:
            audio_int16 = audio_int16.reshape(-1)
        if audio_int16.dtype != np.int16:
            audio_int16 = audio_int16.astype(np.int16)

        started = False
        silence_run = 0
        pre_roll = np.zeros((0,), dtype=np.int16)
        captured: list[np.ndarray] = []

        total_len = audio_int16.size
        idx = 0
        while idx < total_len:
            block = audio_int16[idx : idx + self._block_n]
            idx += self._block_n
            if block.size == 0:
                continue

            rms = self._rms(block)

            if not started:
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
            raise TimeoutError("Geen spraak gedetecteerd in buffer.")

        return np.concatenate(captured).astype(np.int16)


class RmsVadStreamingSegmenter:
    """
    Stateful RMS-VAD voor continue streams.

    Houdt een pre-roll buffer bij terwijl de stream open blijft en levert
    complete utterances terug zodra stilte of de safety cap bereikt is.
    """

    def __init__(self, cfg: RmsVadConfig) -> None:
        self.cfg = cfg
        self._block_n = int(cfg.sample_rate * (cfg.block_ms / 1000.0))
        self._pre_roll_n = int(cfg.sample_rate * (cfg.pre_roll_ms / 1000.0))
        self._stop_sil_n = int(cfg.sample_rate * (cfg.stop_silence_ms / 1000.0))
        self._max_n = int(cfg.sample_rate * cfg.max_utterance_s)
        self._waiting_started_at = time.time()
        self._started = False
        self._silence_run = 0
        self._pre_roll = np.zeros((0,), dtype=np.int16)
        self._captured: list[np.ndarray] = []
        self._captured_n = 0

    @staticmethod
    def _rms(block: np.ndarray) -> int:
        if block.size == 0:
            return 0
        x = block.astype(np.float32)
        return int(np.sqrt(np.mean(x * x)) + 0.5)

    def _reset_waiting(self, *, now: Optional[float] = None) -> None:
        self._started = False
        self._silence_run = 0
        self._pre_roll = np.zeros((0,), dtype=np.int16)
        self._captured = []
        self._captured_n = 0
        self._waiting_started_at = float(now if now is not None else time.time())

    def _normalize_block(self, block: np.ndarray) -> np.ndarray:
        if block.ndim != 1:
            block = block.reshape(-1)
        if block.dtype != np.int16:
            block = block.astype(np.int16)
        return block

    def feed(self, block: np.ndarray, *, now: Optional[float] = None) -> Optional[np.ndarray]:
        block = self._normalize_block(block)
        if block.size == 0:
            return None
        ts = float(now if now is not None else time.time())
        rms = self._rms(block)

        if not self._started:
            if self._pre_roll_n > 0:
                self._pre_roll = np.concatenate([self._pre_roll, block])
                if self._pre_roll.size > self._pre_roll_n:
                    self._pre_roll = self._pre_roll[-self._pre_roll_n :]
            if rms >= self.cfg.start_threshold_rms:
                self._started = True
                self._silence_run = 0
                self._captured = []
                self._captured_n = 0
                if self._pre_roll.size:
                    self._captured.append(self._pre_roll.copy())
                    self._captured_n += int(self._pre_roll.size)
                self._captured.append(block.copy())
                self._captured_n += int(block.size)
            return None

        self._captured.append(block.copy())
        self._captured_n += int(block.size)
        if rms < self.cfg.start_threshold_rms:
            self._silence_run += int(block.size)
        else:
            self._silence_run = 0

        if self._silence_run >= self._stop_sil_n or self._captured_n >= self._max_n:
            utterance = np.concatenate(self._captured).astype(np.int16)
            self._reset_waiting(now=ts)
            return utterance
        return None

    def poll_timeout(self, timeout_s: float, *, now: Optional[float] = None) -> bool:
        if self._started:
            return False
        try:
            timeout_value = float(timeout_s)
        except Exception:
            timeout_value = 0.0
        if timeout_value <= 0:
            return False
        ts = float(now if now is not None else time.time())
        if (ts - self._waiting_started_at) >= timeout_value:
            self._waiting_started_at = ts
            return True
        return False

    def flush(self, *, now: Optional[float] = None) -> Optional[np.ndarray]:
        if not self._started or not self._captured:
            self._reset_waiting(now=now)
            return None
        utterance = np.concatenate(self._captured).astype(np.int16)
        self._reset_waiting(now=now)
        return utterance
