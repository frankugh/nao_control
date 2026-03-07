from __future__ import annotations

from http import HTTPStatus
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Dict
import time

from py3_script_runner import script_builder_app


def _script_payload() -> Dict[str, Any]:
    return {
        "script": {
            "version": 1,
            "robots": {"nao1": {"dm_url": "http://127.0.0.1:5301"}},
            "defaults": {"request_timeout_s": 12, "on_error": "prompt"},
            "steps": [
                {
                    "id": "s1",
                    "robot_id": "nao1",
                    "start": {"mode": "manual"},
                    "action": {"type": "say", "text": "hello"},
                }
            ],
        }
    }


def _wait_until(predicate: Callable[[], bool], timeout_s: float = 2.0) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_run_session_single_run_guard(monkeypatch):
    class FakeRunner:
        def __init__(self, script: Dict[str, Any], **kwargs: Any) -> None:
            self.script = script
            self.abort_event = kwargs.get("abort_event")

        def preflight(self) -> None:
            return None

        def run(self):
            if self.abort_event is not None:
                self.abort_event.wait(timeout=1.0)
            return SimpleNamespace(
                completed_steps=0,
                total_steps=len(list(self.script.get("steps") or [])),
                aborted=bool(self.abort_event is not None and self.abort_event.is_set()),
                log_path=Path("run.log"),
            )

    monkeypatch.setattr(script_builder_app, "ScriptRunner", FakeRunner)
    manager = script_builder_app.RunSessionManager()

    code1, _ = manager.start(_script_payload())
    code2, payload2 = manager.start(_script_payload())
    assert code1 == HTTPStatus.ACCEPTED
    assert code2 == HTTPStatus.CONFLICT
    assert payload2["ok"] is False

    abort_code, _ = manager.request_abort()
    assert abort_code == HTTPStatus.ACCEPTED
    assert _wait_until(lambda: manager.state()["status"] == "aborted")


def test_run_session_next_flow(monkeypatch):
    class FakeRunner:
        def __init__(self, script: Dict[str, Any], **kwargs: Any) -> None:
            self.script = script
            self.continue_event = kwargs.get("continue_event")
            self.abort_event = kwargs.get("abort_event")
            self.event_sink = kwargs.get("event_sink")

        def preflight(self) -> None:
            return None

        def run(self):
            if callable(self.event_sink):
                self.event_sink(
                    {
                        "type": "waiting",
                        "waiting_for_next": True,
                        "waiting_reason": "manual_start",
                        "index": 0,
                        "total": 1,
                        "step_id": "s1",
                    }
                )
            while True:
                if self.abort_event is not None and self.abort_event.is_set():
                    return SimpleNamespace(completed_steps=0, total_steps=1, aborted=True, log_path=Path("run.log"))
                if self.continue_event is not None and self.continue_event.is_set():
                    self.continue_event.clear()
                    break
                time.sleep(0.01)
            if callable(self.event_sink):
                self.event_sink({"type": "waiting_cleared", "index": 0, "total": 1, "step_id": "s1"})
                self.event_sink({"type": "step_done", "index": 0, "total": 1, "step_id": "s1", "status": "completed"})
            return SimpleNamespace(completed_steps=1, total_steps=1, aborted=False, log_path=Path("run.log"))

    monkeypatch.setattr(script_builder_app, "ScriptRunner", FakeRunner)
    manager = script_builder_app.RunSessionManager()

    code, _ = manager.start(_script_payload())
    assert code == HTTPStatus.ACCEPTED
    assert _wait_until(lambda: manager.state()["waiting_for_next"] is True)

    next_code, _ = manager.request_next()
    assert next_code == HTTPStatus.ACCEPTED
    assert _wait_until(lambda: manager.state()["status"] == "completed")
    assert manager.state()["completed_steps"] == 1

    next_code_after, payload_after = manager.request_next()
    assert next_code_after == HTTPStatus.CONFLICT
    assert payload_after["ok"] is False


def test_run_session_abort_transitions_to_aborted(monkeypatch):
    class FakeRunner:
        def __init__(self, script: Dict[str, Any], **kwargs: Any) -> None:
            self.abort_event = kwargs.get("abort_event")

        def preflight(self) -> None:
            return None

        def run(self):
            while self.abort_event is not None and not self.abort_event.is_set():
                time.sleep(0.01)
            return SimpleNamespace(completed_steps=0, total_steps=1, aborted=True, log_path=Path("run.log"))

    monkeypatch.setattr(script_builder_app, "ScriptRunner", FakeRunner)
    manager = script_builder_app.RunSessionManager()

    code, _ = manager.start(_script_payload())
    assert code == HTTPStatus.ACCEPTED

    abort_code, _ = manager.request_abort()
    assert abort_code == HTTPStatus.ACCEPTED
    assert _wait_until(lambda: manager.state()["status"] == "aborted")
