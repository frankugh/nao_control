from __future__ import annotations

import pytest
import requests

from dialog.behavior_executor import BehaviorExecutor, ConsoleAndBehaviorExecutor
from dialog.interfaces import CommandDecision


class _Resp:
    def __init__(self, payload, status_code=200) -> None:
        self._payload = payload
        self.status_code = status_code
        self.ok = status_code < 400

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"status={self.status_code}")


def test_behavior_executor_raises_on_non_ok_payload_and_calls_finish(monkeypatch):
    def fake_post(url, **kwargs):
        return _Resp({"status": "error", "error": "Behavior not installed: dances/stoomboot"})

    monkeypatch.setattr("dialog.behavior_executor.requests.post", fake_post)
    finished = []
    ex = BehaviorExecutor(base_url="http://base:5000", timeout_s=1.0, on_finish=lambda cmd: finished.append(cmd.label))
    cmd = CommandDecision(
        label="DANCE",
        confidence=1.0,
        raw_text="stoomboot",
        resolved={"dance_behavior": "dances/stoomboot"},
    )

    with pytest.raises(RuntimeError, match="Behavior not installed"):
        ex.execute(cmd)
    assert finished == ["DANCE"]


def test_behavior_executor_raises_on_request_error_and_calls_finish(monkeypatch):
    def fake_post(url, **kwargs):
        raise requests.RequestException("connection failed")

    monkeypatch.setattr("dialog.behavior_executor.requests.post", fake_post)
    finished = []
    ex = BehaviorExecutor(base_url="http://base:5000", timeout_s=1.0, on_finish=lambda cmd: finished.append(cmd.label))
    cmd = CommandDecision(
        label="DANCE",
        confidence=1.0,
        raw_text="stoomboot",
        resolved={"dance_behavior": "dances/stoomboot"},
    )

    with pytest.raises(RuntimeError, match="connection failed"):
        ex.execute(cmd)
    assert finished == ["DANCE"]


def test_console_wrapper_calls_finish_even_when_executor_fails():
    class FailingExec:
        def execute(self, cmd):
            raise RuntimeError("boom")

    finished = []
    wrapper = ConsoleAndBehaviorExecutor(FailingExec())
    wrapper.set_on_finish(lambda cmd: finished.append(cmd.label))
    cmd = CommandDecision(label="DANCE", confidence=1.0, raw_text="stoomboot", resolved={})

    with pytest.raises(RuntimeError, match="boom"):
        wrapper.execute(cmd)
    assert finished == ["DANCE"]
