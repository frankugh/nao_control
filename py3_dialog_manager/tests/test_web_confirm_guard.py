from types import SimpleNamespace

from dialog.interfaces import CommandDecision, LLMResult, RouteDecision

import webapp_server


class StubLLMBackend:
    def __init__(self, reply: str = "ok") -> None:
        self._reply = reply
        self.calls = 0

    def generate(self, messages):
        self.calls += 1
        return LLMResult(reply=self._reply, messages=list(messages))


class StubSTTBackend:
    def transcribe(self, audio):  # pragma: no cover
        raise AssertionError("STT not used in these tests")


class StubExecutor:
    def __init__(self) -> None:
        self.calls = 0
        self.labels: list[str] = []

    def execute(self, cmd: CommandDecision) -> None:
        self.calls += 1
        self.labels.append(cmd.label)


class StubCmdRec:
    def __init__(self, decisions: dict[str, RouteDecision], guarded_labels: set[str]) -> None:
        self._decisions = decisions
        self._guarded = {label.upper() for label in guarded_labels}

    def route(self, text: str, mode: str, active_behavior):
        return self._decisions[text]

    def is_guarded(self, label: str) -> bool:
        return label.upper() in self._guarded


def _make_app(monkeypatch, decisions, guarded_labels):
    executor = StubExecutor()
    base_pipeline = SimpleNamespace(
        llm=StubLLMBackend(),
        output=None,
        status_to_console=True,
        system_prompt="SYSTEM",
        log_messages_path=None,
        log_meta={},
        max_history_turns=None,
        _cmdrec=StubCmdRec(decisions, guarded_labels),
        _behavior_executor=executor,
        _debug_cmdrec=False,
    )
    monkeypatch.setattr(webapp_server, "build_pipeline_from_config", lambda *_a, **_k: base_pipeline)
    monkeypatch.setattr(webapp_server, "make_stt_backend_from_config", lambda *_a, **_k: StubSTTBackend())
    app, _, _ = webapp_server.create_app(
        cfg={"confirm_method": "web", "confirm_timeout_s": 10.0},
        config_path="<memory>",
    )
    return app, executor


def test_guarded_command_requires_confirm(monkeypatch):
    decisions = {
        "dans": RouteDecision(
            is_command=True,
            command=CommandDecision(label="DANCE", confidence=0.9, raw_text="dans"),
            top3=[("DANCE", 0.9)],
        )
    }
    app, executor = _make_app(monkeypatch, decisions, {"DANCE"})
    client = app.test_client()

    resp = client.post("/api/send", json={"text": "dans"})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert executor.calls == 0
    confirm_msg = data["history"][-1]
    assert confirm_msg["type"] == "confirm_required"

    confirm_id = confirm_msg["confirmation_id"]
    resp2 = client.post("/api/confirm", json={"confirmation_id": confirm_id, "confirmed": True})
    data2 = resp2.get_json()
    assert data2["ok"] is True
    assert executor.calls == 1
    assert data2["history"][-1]["content"] == "✅ Uitgevoerd: DANCE"


def test_guarded_command_cancel(monkeypatch):
    decisions = {
        "dans": RouteDecision(
            is_command=True,
            command=CommandDecision(label="DANCE", confidence=0.9, raw_text="dans"),
            top3=[("DANCE", 0.9)],
        )
    }
    app, executor = _make_app(monkeypatch, decisions, {"DANCE"})
    client = app.test_client()

    resp = client.post("/api/send", json={"text": "dans"})
    confirm_id = resp.get_json()["history"][-1]["confirmation_id"]
    resp2 = client.post("/api/confirm", json={"confirmation_id": confirm_id, "confirmed": False})
    data2 = resp2.get_json()
    assert data2["ok"] is True
    assert executor.calls == 0
    assert data2["history"][-1]["content"] == "❌ Geannuleerd"


def test_stop_bypasses_guard_and_cancels_pending(monkeypatch):
    decisions = {
        "dans": RouteDecision(
            is_command=True,
            command=CommandDecision(label="DANCE", confidence=0.9, raw_text="dans"),
            top3=[("DANCE", 0.9)],
        ),
        "stop": RouteDecision(
            is_command=True,
            command=CommandDecision(label="STOP", confidence=0.9, raw_text="stop"),
            top3=[("STOP", 0.9)],
        ),
    }
    app, executor = _make_app(monkeypatch, decisions, {"DANCE"})
    client = app.test_client()

    client.post("/api/send", json={"text": "dans"})
    resp = client.post("/api/send", json={"text": "stop"})
    data = resp.get_json()
    assert data["ok"] is True
    assert executor.calls == 1
    assert executor.labels == ["STOP"]
    assert data["history"][-1]["content"] == "✅ Uitgevoerd: STOP"
