from __future__ import annotations

from types import SimpleNamespace

from dialog.interfaces import LLMResult

import pytest
import webapp_server
from tests.perf_utils import collect_latency_ms, default_perf_controls, perf_env_float, record_perf_metric


class StubLLMBackend:
    def generate(self, messages):
        return LLMResult(reply="ok", messages=list(messages))


class StubSTTBackend:
    def transcribe(self, _audio):  # pragma: no cover
        raise AssertionError("STT not used in perf tests")


def _make_app(monkeypatch):
    base_pipeline = SimpleNamespace(
        llm=StubLLMBackend(),
        output=None,
        status_to_console=True,
        system_prompt="SYSTEM",
        log_messages_path=None,
        log_meta={},
        max_history_turns=None,
    )
    monkeypatch.setattr(webapp_server, "build_pipeline_from_config", lambda *_a, **_k: base_pipeline)
    monkeypatch.setattr(webapp_server, "make_stt_backend_from_config", lambda *_a, **_k: StubSTTBackend())
    app, _, _ = webapp_server.create_app(cfg={}, config_path="<memory>")
    return app


@pytest.mark.perf
def test_send_api_latency_budget(monkeypatch):
    app = _make_app(monkeypatch)
    client = app.test_client()

    warmup, iterations = default_perf_controls()
    p95_budget_ms = perf_env_float(key="DM_PERF_SEND_P95_MS", default=120.0)

    def _send():
        resp = client.post("/api/send", json={"text": "hello", "emit": "none"})
        assert resp.status_code == 200
        assert resp.get_json()["ok"] is True

    samples = collect_latency_ms(_send, warmup=warmup, iterations=iterations)
    stats = record_perf_metric(
        metric="api_send_emit_none",
        samples=samples,
        budget_ms=p95_budget_ms,
        extra={"endpoint": "/api/send", "scenario": "emit_none"},
    )
    p95 = float(stats["p95_ms"])
    assert p95 <= p95_budget_ms, f"/api/send p95={p95:.2f}ms > {p95_budget_ms:.2f}ms"


@pytest.mark.perf
def test_dm_events_api_latency_budget(monkeypatch):
    app = _make_app(monkeypatch)
    client = app.test_client()

    warmup, iterations = default_perf_controls()
    p95_budget_ms = perf_env_float(key="DM_PERF_DM_EVENTS_P95_MS", default=120.0)

    # Seed event stream so endpoint performance reflects realistic payload shaping.
    for _ in range(25):
        seeded = client.post("/api/send", json={"text": "seed", "emit": "none"})
        assert seeded.status_code == 200

    def _fetch():
        resp = client.get("/api/dm_events?limit=400")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert "events" in data
        assert "meta" in data

    samples = collect_latency_ms(_fetch, warmup=warmup, iterations=iterations)
    stats = record_perf_metric(
        metric="api_dm_events_limit_400",
        samples=samples,
        budget_ms=p95_budget_ms,
        extra={"endpoint": "/api/dm_events", "limit": 400},
    )
    p95 = float(stats["p95_ms"])
    assert p95 <= p95_budget_ms, f"/api/dm_events p95={p95:.2f}ms > {p95_budget_ms:.2f}ms"
