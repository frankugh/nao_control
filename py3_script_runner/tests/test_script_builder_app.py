from __future__ import annotations

from http import HTTPStatus
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Dict
import time
from urllib.error import URLError

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
                "nao1": {"dm_url": "http://127.0.0.1:5301", "preset": "alex"},
                "nao2": {"dm_url": "http://localhost:5302", "preset": "renee"},
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


def test_run_session_start_passes_resolved_tts_preload_plan_to_runner(monkeypatch, tmp_path):
    seen: Dict[str, Any] = {}

    class FakeRunner:
        def __init__(self, script: Dict[str, Any], **kwargs: Any) -> None:
            seen["kwargs"] = kwargs
            self.script = script

        def preflight(self) -> None:
            return None

        def run(self):
            return SimpleNamespace(
                completed_steps=len(list(self.script.get("steps") or [])),
                total_steps=len(list(self.script.get("steps") or [])),
                aborted=False,
                log_path=Path("run.log"),
            )

    class FakePreloadService:
        def __init__(self) -> None:
            self.store = SimpleNamespace(root=tmp_path / "preloaded_tts")

        def resolve_run_plan(self, script: Dict[str, Any], policy_by_robot: Dict[str, Any]) -> Dict[str, Any]:
            assert policy_by_robot == {"nao1": {"mode": "current"}}
            return {
                "ok": True,
                "robot_modes": {"nao1": "preloaded_current"},
                "step_audio": {
                    "s1": {
                        "clip_id": "clip-1",
                        "clip_rel_path": "clips/fp/demo.wav",
                        "profile_fingerprint": "fp",
                    }
                },
            }

    monkeypatch.setattr(script_builder_app, "ScriptRunner", FakeRunner)
    monkeypatch.setattr(script_builder_app, "TTS_PRELOAD_SERVICE", FakePreloadService())
    manager = script_builder_app.RunSessionManager()

    code, _payload = manager.start(
        {
            **_script_payload(),
            "tts_preload": {"policy_by_robot": {"nao1": {"mode": "current"}}},
        }
    )

    assert code == HTTPStatus.ACCEPTED
    assert _wait_until(lambda: manager.state()["status"] == "completed")
    assert seen["kwargs"]["tts_preload_root"] == tmp_path / "preloaded_tts"
    assert seen["kwargs"]["tts_preload_robot_modes"] == {"nao1": "preloaded_current"}
    assert seen["kwargs"]["tts_preload_step_audio"]["s1"]["clip_rel_path"] == "clips/fp/demo.wav"


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


def test_run_session_start_is_blocked_while_summary_is_active(monkeypatch):
    monkeypatch.setattr(script_builder_app, "ScriptRunner", object)
    manager = script_builder_app.RunSessionManager()
    manager._state["summary_active"] = True

    code, payload = manager.start(_script_payload())

    assert code == HTTPStatus.CONFLICT
    assert payload["ok"] is False
    assert "summary is still active" in payload["error"]


def test_request_summary_abort_uses_dm_summary_abort(monkeypatch):
    seen: Dict[str, Any] = {}

    class FakeClient:
        def __init__(self, base_url: str, timeout_s: float = 0) -> None:
            seen["base_url"] = base_url
            seen["timeout_s"] = timeout_s

        def summary_abort(self, timeout_s=None):
            seen["summary_abort_timeout_s"] = timeout_s
            return {
                "ok": True,
                "session": {"session_id": "summary-123", "status": "aborted", "last_error": None},
            }

    monkeypatch.setattr(script_builder_app, "DMClient", FakeClient)
    manager = script_builder_app.RunSessionManager()
    manager._state.update(
        {
            "run_id": "run_1",
            "summary_active": True,
            "summary_waiting": False,
            "summary_url": "http://127.0.0.1:5301/summary",
            "summary_session_id": "summary-123",
            "summary_status": "capturing",
        }
    )
    manager._summary_dm_url = "http://127.0.0.1:5301"

    code, payload = manager.request_summary_abort()

    assert code == HTTPStatus.ACCEPTED
    assert payload["summary_active"] is False
    assert payload["summary_status"] == "aborted"
    assert seen["base_url"] == "http://127.0.0.1:5301"
    assert seen["summary_abort_timeout_s"] == 4.0


