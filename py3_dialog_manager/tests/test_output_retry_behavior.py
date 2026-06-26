from __future__ import annotations

import io
import wave
from pathlib import Path

import numpy as np
import requests

from dialog.backends import output_router as output_router_module
from dialog.backends.output_router import OutputRouterBackend
from dialog.nao_api_router import NaoApiRouter


def _resp(status: int) -> requests.Response:
    r = requests.Response()
    r.status_code = status
    r._content = b"{}"
    return r


def test_output_router_warn_redacts_secret_header_values(monkeypatch):
    backend = OutputRouterBackend(target="none", tts_engine="none")
    emitted: list[str] = []
    monkeypatch.setattr(backend._console, "emit", emitted.append)

    backend._warn("[output] Azure TTS canceled: Error (header value: '\\nsecret-azure-key')")

    assert "secret-azure-key" not in backend.last_error_message()
    assert "secret-azure-key" not in emitted[0]
    assert "<redacted>" in emitted[0]


def test_nao_router_no_fallback_on_side_effect_timeout(monkeypatch):
    router = NaoApiRouter(
        primary_base_url="http://primary",
        fallback_base_url="http://fallback",
        health_checks=[],
        timeout_s=1.0,
        status_to_console=False,
    )
    calls: list[str] = []

    def fake_request(base_url, method, path, *, timeout, **kwargs):
        calls.append(base_url + path)
        if base_url == "http://primary":
            raise requests.Timeout("late timeout")
        return _resp(200)

    monkeypatch.setattr(router, "_request", fake_request)

    try:
        router.post("/play_stream", data=b"\x00")
        assert False, "Expected timeout"
    except requests.Timeout:
        pass

    assert calls == ["http://primary/play_stream"]


def test_nao_router_no_fallback_on_side_effect_5xx(monkeypatch):
    router = NaoApiRouter(
        primary_base_url="http://primary",
        fallback_base_url="http://fallback",
        health_checks=[],
        timeout_s=1.0,
        status_to_console=False,
    )
    calls: list[str] = []

    def fake_request(base_url, method, path, *, timeout, **kwargs):
        calls.append(base_url + path)
        if base_url == "http://primary":
            return _resp(500)
        return _resp(200)

    monkeypatch.setattr(router, "_request", fake_request)
    r = router.post("/play_audio", files={"file": ("a.wav", b"x", "audio/wav")})
    assert r.status_code == 500
    assert calls == ["http://primary/play_audio"]


def test_nao_router_keeps_fallback_for_non_side_effect(monkeypatch):
    router = NaoApiRouter(
        primary_base_url="http://primary",
        fallback_base_url="http://fallback",
        health_checks=[],
        timeout_s=1.0,
        status_to_console=False,
    )
    calls: list[str] = []

    def fake_request(base_url, method, path, *, timeout, **kwargs):
        calls.append(base_url + path)
        if base_url == "http://primary":
            raise requests.ConnectionError("down")
        return _resp(200)

    monkeypatch.setattr(router, "_request", fake_request)
    r = router.post("/set_eye_color", json={"color": "#00ff00", "duration": 0.2})
    assert r.status_code == 200
    assert calls == ["http://primary/set_eye_color", "http://fallback/set_eye_color"]


def test_nao_router_logs_retry_for_same_endpoint(monkeypatch, capsys):
    router = NaoApiRouter(
        primary_base_url="http://same",
        fallback_base_url="http://same",
        health_checks=[],
        timeout_s=1.0,
        status_to_console=True,
    )
    calls: list[str] = []

    def fake_request(base_url, method, path, *, timeout, **kwargs):
        calls.append(base_url + path)
        if len(calls) == 1:
            raise requests.ConnectionError("down")
        return _resp(200)

    monkeypatch.setattr(router, "_request", fake_request)
    r = router.post("/set_eye_color", json={"color": "#00ff00", "duration": 0.2})

    captured = capsys.readouterr()
    assert r.status_code == 200
    assert calls == ["http://same/set_eye_color", "http://same/set_eye_color"]
    assert "retrying same endpoint" in captured.out
    assert "using fallback" not in captured.out


class _FakeApiRouter:
    def __init__(self, stream_response: requests.Response | Exception):
        self.stream_response = stream_response
        self.calls: list[str] = []
        self.timeouts: list[float | None] = []

    def post(self, path: str, **kwargs):
        self.calls.append(path)
        self.timeouts.append(kwargs.get("timeout"))
        if path == "/play_stream":
            if isinstance(self.stream_response, Exception):
                raise self.stream_response
            return self.stream_response
        if path == "/play_audio":
            return _resp(200)
        return _resp(200)


def _make_backend(fake_router: _FakeApiRouter) -> OutputRouterBackend:
    backend = OutputRouterBackend(
        target="nao",
        tts_engine="piper",
        piper_model_path="unused",
        api_router=fake_router,
        timeout=1.0,
    )
    backend._synthesize_piper = lambda text: b"WAV"  # type: ignore[method-assign]
    backend._wav_bytes_to_int16 = lambda wav_bytes: (np.zeros(160, dtype=np.int16), 16000)  # type: ignore[method-assign]
    return backend


