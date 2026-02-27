from __future__ import annotations

import time
from types import SimpleNamespace

from dialog.interfaces import CommandDecision, LLMResult, RouteDecision, STTResult

import pytest
import webapp_server
from tests.perf_utils import collect_latency_ms, default_perf_controls, perf_env_float, record_perf_metric


_FAKE_WAV_BYTES = b"RIFF\x24\x00\x00\x00WAVEfmt "


def _sleep_ms(delay_ms: float) -> None:
    if delay_ms > 0.0:
        time.sleep(delay_ms / 1000.0)


class TimedLLMBackend:
    def __init__(self, *, delay_ms: float) -> None:
        self.delay_ms = max(0.0, float(delay_ms))
        self.calls = 0

    def generate(self, messages):
        self.calls += 1
        _sleep_ms(self.delay_ms)
        return LLMResult(reply="mocked-reasoning-reply", messages=list(messages))


class TimedSTTBackend:
    def __init__(self, *, delay_ms: float) -> None:
        self.delay_ms = max(0.0, float(delay_ms))
        self.calls = 0

    def transcribe(self, _audio):
        self.calls += 1
        _sleep_ms(self.delay_ms)
        return STTResult(text="zet lamp aan", language="nl", confidence=0.99)


class TimedCmdRec:
    def __init__(
        self,
        *,
        delay_ms: float,
        route_map: dict[str, RouteDecision] | None = None,
        dance_catalog: list[dict[str, str]] | None = None,
    ) -> None:
        self.delay_ms = max(0.0, float(delay_ms))
        self.calls = 0
        self.bundle_path = None
        self._route_map = {(k or "").strip().casefold(): v for (k, v) in (route_map or {}).items()}
        self._dance_catalog = list(dance_catalog or [])

    def route(self, text: str, _mode: str, _active_behavior):
        self.calls += 1
        _sleep_ms(self.delay_ms)
        key = (text or "").strip().casefold()
        if key in self._route_map:
            return self._route_map[key]
        return RouteDecision(
            is_command=False,
            command=None,
            reason="disabled",
            top3=[("NONE", 0.97), ("STOP", 0.03)],
        )

    def is_guarded(self, _label: str) -> bool:
        return False

    def get_dance_catalog(self):
        return list(self._dance_catalog)


class TimedBehaviorExecutor:
    def __init__(self, *, delay_ms: float) -> None:
        self.delay_ms = max(0.0, float(delay_ms))
        self.calls = 0
        self.labels: list[str] = []
        self.resolved: list[dict[str, str]] = []
        self.custom_life_enabled = False

    def set_custom_life_management_enabled(self, enabled: bool) -> None:
        self.custom_life_enabled = bool(enabled)

    def execute(self, cmd: CommandDecision) -> None:
        self.calls += 1
        self.labels.append(str(cmd.label))
        self.resolved.append(dict(cmd.resolved or {}))
        _sleep_ms(self.delay_ms)


def _make_app(
    monkeypatch,
    *,
    cmdrec: TimedCmdRec | None = None,
    behavior_executor: TimedBehaviorExecutor | None = None,
):
    llm_delay_ms = perf_env_float(key="DM_PERF_MOCK_LLM_MS", default=6.0)
    stt_delay_ms = perf_env_float(key="DM_PERF_MOCK_STT_MS", default=4.0)
    llm = TimedLLMBackend(delay_ms=llm_delay_ms)
    stt = TimedSTTBackend(delay_ms=stt_delay_ms)
    base_pipeline = SimpleNamespace(
        llm=llm,
        output=None,
        status_to_console=True,
        system_prompt="SYSTEM",
        log_messages_path=None,
        log_meta={},
        max_history_turns=None,
        _cmdrec=cmdrec,
        _debug_cmdrec=False,
        _behavior_executor=behavior_executor,
    )

    monkeypatch.setattr(webapp_server, "build_pipeline_from_config", lambda *_a, **_k: base_pipeline)
    monkeypatch.setattr(webapp_server, "make_stt_backend_from_config", lambda *_a, **_k: stt)
    app, _, _ = webapp_server.create_app(cfg={}, config_path="<memory>")
    return app, llm, stt, cmdrec, behavior_executor


def _prime_sid(client) -> None:
    # Ensure stable sid across requests in this test client.
    resp = client.get("/api/state")
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True


