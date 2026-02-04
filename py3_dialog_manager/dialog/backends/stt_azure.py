# py3_nao_behavior_manager/dialog/backends/stt_azure.py
import io
import json
import os
import wave
from typing import Optional, Tuple

import numpy as np

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

    audio = np.frombuffer(frames, dtype=np.int16)
    if num_channels > 1:
        audio = audio.reshape(-1, num_channels).mean(axis=1).astype(np.int16)

    return audio.tobytes(), sample_rate


class AzureSTTBackend(STTBackend):
    """
    STT-backend op basis van Azure Speech SDK.

    Config (via JSON -> params):
      - subscription_key: str | null       (default: env AZURE_SPEECH_KEY)
      - region: str | null                 (default: env AZURE_SPEECH_REGION)
      - language: str                      (default: nl-NL)
      - output_format: "simple" | "detailed"  (default: simple)
      - profanity: "masked" | "removed" | "raw" | null
      - endpoint_id: str | null            (Custom Speech endpoint)
    """

    def __init__(
        self,
        subscription_key: Optional[str] = None,
        region: Optional[str] = None,
        language: str = "nl-NL",
        *,
        output_format: str = "detailed",
        profanity: Optional[str] = None,
        endpoint_id: Optional[str] = None,
        initial_silence_ms: Optional[int] = None,
        end_silence_ms: Optional[int] = None,
    ) -> None:
        self.subscription_key = subscription_key or os.environ.get("AZURE_SPEECH_KEY")
        self.region = region or os.environ.get("AZURE_SPEECH_REGION")
        self.language = language
        self.output_format = (output_format or "simple").lower()
        self.profanity = profanity
        self.endpoint_id = endpoint_id
        self.initial_silence_ms = None if initial_silence_ms is None else int(initial_silence_ms)
        self.end_silence_ms = None if end_silence_ms is None else int(end_silence_ms)

        if not self.subscription_key or not self.region:
            raise RuntimeError("Azure STT mist AZURE_SPEECH_KEY of AZURE_SPEECH_REGION.")

    def _make_speech_config(self):
        import azure.cognitiveservices.speech as speechsdk

        speech_config = speechsdk.SpeechConfig(subscription=self.subscription_key, region=self.region)
        speech_config.speech_recognition_language = self.language

        fmt = self.output_format
        if fmt == "detailed":
            speech_config.output_format = speechsdk.OutputFormat.Detailed
        else:
            speech_config.output_format = speechsdk.OutputFormat.Simple

        if self.endpoint_id:
            speech_config.endpoint_id = self.endpoint_id

        if self.profanity:
            try:
                speech_config.set_profanity(speechsdk.ProfanityOption[self.profanity.upper()])
            except Exception:
                pass

        if self.initial_silence_ms is not None:
            try:
                speech_config.set_property(
                    speechsdk.PropertyId.SpeechServiceConnection_InitialSilenceTimeoutMs,
                    str(self.initial_silence_ms),
                )
            except Exception:
                pass

        if self.end_silence_ms is not None:
            try:
                speech_config.set_property(
                    speechsdk.PropertyId.SpeechServiceConnection_EndSilenceTimeoutMs,
                    str(self.end_silence_ms),
                )
            except Exception:
                pass

        return speech_config, speechsdk

    def transcribe(self, audio: UtteranceAudio) -> STTResult:
        pcm_bytes, sample_rate = _wav_bytes_to_pcm16_mono(audio.pcm)

        speech_config, speechsdk = self._make_speech_config()
        stream_format = speechsdk.audio.AudioStreamFormat(
            samples_per_second=sample_rate,
            bits_per_sample=16,
            channels=1,
        )
        push_stream = speechsdk.audio.PushAudioInputStream(stream_format)
        push_stream.write(pcm_bytes)
        push_stream.close()

        audio_config = speechsdk.audio.AudioConfig(stream=push_stream)
        recognizer = speechsdk.SpeechRecognizer(speech_config=speech_config, audio_config=audio_config)

        result = recognizer.recognize_once()

        if result.reason == speechsdk.ResultReason.RecognizedSpeech:
            text = result.text or ""
        elif result.reason == speechsdk.ResultReason.NoMatch:
            text = ""
        elif result.reason == speechsdk.ResultReason.Canceled:
            details = speechsdk.CancellationDetails(result)
            raise RuntimeError(f"Azure STT canceled: {details.reason} ({details.error_details})")
        else:
            raise RuntimeError(f"Azure STT failed: {result.reason}")

        confidence = None
        if self.output_format == "detailed" and getattr(result, "json", None):
            try:
                payload = json.loads(result.json)
                nbest = (payload.get("NBest") or [])
                if nbest and isinstance(nbest[0], dict):
                    confidence = nbest[0].get("Confidence")
            except Exception:
                confidence = None

        return STTResult(text=text.strip(), language=self.language, confidence=confidence)