def _make_wav(samples: np.ndarray, sample_rate: int = 16000) -> bytes:
    out = io.BytesIO()
    with wave.open(out, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(samples.astype(np.int16).tobytes())
    return out.getvalue()


def test_output_router_does_not_upload_after_stream_timeout():
    fake_router = _FakeApiRouter(requests.Timeout("late timeout"))
    backend = _make_backend(fake_router)

    backend.emit("lang bericht")

    assert fake_router.calls == ["/play_stream"]


def test_output_router_falls_back_to_upload_when_stream_unsupported():
    fake_router = _FakeApiRouter(_resp(404))
    backend = _make_backend(fake_router)

    backend.emit("kort bericht")

    assert fake_router.calls == ["/play_stream", "/play_audio"]


def test_output_router_stream_timeout_scales_with_audio_duration():
    fake_router = _FakeApiRouter(_resp(200))
    backend = OutputRouterBackend(
        target="nao",
        tts_engine="piper",
        piper_model_path="unused",
        api_router=fake_router,
    )
    backend._wav_bytes_to_int16 = lambda wav_bytes: (np.zeros(160000, dtype=np.int16), 16000)  # type: ignore[method-assign]

    ok = backend.emit_preloaded_wav_bytes(_make_wav(np.zeros(160000, dtype=np.int16)))

    assert ok is True
    assert fake_router.calls == ["/play_stream"]
    assert fake_router.timeouts == [15.0]


def test_output_router_prepends_lead_silence_for_server_tts():
    backend = OutputRouterBackend(
        target="server",
        tts_engine="azure",
        server_tts_lead_silence_ms=300,
    )
    raw_samples = np.full(1600, 1000, dtype=np.int16)
    raw_wav = _make_wav(raw_samples)
    played: dict[str, bytes] = {}

    backend._synthesize_azure = lambda text: raw_wav  # type: ignore[method-assign]
    backend._play_wav_bytes = lambda wav_bytes: played.setdefault("wav", wav_bytes)  # type: ignore[method-assign]

    backend.emit("hoi")

    assert "wav" in played
    with wave.open(io.BytesIO(played["wav"]), "rb") as wf:
        frames = wf.readframes(wf.getnframes())
        sample_rate = wf.getframerate()
    audio = np.frombuffer(frames, dtype=np.int16)
    lead_samples = int(sample_rate * 0.3)
    assert audio.size == lead_samples + raw_samples.size
    assert np.all(audio[:lead_samples] == 0)
    assert np.array_equal(audio[lead_samples:], raw_samples)


def test_output_router_server_playback_resamples_to_output_device_rate(monkeypatch):
    class FakeSoundDevice:
        def __init__(self) -> None:
            self.played = None

        def query_devices(self, device, kind):
            assert device is None
            assert kind == "output"
            return {"default_samplerate": 48000}

        def play(self, audio, *, samplerate, device):
            self.played = (audio, samplerate, device)

        def wait(self):
            pass

    fake_sd = FakeSoundDevice()
    monkeypatch.setattr(output_router_module, "sd", fake_sd)
    backend = OutputRouterBackend(target="server", tts_engine="azure")
    raw_wav = _make_wav(np.full(160, 1000, dtype=np.int16), sample_rate=16000)

    ok = backend._play_wav_bytes(raw_wav)

    assert ok is True
    assert fake_sd.played is not None
    audio, sample_rate, device = fake_sd.played
    assert sample_rate == 48000
    assert device is None
    assert audio.size == 480


def test_output_router_server_playback_fades_edges(monkeypatch):
    class FakeSoundDevice:
        def query_devices(self, device, kind):
            return {"default_samplerate": 16000}

        def play(self, audio, *, samplerate, device):
            self.audio = audio

        def wait(self):
            pass

    fake_sd = FakeSoundDevice()
    monkeypatch.setattr(output_router_module, "sd", fake_sd)
    backend = OutputRouterBackend(target="server", tts_engine="azure")
    raw_wav = _make_wav(np.full(1600, 12000, dtype=np.int16), sample_rate=16000)

    assert backend._play_wav_bytes(raw_wav) is True
    assert fake_sd.audio[0] == 0
    assert fake_sd.audio[-1] == 0
    assert fake_sd.audio[120] == 12000


def test_output_router_describes_renderable_profile_without_target_in_fingerprint():
    backend = OutputRouterBackend(
        target="server",
        tts_engine="azure",
        azure_tts_voice="nl-NL-ColetteNeural",
        azure_tts_rate=95,
    )

    profile = backend.describe_tts_profile()

    assert profile["supported"] is True
    assert profile["engine"] == "azure"
    assert profile["fingerprint"]
    assert "Colette" in profile["summary"]


def test_output_router_emit_preloaded_wav_bytes_uses_server_playback(monkeypatch):
    backend = OutputRouterBackend(
        target="server",
        tts_engine="azure",
        server_tts_lead_silence_ms=250,
    )
    raw_wav = _make_wav(np.full(800, 200, dtype=np.int16))
    played: dict[str, bytes] = {}
    monkeypatch.setattr(backend, "_play_wav_bytes", lambda wav_bytes: played.setdefault("wav", wav_bytes))

    ok = backend.emit_preloaded_wav_bytes(raw_wav, filename="demo.wav")

    assert ok is True
    assert "wav" in played


def test_output_router_resolves_piper_from_python_sibling_when_path_missing(monkeypatch, tmp_path):
    scripts_dir = tmp_path / "Scripts"
    scripts_dir.mkdir()
    python_exe = scripts_dir / "python.exe"
    python_exe.write_bytes(b"")
    piper_exe = scripts_dir / "piper.exe"
    piper_exe.write_bytes(b"")

    monkeypatch.setattr(output_router_module.shutil, "which", lambda name: None)
    monkeypatch.setattr(output_router_module.sys, "executable", str(python_exe))

    backend = OutputRouterBackend(target="server", tts_engine="piper", piper_model_path="model.onnx")

    assert Path(backend.piper_bin) == piper_exe