def test_request_abort_can_best_effort_abort_summary(monkeypatch):
    seen: Dict[str, Any] = {}

    class FakeClient:
        def __init__(self, base_url: str, timeout_s: float = 0) -> None:
            seen["base_url"] = base_url
            seen["timeout_s"] = timeout_s

        def summary_abort(self, timeout_s=None):
            seen["summary_abort_timeout_s"] = timeout_s
            return {
                "ok": True,
                "session": {"session_id": "summary-123", "status": "aborted", "last_error": None},
            }

    monkeypatch.setattr(script_builder_app, "DMClient", FakeClient)
    manager = script_builder_app.RunSessionManager()
    manager._state.update(
        {
            "status": "waiting",
            "run_id": "run_1",
            "summary_active": True,
            "summary_waiting": True,
            "summary_url": "http://127.0.0.1:5301/summary",
            "summary_session_id": "summary-123",
            "summary_status": "capturing",
        }
    )
    manager._abort_event = SimpleNamespace(set=lambda: seen.setdefault("abort_set", True))  # type: ignore[assignment]
    manager._summary_dm_url = "http://127.0.0.1:5301"

    code, payload = manager.request_abort(summary_action="abort")

    assert code == HTTPStatus.ACCEPTED
    assert payload["summary_active"] is False
    assert payload["summary_status"] == "aborted"
    assert seen["abort_set"] is True


def test_build_dm_launch_command_uses_dm_url_and_preset():
    spec = script_builder_app.DmLaunchSpec(
        robot_id="nao1",
        dm_url="http://127.0.0.1:5301",
        bind_host="127.0.0.1",
        port=5301,
        preset="alex",
    )
    command = script_builder_app._build_dm_launch_command(spec)
    assert command[:2] == ["cmd.exe", "/k"]
    assert "--host 127.0.0.1" in command[2]
    assert "--port 5301" in command[2]
    assert "--preset alex" in command[2]


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
        def __init__(self, cmd, cwd, creationflags, env):
            popen_calls.append({"cmd": cmd, "cwd": cwd, "creationflags": creationflags, "env": env})

    monkeypatch.setattr(script_builder_app.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(script_builder_app, "_local_target_is_occupied", lambda spec: False)
    code, payload = script_builder_app.start_dialog_managers(_dm_payload())

    assert code == HTTPStatus.OK
    assert payload["started_count"] == 2
    assert payload["error_count"] == 0
    assert len(popen_calls) == 2
    assert popen_calls[0]["cwd"] == str(tmp_path)
    assert popen_calls[0]["env"]["VIRTUAL_ENV"] == str(tmp_path)
    assert popen_calls[0]["env"]["PATH"].split(";")[0] == str(tmp_path)
    assert "--preset alex" in popen_calls[0]["cmd"][2]
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
        def __init__(self, cmd, cwd, creationflags, env):
            popen_calls.append({"cmd": cmd, "cwd": cwd, "creationflags": creationflags, "env": env})

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
        def __init__(self, cmd, cwd, creationflags, env):
            popen_calls.append({"cmd": cmd, "cwd": cwd, "creationflags": creationflags, "env": env})

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
        def __init__(self, cmd, cwd, creationflags, env):
            popen_calls.append({"cmd": cmd, "cwd": cwd, "creationflags": creationflags, "env": env})

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


def test_fetch_cmdrec_labels_uses_local_bundle_fallback_when_dm_is_unavailable(monkeypatch, tmp_path):
    bundle_dir = tmp_path / "bundle_v999"
    bundle_dir.mkdir(parents=True)
    (bundle_dir / "labels.json").write_text('["WAVE", "STAND_UP", "NONE"]', encoding="utf-8")

    monkeypatch.setattr(script_builder_app, "_resolve_cmdrec_bundle_path", lambda cmdrec_value, bundles_dir: bundle_dir)

    def _raise_urlerror(_request, timeout=0):  # noqa: ARG001
        raise URLError("connection refused")

    monkeypatch.setattr(script_builder_app, "urlopen", _raise_urlerror)

    code, payload = script_builder_app.fetch_cmdrec_labels(_dm_payload())

    assert code == HTTPStatus.OK
    assert payload["ok"] is True
    assert payload["labels"] == ["STAND_UP", "WAVE"]
    assert payload["errors"]
    assert any("connection refused" in str(item).lower() for item in payload["errors"])
