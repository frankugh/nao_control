# app/dialog/backends/mic_laptop.py
from __future__ import annotations

import queue
import sys
from typing import Callable, Optional

import numpy as np
import sounddevice as sd

from dialog.interfaces import MicBackend, UtteranceAudio
from dialog.backends.vad_segmenter import (
    RmsVadConfig,
    RmsVadStreamingSegmenter,
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
        self._q = queue.Queue()

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

    def record_until(self, stop_event, *, max_duration_s: Optional[float] = None) -> UtteranceAudio:
        """
        Neem raw audio op tot stop_event gezet wordt (zonder VAD).
        """
        blocksize = int(self.cfg.sample_rate * (self.cfg.block_ms / 1000.0))
        max_samples = None if max_duration_s is None else int(self.cfg.sample_rate * float(max_duration_s))
        self._q = queue.Queue()

        def get_block(timeout: float) -> Optional[np.ndarray]:
            try:
                return self._q.get(timeout=timeout)
            except queue.Empty:
                return None

        chunks: list[np.ndarray] = []
        total = 0

        with sd.InputStream(
            samplerate=self.cfg.sample_rate,
            channels=1,
            dtype="int16",
            callback=self._cb,
            blocksize=blocksize,
            device=self.input_device,
        ):
            while not stop_event.is_set():
                block = get_block(0.1)
                if block is None:
                    continue
                if block.ndim != 1:
                    block = block.reshape(-1)
                if block.dtype != np.int16:
                    block = block.astype(np.int16)
                chunks.append(block)
                total += block.size
                if max_samples is not None and total >= max_samples:
                    break

        if chunks:
            audio_int16 = np.concatenate(chunks).astype(np.int16)
        else:
            audio_int16 = np.zeros((0,), dtype=np.int16)

        wav_bytes = int16_to_wav_bytes(audio_int16, self.cfg.sample_rate)
        return UtteranceAudio(
            pcm=wav_bytes,
            sample_rate=self.cfg.sample_rate,
            channels=1,
            sample_width=2,
        )

    def stream_utterances(
        self,
        stop_event,
        *,
        timeout_s: float = 10.0,
        on_utterance: Callable[[UtteranceAudio], None],
        on_timeout: Optional[Callable[[], None]] = None,
    ) -> None:
        """
        Houd de microfoonstream open en lever utterances door zodra de
        stateful VAD ze afgrenst.
        """
        blocksize = int(self.cfg.sample_rate * (self.cfg.block_ms / 1000.0))
        q: "queue.Queue[np.ndarray]" = queue.Queue()
        vad = RmsVadStreamingSegmenter(self.cfg)

        def _cb(indata, frames, t, status):
            if status:
                print(status, file=sys.stderr)
            q.put(indata[:, 0].copy())

        def _emit(audio_int16: Optional[np.ndarray]) -> None:
            if audio_int16 is None or audio_int16.size == 0:
                return
            wav_bytes = int16_to_wav_bytes(audio_int16, self.cfg.sample_rate)
            on_utterance(
                UtteranceAudio(
                    pcm=wav_bytes,
                    sample_rate=self.cfg.sample_rate,
                    channels=1,
                    sample_width=2,
                )
            )

        with sd.InputStream(
            samplerate=self.cfg.sample_rate,
            channels=1,
            dtype="int16",
            callback=_cb,
            blocksize=blocksize,
            device=self.input_device,
        ):
            while not stop_event.is_set():
                try:
                    block = q.get(timeout=0.1)
                except queue.Empty:
                    if on_timeout is not None and vad.poll_timeout(timeout_s):
                        on_timeout()
                    continue
                utterance = vad.feed(block)
                if utterance is not None:
                    _emit(utterance)
            _emit(vad.flush())
