from __future__ import annotations

from types import SimpleNamespace

from dialog.interfaces import LLMResult

import webapp_server


class StubLLMBackend:
    def generate(self, messages):
        return LLMResult(reply="ok", messages=list(messages))


class StubSTTBackend:
    def transcribe(self, audio):  # pragma: no cover
        raise AssertionError("STT not used in these tests")


class NoopThread:
    def __init__(self, target=None, daemon=None, *args, **kwargs):
        self._target = target
        self.daemon = daemon
        self.started = False

    def start(self):
        self.started = True


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


def test_new_continuous_state_starts_with_metadata():
    state = webapp_server._new_continuous_state(now_ts=123.0)
    assert state["phase"] == "idle"
    assert state["phase_reason"] == "init"
    assert state["phase_seq"] == 0
    assert state["phase_changed_at"] == 123.0
    assert state["phase_log"][-1]["phase"] == "idle"
    assert state["phase_log"][-1]["reason"] == "init"


def test_transition_same_phase_keeps_seq_and_updates_reason():
    state = webapp_server._new_continuous_state(now_ts=10.0)
    changed = webapp_server._transition_continuous_phase(
        state,
        "idle",
        reason="still_idle",
        now_ts=11.0,
    )
    assert changed is False
    assert state["phase"] == "idle"
    assert state["phase_reason"] == "still_idle"
    assert state["phase_seq"] == 0
    assert len(state["phase_log"]) == 1


def test_transition_log_is_bounded():
    state = webapp_server._new_continuous_state(now_ts=1.0)
    limit = webapp_server._CONTINUOUS_PHASE_LOG_LIMIT
    for i in range(limit + 8):
        phase = "listening" if (i % 2 == 0) else "thinking"
        webapp_server._transition_continuous_phase(
            state,
            phase,
            reason=f"step_{i}",
            now_ts=100.0 + i,
        )
    assert len(state["phase_log"]) == limit
    assert state["phase_log"][-1]["seq"] == state["phase_seq"]
    assert state["phase_log"][-1]["reason"] == f"step_{limit + 7}"


def test_continuous_capture_timeout_s_probes_while_timeout_gate_open():
    bounded = webapp_server._continuous_capture_timeout_s(
        10**9,
        wake_mode="timeout",
        gate_open=True,
        wake_open_until=120.0,
        now_s=100.0,
        probe_max_s=0.8,
    )
    assert bounded <= 0.8
    assert bounded >= 0.25


def test_continuous_capture_timeout_s_keeps_base_when_gate_not_open():
    base = webapp_server._continuous_capture_timeout_s(
        12.0,
        wake_mode="timeout",
        gate_open=False,
        wake_open_until=120.0,
        now_s=100.0,
    )
    assert base == 12.0


def test_continuous_state_api_reports_phase_metadata(monkeypatch):
    monkeypatch.setattr(webapp_server.threading, "Thread", NoopThread)
    app = _make_app(monkeypatch)
    client = app.test_client()

    before = client.get("/api/continuous_state").get_json()
    assert before["ok"] is True
    assert before["phase"] == "idle"
    assert before["phase_reason"] == "init"
    assert before["phase_seq"] == 0

    start_resp = client.post("/api/continuous_start")
    assert start_resp.status_code == 200
    started = client.get("/api/continuous_state").get_json()
    assert started["running"] is True
    assert started["phase"] == "listening"
    assert started["phase_reason"] == "continuous_start"
    assert started["phase_seq"] >= 1
    assert isinstance(started["phase_log"], list)
    assert started["phase_log"][-1]["phase"] == "listening"

    stop_resp = client.post("/api/continuous_stop")
    assert stop_resp.status_code == 200
    stopped = client.get("/api/continuous_state").get_json()
    assert stopped["running"] is False
    assert stopped["phase"] == "stopped"
    assert stopped["phase_reason"] == "manual_stop"
    assert stopped["phase_seq"] >= started["phase_seq"]


def test_continuous_start_with_wake_mode_always_starts_in_wake_listening(monkeypatch):
    monkeypatch.setattr(webapp_server.threading, "Thread", NoopThread)
    app = _make_app(monkeypatch)
    client = app.test_client()

    cfg_resp = client.post("/api/runtime_config", json={"wake_mode": "always"})
    assert cfg_resp.status_code == 200
    assert cfg_resp.get_json()["ok"] is True

    resp = client.post("/api/continuous_start")
    assert resp.status_code == 200
    state = client.get("/api/continuous_state").get_json()
    assert state["ok"] is True
    assert state["running"] is True
    assert state["phase"] == "wake_listening"
