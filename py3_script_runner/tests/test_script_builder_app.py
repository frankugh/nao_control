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


def _dm_payload() -> Dict[str, Any]:
    return {
        "script": {
            "version": 1,
            "robots": {
                "nao1": {"dm_url": "http://127.0.0.1:5301", "instance_id": "alex"},
                "nao2": {"dm_url": "http://localhost:5302"},
            },
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


def test_build_dm_launch_command_uses_dm_url_and_instance():
    spec = script_builder_app.DmLaunchSpec(
        robot_id="nao1",
        dm_url="http://127.0.0.1:5301",
        bind_host="127.0.0.1",
        port=5301,
        instance_id="alex",
    )
    command = script_builder_app._build_dm_launch_command(spec)
    assert command[:2] == ["cmd.exe", "/k"]
    assert "--host 127.0.0.1" in command[2]
    assert "--port 5301" in command[2]
    assert "--instance-id alex" in command[2]


def test_start_dialog_managers_starts_each_unique_local_target(monkeypatch, tmp_path):
    dm_python = tmp_path / "python.exe"
    dm_python.write_text("", encoding="utf-8")
    dm_webapp = tmp_path / "webapp_server.py"
    dm_webapp.write_text("", encoding="utf-8")
    monkeypatch.setattr(script_builder_app, "DM_DIR", tmp_path)
    monkeypatch.setattr(script_builder_app, "DM_PYTHON", dm_python)
    monkeypatch.setattr(script_builder_app, "DM_WEBAPP", dm_webapp)

    popen_calls = []

    class FakePopen:
        def __init__(self, cmd, cwd, creationflags):
            popen_calls.append({"cmd": cmd, "cwd": cwd, "creationflags": creationflags})

    monkeypatch.setattr(script_builder_app.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(script_builder_app, "_local_target_is_occupied", lambda spec: False)
    code, payload = script_builder_app.start_dialog_managers(_dm_payload())

    assert code == HTTPStatus.OK
    assert payload["started_count"] == 2
    assert payload["error_count"] == 0
    assert len(popen_calls) == 2
    assert popen_calls[0]["cwd"] == str(tmp_path)
    assert "--instance-id alex" in popen_calls[0]["cmd"][2]
    assert "--port 5302" in popen_calls[1]["cmd"][2]


def test_start_dialog_managers_rejects_remote_targets_and_keeps_local_ones(monkeypatch, tmp_path):
    dm_python = tmp_path / "python.exe"
    dm_python.write_text("", encoding="utf-8")
    dm_webapp = tmp_path / "webapp_server.py"
    dm_webapp.write_text("", encoding="utf-8")
    monkeypatch.setattr(script_builder_app, "DM_DIR", tmp_path)
    monkeypatch.setattr(script_builder_app, "DM_PYTHON", dm_python)
    monkeypatch.setattr(script_builder_app, "DM_WEBAPP", dm_webapp)

    popen_calls = []

    class FakePopen:
        def __init__(self, cmd, cwd, creationflags):
            popen_calls.append({"cmd": cmd, "cwd": cwd, "creationflags": creationflags})

    monkeypatch.setattr(script_builder_app.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(script_builder_app, "_local_target_is_occupied", lambda spec: False)
    payload = _dm_payload()
    payload["script"]["robots"]["nao2"]["dm_url"] = "http://192.168.68.102:5302"

    code, response = script_builder_app.start_dialog_managers(payload)

    assert code == HTTPStatus.OK
    assert response["started_count"] == 1
    assert response["error_count"] == 1
    assert len(popen_calls) == 1
    robot2 = next(item for item in response["results"] if item["robot_id"] == "nao2")
    assert robot2["started"] is False
    assert "lokale host" in robot2["error"]


def test_start_dialog_managers_rejects_duplicate_local_targets(monkeypatch, tmp_path):
    dm_python = tmp_path / "python.exe"
    dm_python.write_text("", encoding="utf-8")
    dm_webapp = tmp_path / "webapp_server.py"
    dm_webapp.write_text("", encoding="utf-8")
    monkeypatch.setattr(script_builder_app, "DM_DIR", tmp_path)
    monkeypatch.setattr(script_builder_app, "DM_PYTHON", dm_python)
    monkeypatch.setattr(script_builder_app, "DM_WEBAPP", dm_webapp)

    popen_calls = []

    class FakePopen:
        def __init__(self, cmd, cwd, creationflags):
            popen_calls.append({"cmd": cmd, "cwd": cwd, "creationflags": creationflags})

    monkeypatch.setattr(script_builder_app.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(script_builder_app, "_local_target_is_occupied", lambda spec: False)
    payload = _dm_payload()
    payload["script"]["robots"]["nao2"]["dm_url"] = "http://localhost:5301"

    code, response = script_builder_app.start_dialog_managers(payload)

    assert code == HTTPStatus.OK
    assert response["started_count"] == 0
    assert response["error_count"] == 2
    assert len(popen_calls) == 0
    assert all("Dubbele lokale DM target" in item["error"] for item in response["results"])


def test_start_dialog_managers_skips_targets_that_are_already_occupied(monkeypatch, tmp_path):
    dm_python = tmp_path / "python.exe"
    dm_python.write_text("", encoding="utf-8")
    dm_webapp = tmp_path / "webapp_server.py"
    dm_webapp.write_text("", encoding="utf-8")
    monkeypatch.setattr(script_builder_app, "DM_DIR", tmp_path)
    monkeypatch.setattr(script_builder_app, "DM_PYTHON", dm_python)
    monkeypatch.setattr(script_builder_app, "DM_WEBAPP", dm_webapp)

    popen_calls = []

    class FakePopen:
        def __init__(self, cmd, cwd, creationflags):
            popen_calls.append({"cmd": cmd, "cwd": cwd, "creationflags": creationflags})

    monkeypatch.setattr(script_builder_app.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(script_builder_app, "_local_target_is_occupied", lambda spec: spec.port == 5301)

    code, response = script_builder_app.start_dialog_managers(_dm_payload())

    assert code == HTTPStatus.OK
    assert response["started_count"] == 1
    assert response["error_count"] == 1
    assert len(popen_calls) == 1
    robot1 = next(item for item in response["results"] if item["robot_id"] == "nao1")
    assert robot1["started"] is False
    assert "Er draait al iets op" in robot1["error"]
