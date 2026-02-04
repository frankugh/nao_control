# app/dialog/backends/mic_nao_ssh.py
from __future__ import annotations

import socket
from typing import Optional

import numpy as np
import paramiko

from dialog.interfaces import MicBackend, UtteranceAudio
from dialog.backends.vad_segmenter import (
    RmsVadConfig,
    RmsVadUtteranceCapturer,
    int16_to_wav_bytes,
)


class NaoSshMic(MicBackend):
    """
    NAO-mic backend via SSH + arecord (raw stream), met utterance-VAD (RMS).

    Belangrijk:
    - We streamen raw S16_LE (geen -d), en stoppen lokaal op stilte.
    - Output blijft WAV-bytes in UtteranceAudio.pcm (zodat STT unchanged kan blijven).
    """

    def __init__(
        self,
        host: str,
        username: str = "nao",
        password: str = "nao",
        sample_rate: int = 16000,
        start_threshold_rms: int = 500,
        stop_silence_ms: int = 1000,
        pre_roll_ms: int = 200,
        max_utterance_s: float = 12.0,
        block_ms: int = 20,
        arecord_cmd: Optional[str] = None,
    ) -> None:
        self.host = host
        self.username = username
        self.password = password

        self.cfg = RmsVadConfig(
            sample_rate=sample_rate,
            start_threshold_rms=start_threshold_rms,
            stop_silence_ms=stop_silence_ms,
            pre_roll_ms=pre_roll_ms,
            max_utterance_s=max_utterance_s,
            block_ms=block_ms,
        )

        # Stream raw PCM (geen -d) zodat we zelf stop op stilte kunnen doen.
        self.arecord_cmd = arecord_cmd or f"arecord -f S16_LE -r {sample_rate} -c 1 -t raw"

    def capture_utterance(self, timeout_s: float = 10.0) -> UtteranceAudio:
        vad = RmsVadUtteranceCapturer(self.cfg)

        block_n = int(self.cfg.sample_rate * (self.cfg.block_ms / 1000.0))
        block_bytes = block_n * 2  # int16

        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(
            self.host,
            username=self.username,
            password=self.password,
            timeout=5,
        )

        stdout = None
        channel = None

        try:
            stdin, stdout, stderr = client.exec_command(self.arecord_cmd)
            channel = stdout.channel
            channel.settimeout(0.5)

            buf = bytearray()

            def get_block(timeout: float) -> Optional[np.ndarray]:
                nonlocal buf
                # timeout wordt primair geregeld door channel.settimeout; dit argument houden we voor compat.
                try:
                    chunk = channel.recv(block_bytes)
                except socket.timeout:
                    return None

                if not chunk:
                    return None

                buf.extend(chunk)

                # Lever vaste blokgrootte
                need = block_bytes
                if len(buf) < need:
                    return None

                out = bytes(buf[:need])
                del buf[:need]

                # int16-boundary
                if len(out) % 2 == 1:
                    out = out[:-1]
                    if not out:
                        return None

                return np.frombuffer(out, dtype=np.int16)

            audio_int16 = vad.capture(get_block=get_block, timeout_s=timeout_s)

        finally:
            # Sluit kanaal zodat remote arecord stopt (SIGPIPE)
            try:
                if channel is not None:
                    channel.close()
            except Exception:
                pass
            try:
                client.close()
            except Exception:
                pass

        wav_bytes = int16_to_wav_bytes(audio_int16, self.cfg.sample_rate)

        return UtteranceAudio(
            pcm=wav_bytes,
            sample_rate=self.cfg.sample_rate,
            channels=1,
            sample_width=2,
        )

    def record_until(self, stop_event, *, max_duration_s: Optional[float] = None) -> UtteranceAudio:
        """
        Neem raw audio op tot stop_event gezet wordt (zonder VAD).
        """
        block_n = int(self.cfg.sample_rate * (self.cfg.block_ms / 1000.0))
        block_bytes = block_n * 2  # int16
        max_samples = None if max_duration_s is None else int(self.cfg.sample_rate * float(max_duration_s))

        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(
            self.host,
            username=self.username,
            password=self.password,
            timeout=5,
        )

        stdout = None
        channel = None
        buf = bytearray()
        chunks: list[np.ndarray] = []
        total = 0

        try:
            stdin, stdout, stderr = client.exec_command(self.arecord_cmd)
            channel = stdout.channel
            channel.settimeout(0.5)

            while not stop_event.is_set():
                try:
                    chunk = channel.recv(block_bytes)
                except socket.timeout:
                    continue
                if not chunk:
                    continue
                buf.extend(chunk)
                if len(buf) < block_bytes:
                    continue
                out = bytes(buf[:block_bytes])
                del buf[:block_bytes]
                if len(out) % 2 == 1:
                    out = out[:-1]
                    if not out:
                        continue
                block = np.frombuffer(out, dtype=np.int16)
                chunks.append(block)
                total += block.size
                if max_samples is not None and total >= max_samples:
                    break
        finally:
            try:
                if channel is not None:
                    channel.close()
            except Exception:
                pass
            try:
                client.close()
            except Exception:
                pass

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
