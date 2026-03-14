from __future__ import annotations

import io
import wave

import numpy as np
import requests

from dialog.backends.output_router import OutputRouterBackend
from dialog.nao_api_router import NaoApiRouter


def _resp(status: int) -> requests.Response:
    r = requests.Response()
    r.status_code = status
    r._content = b"{}"
    return r


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


class _FakeApiRouter:
    def __init__(self, stream_response: requests.Response | Exception):
        self.stream_response = stream_response
        self.calls: list[str] = []

    def post(self, path: str, **kwargs):
        self.calls.append(path)
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
