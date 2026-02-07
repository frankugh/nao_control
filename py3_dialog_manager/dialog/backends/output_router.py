from __future__ import annotations

import os
import subprocess
import io
import tempfile
import wave
import time
from typing import Optional

import numpy as np
import requests

try:
    import sounddevice as sd
except Exception:  # pragma: no cover
    sd = None

from dialog.backends.output_console import ConsoleOutputBackend
from dialog.backends.output_nao import NaoTTSOutputBackend
from dialog.backends.output_none import NoOpOutputBackend
from dialog.interfaces import OutputBackend
from dialog.nao_api_router import NaoApiRouter


class OutputRouterBackend(OutputBackend):
    """
    Routeer output op basis van target/engine.
    Voor nu: NAO native werkt; server/client audio volgen later.
    """

    def __init__(
        self,
        *,
        target: str = "nao",
        tts_engine: str = "nao_native",
        output_device: Optional[int] = None,
        piper_model_path: Optional[str] = None,
        piper_length_scale: Optional[float] = None,
        piper_noise_scale: Optional[float] = None,
        piper_noise_w_scale: Optional[float] = None,
        piper_sentence_silence: Optional[float] = None,
        piper_volume: Optional[float] = None,
        piper_tail_silence_ms: Optional[int] = None,
        azure_tts_voice: Optional[str] = None,
        azure_voice: Optional[str] = None,
        azure_tts_rate: Optional[float] = None,
        azure_tts_pitch: Optional[float] = None,
        azure_tts_volume_db: Optional[float] = None,
        piper_bin: Optional[str] = None,
        api_router: Optional[NaoApiRouter] = None,
        timeout: Optional[float] = 5.0,
    ) -> None:
        self.target = (target or "nao").lower()
        self.tts_engine = (tts_engine or "nao_native").lower()
        self.output_device = output_device
        self.piper_model_path = piper_model_path
        self.piper_length_scale = None if piper_length_scale is None else float(piper_length_scale)
        self.piper_noise_scale = None if piper_noise_scale is None else float(piper_noise_scale)
        self.piper_noise_w_scale = None if piper_noise_w_scale is None else float(piper_noise_w_scale)
        self.piper_sentence_silence = None if piper_sentence_silence is None else float(piper_sentence_silence)
        self.piper_volume = None if piper_volume is None else float(piper_volume)
        self.piper_tail_silence_ms = (
            int(piper_tail_silence_ms) if piper_tail_silence_ms is not None else 500
        )
        selected_voice = azure_tts_voice if azure_tts_voice is not None else azure_voice
        self.azure_voice = (selected_voice or "").strip() or None
        self.azure_rate_pct = None if azure_tts_rate is None else float(azure_tts_rate)
        self.azure_pitch_st = None if azure_tts_pitch is None else float(azure_tts_pitch)
        self.azure_volume_db = None if azure_tts_volume_db is None else float(azure_tts_volume_db)
        self.piper_bin = piper_bin or "piper"
        self._no_op = NoOpOutputBackend()
        self._console = ConsoleOutputBackend()
        self._nao = NaoTTSOutputBackend(api_router=api_router, timeout=timeout or 5.0)
        self._api_router = api_router
        self._timeout = float(timeout) if timeout else 5.0
        self._warned = False
        self._stream_failures = 0
        self._stream_cooldown_until = 0.0
        self._stream_fail_threshold = 2
        self._stream_cooldown_s = 300.0

    def _warn(self, msg: str) -> None:
        self._console.emit(msg)

    def _wav_bytes_to_int16(self, wav_bytes: bytes) -> tuple[np.ndarray, int]:
        with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
            sample_rate = wf.getframerate()
            num_channels = wf.getnchannels()
            sample_width = wf.getsampwidth()
            frames = wf.readframes(wf.getnframes())

        if sample_width != 2:
            raise ValueError("Verwacht 16-bit WAV.")

        audio = np.frombuffer(frames, dtype=np.int16)
        if num_channels > 1:
            audio = audio.reshape(-1, num_channels).mean(axis=1).astype(np.int16)
        return audio, sample_rate

    def _append_wav_silence(self, wav_bytes: bytes, *, ms: int) -> bytes:
        if ms <= 0:
            return wav_bytes
        with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
            params = wf.getparams()
            frames = wf.readframes(wf.getnframes())
        sample_rate = params.framerate
        num_channels = params.nchannels
        sample_width = params.sampwidth
        pad_frames = int(sample_rate * (ms / 1000.0))
        pad_bytes = b"\x00" * (pad_frames * num_channels * sample_width)
        out = io.BytesIO()
        with wave.open(out, "wb") as wf_out:
            wf_out.setparams(params)
            wf_out.writeframes(frames + pad_bytes)
        return out.getvalue()

    def _ensure_terminal_punct(self, text: str) -> str:
        trimmed = text.rstrip()
        if not trimmed:
            return text
        if trimmed.endswith((".", "!", "?", "…")):
            return text
        return trimmed + "."

    def _play_wav_bytes(self, wav_bytes: bytes) -> None:
        if sd is None:
            self._warn("[output] sounddevice niet beschikbaar; kan niet afspelen.")
            return
        audio, sample_rate = self._wav_bytes_to_int16(wav_bytes)
        sd.play(audio, samplerate=sample_rate, device=self.output_device)
        sd.wait()

    def _send_wav_to_nao(self, wav_bytes: bytes, filename: str = "piper.wav") -> None:
        if self._api_router is None:
            self._warn("[output] NAO router ontbreekt; kan audio niet sturen.")
            return
        files = {"file": (filename, wav_bytes, "audio/wav")}
        data = {"filename": filename}
        try:
            resp = self._api_router.post("/play_audio", files=files, data=data, timeout=self._timeout)
        except requests.RequestException as exc:
            self._warn(f"[output] NAO play_audio failed: {exc}")
            return
        if resp.status_code >= 400:
            self._warn(f"[output] NAO play_audio returned {resp.status_code}")

    def _try_stream_to_nao(self, wav_bytes: bytes) -> bool:
        if self._api_router is None:
            self._warn("[output] NAO router ontbreekt; kan audio niet streamen.")
            return False
        now = time.time()
        if now < self._stream_cooldown_until:
            return False
        try:
            audio, sample_rate = self._wav_bytes_to_int16(wav_bytes)
        except Exception as exc:
            self._warn(f"[output] WAV->PCM faalde: {exc}")
            return False
        try:
            resp = self._api_router.post(
                "/play_stream",
                data=audio.tobytes(),
                headers={"Content-Type": "application/octet-stream"},
                params={"sample_rate": int(sample_rate)},
                timeout=self._timeout,
            )
        except requests.RequestException as exc:
            self._stream_failures += 1
            if self._stream_failures >= self._stream_fail_threshold:
                self._stream_cooldown_until = now + self._stream_cooldown_s
                self._warn("[output] Stream faalde; tijdelijk terug naar upload (cooldown).")
            self._warn(f"[output] NAO play_stream failed: {exc}")
            return False
        if resp.status_code >= 400:
            self._stream_failures += 1
            if self._stream_failures >= self._stream_fail_threshold:
                self._stream_cooldown_until = now + self._stream_cooldown_s
                self._warn("[output] Stream faalde; tijdelijk terug naar upload (cooldown).")
            self._warn(f"[output] NAO play_stream returned {resp.status_code}")
            return False
        self._stream_failures = 0
        self._stream_cooldown_until = 0.0
        return True

    def _synthesize_piper(self, text: str) -> Optional[bytes]:
        if not self.piper_model_path:
            self._warn("[output] Piper model ontbreekt.")
            return None
        model_path = self.piper_model_path
        if not os.path.exists(model_path):
            self._warn(f"[output] Piper model niet gevonden: {model_path}")
            return None
        with tempfile.NamedTemporaryFile(delete=False, suffix=".wav") as tmp:
            out_path = tmp.name
        text = self._ensure_terminal_punct(text)
        cmd = [self.piper_bin, "-m", model_path, "-f", out_path]
        if self.piper_length_scale is not None:
            cmd += ["--length-scale", str(self.piper_length_scale)]
        if self.piper_noise_scale is not None:
            cmd += ["--noise-scale", str(self.piper_noise_scale)]
        if self.piper_noise_w_scale is not None:
            cmd += ["--noise-w-scale", str(self.piper_noise_w_scale)]
        if self.piper_sentence_silence is not None:
            cmd += ["--sentence-silence", str(self.piper_sentence_silence)]
        if self.piper_volume is not None:
            cmd += ["--volume", str(self.piper_volume)]
        cmd.append(text)
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
        except OSError as exc:
            self._warn(f"[output] Piper binary niet gevonden: {exc}")
            return None
        if proc.returncode != 0:
            msg = (proc.stderr or proc.stdout or "").strip()
            self._warn(f"[output] Piper failed ({proc.returncode}): {msg}")
            return None
        try:
            with open(out_path, "rb") as handle:
                wav_bytes = handle.read()
            if self.piper_tail_silence_ms and self.piper_tail_silence_ms > 0:
                wav_bytes = self._append_wav_silence(wav_bytes, ms=self.piper_tail_silence_ms)
            return wav_bytes
        finally:
            try:
                os.remove(out_path)
            except OSError:
                pass

    def _synthesize_azure(self, text: str) -> Optional[bytes]:
        key = os.environ.get("AZURE_SPEECH_KEY") or os.environ.get("AZURE_TTS_KEY")
        region = os.environ.get("AZURE_SPEECH_REGION") or os.environ.get("AZURE_TTS_REGION")
        if not key or not region:
            self._warn("[output] Azure TTS mist AZURE_SPEECH_KEY/REGION.")
            return None
        try:
            import azure.cognitiveservices.speech as speechsdk
        except Exception as exc:
            self._warn(f"[output] Azure SDK import faalde: {exc}")
            return None
        try:
            from xml.sax.saxutils import escape as _xml_escape
        except Exception:
            _xml_escape = None
        speech_config = speechsdk.SpeechConfig(subscription=key, region=region)
        if self.azure_voice:
            speech_config.speech_synthesis_voice_name = self.azure_voice
        try:
            speech_config.set_speech_synthesis_output_format(
                speechsdk.SpeechSynthesisOutputFormat.Riff16Khz16BitMonoPcm
            )
        except Exception:
            pass
        synthesizer = speechsdk.SpeechSynthesizer(speech_config=speech_config, audio_config=None)
        use_ssml = any(
            v is not None for v in (self.azure_rate_pct, self.azure_pitch_st, self.azure_volume_db)
        )
        if use_ssml or self.azure_voice:
            safe_text = _xml_escape(text) if _xml_escape else text
            locale = "en-US"
            if self.azure_voice:
                parts = self.azure_voice.split("-")
                if len(parts) >= 2:
                    locale = f"{parts[0]}-{parts[1]}"
            prosody_attrs = []
            if self.azure_rate_pct is not None and abs(self.azure_rate_pct - 100.0) > 1e-6:
                prosody_attrs.append(f'rate="{self.azure_rate_pct:.0f}%"')
            if self.azure_pitch_st is not None and abs(self.azure_pitch_st) > 1e-6:
                sign = "+" if self.azure_pitch_st > 0 else ""
                prosody_attrs.append(f'pitch="{sign}{self.azure_pitch_st:.1f}st"')
            if self.azure_volume_db is not None and abs(self.azure_volume_db) > 1e-6:
                sign = "+" if self.azure_volume_db > 0 else ""
                prosody_attrs.append(f'volume="{sign}{self.azure_volume_db:.0f}dB"')
            inner = safe_text
            if prosody_attrs:
                inner = f"<prosody {' '.join(prosody_attrs)}>{safe_text}</prosody>"
            if self.azure_voice:
                inner = f"<voice name=\"{self.azure_voice}\">{inner}</voice>"
            ssml = f"<speak version=\"1.0\" xml:lang=\"{locale}\">{inner}</speak>"
            result = synthesizer.speak_ssml_async(ssml).get()
        else:
            result = synthesizer.speak_text_async(text).get()
        if result.reason == speechsdk.ResultReason.SynthesizingAudioCompleted:
            return bytes(result.audio_data)
        if result.reason == speechsdk.ResultReason.Canceled:
            try:
                details = speechsdk.SpeechSynthesisCancellationDetails(result)
                self._warn(f"[output] Azure TTS canceled: {details.reason} ({details.error_details})")
            except Exception:
                self._warn("[output] Azure TTS canceled.")
            return None
        self._warn(f"[output] Azure TTS failed: {result.reason}")
        return None

    def emit(self, text: str) -> None:
        if self.target == "none" or self.tts_engine == "none":
            return self._no_op.emit(text)

        if self.target == "nao" and self.tts_engine in ("nao_native", "nao"):
            return self._nao.emit(text)

        if self.tts_engine == "piper":
            wav_bytes = self._synthesize_piper(text)
            if not wav_bytes:
                return
            if self.target == "server":
                return self._play_wav_bytes(wav_bytes)
            if self.target == "nao":
                if self._try_stream_to_nao(wav_bytes):
                    return
                return self._send_wav_to_nao(wav_bytes)

        if self.tts_engine == "azure":
            wav_bytes = self._synthesize_azure(text)
            if not wav_bytes:
                return
            if self.target == "server":
                return self._play_wav_bytes(wav_bytes)
            if self.target == "nao":
                if self._try_stream_to_nao(wav_bytes):
                    return
                return self._send_wav_to_nao(wav_bytes)

        if not self._warned:
            self._warned = True
            self._console.emit(
                f"[output] target={self.target} tts_engine={self.tts_engine} nog niet ondersteund."
            )
        return self._console.emit(text)