@pytest.mark.perf
def test_transcribe_api_latency_budget_with_mock_stt(monkeypatch):
    app, _llm, stt, _cmdrec, _executor = _make_app(monkeypatch, cmdrec=None, behavior_executor=None)
    client = app.test_client()
    _prime_sid(client)

    warmup, iterations = default_perf_controls()
    p95_budget_ms = perf_env_float(key="DM_PERF_TRANSCRIBE_P95_MS", default=120.0)

    def _transcribe():
        resp = client.post("/api/transcribe", data=_FAKE_WAV_BYTES, content_type="audio/wav")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert data["transcript"] == "zet lamp aan"

    samples = collect_latency_ms(_transcribe, warmup=warmup, iterations=iterations)
    stats = record_perf_metric(
        metric="api_transcribe_mock_stt",
        samples=samples,
        budget_ms=p95_budget_ms,
        extra={"endpoint": "/api/transcribe"},
    )
    p95 = float(stats["p95_ms"])
    expected_calls = warmup + iterations
    assert stt.calls >= expected_calls
    assert p95 <= p95_budget_ms, f"/api/transcribe p95={p95:.2f}ms > {p95_budget_ms:.2f}ms"


@pytest.mark.perf
def test_interaction_flow_latency_budget_with_cmdrec_and_reasoning(monkeypatch):
    cmdrec_delay_ms = perf_env_float(key="DM_PERF_MOCK_CMDREC_MS", default=1.0)
    cmdrec = TimedCmdRec(delay_ms=cmdrec_delay_ms)
    app, llm, stt, cmdrec, _executor = _make_app(monkeypatch, cmdrec=cmdrec, behavior_executor=None)
    client = app.test_client()
    _prime_sid(client)

    warmup, iterations = default_perf_controls()
    p95_budget_ms = perf_env_float(key="DM_PERF_INTERACTION_P95_MS", default=220.0)

    def _interaction_flow():
        transcribe_resp = client.post("/api/transcribe", data=_FAKE_WAV_BYTES, content_type="audio/wav")
        assert transcribe_resp.status_code == 200
        transcript = transcribe_resp.get_json().get("transcript") or "hallo"
        send_resp = client.post(
            "/api/send",
            json={
                "text": transcript,
                "emit": "none",
                "reset": True,
                "input_meta": {"stt_used": True, "stt_edited": False},
            },
        )
        assert send_resp.status_code == 200
        send_data = send_resp.get_json()
        assert send_data["ok"] is True
        assert send_data["reply"] == "mocked-reasoning-reply"

    samples = collect_latency_ms(_interaction_flow, warmup=warmup, iterations=iterations)
    stats = record_perf_metric(
        metric="interaction_transcribe_send_cmdrec_reasoning",
        samples=samples,
        budget_ms=p95_budget_ms,
        extra={"flow": "transcribe->send"},
    )
    p95 = float(stats["p95_ms"])
    expected_calls = warmup + iterations
    assert stt.calls >= expected_calls
    assert llm.calls >= expected_calls
    assert cmdrec is not None
    assert cmdrec.calls >= expected_calls
    assert p95 <= p95_budget_ms, f"transcribe->send p95={p95:.2f}ms > {p95_budget_ms:.2f}ms"


@pytest.mark.perf
def test_stop_command_flow_latency_budget(monkeypatch):
    cmdrec_delay_ms = perf_env_float(key="DM_PERF_MOCK_CMDREC_MS", default=1.0)
    executor_delay_ms = perf_env_float(key="DM_PERF_MOCK_EXECUTOR_MS", default=1.0)
    stop_decision = RouteDecision(
        is_command=True,
        command=CommandDecision(label="STOP", confidence=0.99, raw_text="stop"),
        reason="mock_rule",
        top3=[("STOP", 0.99)],
    )
    cmdrec = TimedCmdRec(delay_ms=cmdrec_delay_ms, route_map={"stop": stop_decision})
    executor = TimedBehaviorExecutor(delay_ms=executor_delay_ms)
    app, _llm, _stt, cmdrec, executor = _make_app(monkeypatch, cmdrec=cmdrec, behavior_executor=executor)
    client = app.test_client()
    _prime_sid(client)

    warmup, iterations = default_perf_controls()
    p95_budget_ms = perf_env_float(key="DM_PERF_STOP_P95_MS", default=220.0)

    def _stop_flow():
        resp = client.post("/api/send", json={"text": "stop", "emit": "none", "reset": True})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert data["reply"] == "OK. Uitgevoerd: STOP"

    samples = collect_latency_ms(_stop_flow, warmup=warmup, iterations=iterations)
    stats = record_perf_metric(
        metric="command_stop_flow",
        samples=samples,
        budget_ms=p95_budget_ms,
        extra={"endpoint": "/api/send", "command": "STOP"},
    )
    p95 = float(stats["p95_ms"])
    expected_calls = warmup + iterations
    assert cmdrec is not None
    assert executor is not None
    assert cmdrec.calls >= expected_calls
    assert executor.calls >= expected_calls
    assert p95 <= p95_budget_ms, f"stop-command p95={p95:.2f}ms > {p95_budget_ms:.2f}ms"


@pytest.mark.perf
def test_stand_up_command_flow_latency_budget(monkeypatch):
    cmdrec_delay_ms = perf_env_float(key="DM_PERF_MOCK_CMDREC_MS", default=1.0)
    executor_delay_ms = perf_env_float(key="DM_PERF_MOCK_EXECUTOR_MS", default=1.0)
    standup_decision = RouteDecision(
        is_command=True,
        command=CommandDecision(label="STAND_UP", confidence=0.98, raw_text="standup"),
        reason="mock_rule",
        top3=[("STAND_UP", 0.98)],
    )
    cmdrec = TimedCmdRec(delay_ms=cmdrec_delay_ms, route_map={"standup": standup_decision})
    executor = TimedBehaviorExecutor(delay_ms=executor_delay_ms)
    app, _llm, _stt, cmdrec, executor = _make_app(monkeypatch, cmdrec=cmdrec, behavior_executor=executor)
    client = app.test_client()
    _prime_sid(client)

    warmup, iterations = default_perf_controls()
    p95_budget_ms = perf_env_float(key="DM_PERF_STANDUP_P95_MS", default=220.0)

    def _standup_flow():
        resp = client.post("/api/send", json={"text": "standup", "emit": "none", "reset": True})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert data["reply"] == "OK. Uitgevoerd: STAND_UP"

    samples = collect_latency_ms(_standup_flow, warmup=warmup, iterations=iterations)
    stats = record_perf_metric(
        metric="command_stand_up_flow",
        samples=samples,
        budget_ms=p95_budget_ms,
        extra={"endpoint": "/api/send", "command": "STAND_UP"},
    )
    p95 = float(stats["p95_ms"])
    expected_calls = warmup + iterations
    assert cmdrec is not None
    assert executor is not None
    assert cmdrec.calls >= expected_calls
    assert executor.calls >= expected_calls
    assert all(label == "STAND_UP" for label in executor.labels[-min(5, len(executor.labels)) :])
    assert p95 <= p95_budget_ms, f"standup-command p95={p95:.2f}ms > {p95_budget_ms:.2f}ms"


@pytest.mark.perf
def test_dance_followup_happy_flow_latency_budget(monkeypatch):
    cmdrec_delay_ms = perf_env_float(key="DM_PERF_MOCK_CMDREC_MS", default=1.0)
    executor_delay_ms = perf_env_float(key="DM_PERF_MOCK_EXECUTOR_MS", default=1.0)
    dance_decision = RouteDecision(
        is_command=True,
        command=CommandDecision(label="DANCE", confidence=0.97, raw_text="doe een dans", resolved={}),
        reason="mock_rule",
        top3=[("DANCE", 0.97)],
    )
    dance_catalog = [
        {"key": "happy", "aliases": ["blij", "happy"], "behavior": "dances/happy"},
    ]
    cmdrec = TimedCmdRec(
        delay_ms=cmdrec_delay_ms,
        route_map={"doe een dans": dance_decision},
        dance_catalog=dance_catalog,
    )
    executor = TimedBehaviorExecutor(delay_ms=executor_delay_ms)
    app, _llm, _stt, cmdrec, executor = _make_app(monkeypatch, cmdrec=cmdrec, behavior_executor=executor)
    client = app.test_client()
    _prime_sid(client)

    warmup, iterations = default_perf_controls()
    p95_budget_ms = perf_env_float(key="DM_PERF_DANCE_HAPPY_P95_MS", default=320.0)

    def _dance_happy_flow():
        first = client.post("/api/send", json={"text": "doe een dans", "emit": "none", "reset": True})
        assert first.status_code == 200
        first_data = first.get_json()
        assert first_data["ok"] is True

        second = client.post("/api/send", json={"text": "happy", "emit": "none"})
        assert second.status_code == 200
        second_data = second.get_json()
        assert second_data["ok"] is True
        assert second_data["reply"] == "OK. Uitgevoerd: DANCE"

    samples = collect_latency_ms(_dance_happy_flow, warmup=warmup, iterations=iterations)
    stats = record_perf_metric(
        metric="dance_followup_happy_flow",
        samples=samples,
        budget_ms=p95_budget_ms,
        extra={"flow": "doe een dans -> happy"},
    )
    p95 = float(stats["p95_ms"])
    expected_calls = warmup + iterations
    assert cmdrec is not None
    assert executor is not None
    assert cmdrec.calls >= expected_calls
    assert executor.calls >= expected_calls
    assert executor.resolved
    latest_resolved = executor.resolved[-1]
    assert latest_resolved.get("dance_key") == "happy"
    assert latest_resolved.get("dance_behavior") == "dances/happy"
    assert p95 <= p95_budget_ms, f"dance-happy-flow p95={p95:.2f}ms > {p95_budget_ms:.2f}ms"
