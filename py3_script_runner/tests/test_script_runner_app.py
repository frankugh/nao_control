from __future__ import annotations

from http import HTTPStatus
import time

from py3_script_runner import script_runner_app


def test_run_session_manager_formats_step_errors_with_step_context():
    manager = script_runner_app.RunSessionManager()
    manager._state["run_id"] = "run_1"

    manager._handle_runner_event(
        "run_1",
        {
            "type": "step_error",
            "index": 1,
            "total": 3,
            "step_id": "box_demo",
            "error": "Behavior greetings/doboxsit niet uitgevoerd: robot meldt rust/wake-state false.",
        },
    )

    state = manager.state()
    assert state["last_error"] == (
        "[2/3] box_demo: Behavior greetings/doboxsit niet uitgevoerd: robot meldt rust/wake-state false."
    )
    assert state["log_tail"][-1] == (
        "[RUN][STEP_ERROR] [2/3] box_demo: Behavior greetings/doboxsit niet uitgevoerd: robot meldt rust/wake-state false."
    )


def test_run_session_manager_tracks_summary_state_from_runner_event():
    manager = script_runner_app.RunSessionManager()
    manager._state["run_id"] = "run_1"

    manager._handle_runner_event(
        "run_1",
        {
            "type": "summary_state",
            "summary_active": True,
            "summary_waiting": True,
            "summary_url": "http://127.0.0.1:5301/summary",
            "summary_session_id": "summary-123",
            "summary_status": "capturing",
            "summary_connection_ok": True,
            "summary_last_error": "",
            "summary_open_nonce": 2,
            "summary_dm_url": "http://127.0.0.1:5301",
        },
    )

    state = manager.state()
    assert state["summary_active"] is True
    assert state["summary_waiting"] is True
    assert state["summary_url"] == "http://127.0.0.1:5301/summary"
    assert state["summary_session_id"] == "summary-123"
    assert state["summary_status"] == "capturing"
    assert state["summary_open_nonce"] == 2


def test_run_session_manager_rejects_next_while_waiting_for_summary():
    manager = script_runner_app.RunSessionManager()
    manager._state.update(
        {
            "status": "waiting",
            "run_id": "run_1",
            "waiting_for_next": True,
            "waiting_reason": "summary_wait",
        }
    )
    manager._continue_event = object()  # type: ignore[assignment]

    code, payload = manager.request_next()

    assert code == HTTPStatus.CONFLICT
    assert payload["ok"] is False
    assert "summary completion" in payload["error"]


def test_run_session_manager_shutdown_aborts_active_runner_and_waits_for_close(monkeypatch):
    closed = {"value": False}

    class FakeRunner:
        def __init__(self, script, **kwargs):
            self.abort_event = kwargs.get("abort_event")

        def preflight(self):
            while self.abort_event is not None and not self.abort_event.is_set():
                time.sleep(0.01)
            raise RuntimeError("abort requested during shutdown")

        def run(self):
            return None

        def close(self):
            closed["value"] = True

    monkeypatch.setattr(script_runner_app, "ScriptRunner", FakeRunner)

    manager = script_runner_app.RunSessionManager()
    code, _payload = manager.start(_watch_payload())
    assert code == HTTPStatus.ACCEPTED

    deadline = time.time() + 2.0
    while time.time() < deadline and manager.state()["status"] != "preflight":
        time.sleep(0.01)

    manager.shutdown(join_timeout_s=1.0)

    assert closed["value"] is True


def _watch_payload():
    return {
        "script": {
            "version": 1,
            "robots": {"nao1": {"dm_url": "http://127.0.0.1:5301", "instance_id": "alex"}},
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


def test_poll_auto_rest_watch_reports_robot_state(monkeypatch):
    seen = {}

    class FakeClient:
        def __init__(self, base_url, timeout_s=0):
            seen["base_url"] = base_url
            seen["timeout_s"] = timeout_s

        def nao_command_state(self, timeout_s=None):
            seen["nao_command_state_timeout_s"] = timeout_s
            return {
                "ok": True,
                "nao_enabled": True,
                "virtual_robot": False,
                "reachable": True,
                "awake": {"ok": True, "is_awake": True},
                "posture": {"ok": True, "posture": "Standing"},
                "auto_rest": {
                    "enabled_by_config": True,
                    "suspended": False,
                    "timer_active": True,
                    "timeout_s": 180,
                    "seconds_until_rest": 17,
                },
            }

    monkeypatch.setattr(script_runner_app, "DMClient", FakeClient)

    code, payload = script_runner_app.poll_auto_rest_watch(_watch_payload())
    assert code == HTTPStatus.OK
    assert payload["ok"] is True
    assert payload["robots"][0]["ok"] is True
    assert payload["robots"][0]["reachable"] is True
    assert payload["robots"][0]["nao_enabled"] is True
    assert payload["robots"][0]["awake"]["is_awake"] is True
    assert payload["robots"][0]["posture"]["posture"] == "Standing"
    assert payload["robots"][0]["auto_rest"]["seconds_until_rest"] == 17
    assert payload["robots"][0]["instance_id"] == "alex"
    assert seen["base_url"] == "http://127.0.0.1:5301"


def test_poll_auto_rest_watch_reports_unreachable_dm(monkeypatch):
    class FakeClient:
        def __init__(self, *_args, **_kwargs):
            pass

        def nao_command_state(self, timeout_s=None):
            assert timeout_s == 4.0
            return {
                "ok": True,
                "nao_enabled": True,
                "virtual_robot": False,
                "reachable": False,
                "errors": ["base down"],
                "awake": {"ok": False, "error": "base down"},
                "posture": {"ok": False, "error": "base down"},
                "auto_rest": {
                    "enabled_by_config": True,
                    "suspended": False,
                    "timer_active": True,
                    "timeout_s": 180,
                    "seconds_until_rest": 12,
                },
            }

    monkeypatch.setattr(script_runner_app, "DMClient", FakeClient)

    code, payload = script_runner_app.poll_auto_rest_watch(_watch_payload())
    assert code == HTTPStatus.OK
    assert payload["ok"] is True
    robot = payload["robots"][0]
    assert robot["ok"] is False
    assert robot["reachable"] is False
    assert robot["error"] == "base down"
    assert robot["awake"]["ok"] is False
    assert robot["posture"]["ok"] is False


def test_poll_auto_rest_watch_marks_virtual_robot_when_nao_is_disabled(monkeypatch):
    class FakeClient:
        def __init__(self, *_args, **_kwargs):
            pass

        def nao_command_state(self, timeout_s=None):
            assert timeout_s == 4.0
            return {
                "ok": True,
                "nao_enabled": False,
                "virtual_robot": True,
                "reachable": False,
                "awake": {"ok": False, "disabled": True},
                "posture": {"ok": False, "disabled": True},
                "auto_rest": {
                    "enabled_by_config": True,
                    "suspended": False,
                    "timer_active": False,
                    "timeout_s": 180,
                    "seconds_until_rest": 180,
                },
                "errors": [],
                "connectivity_issues": [],
            }

    monkeypatch.setattr(script_runner_app, "DMClient", FakeClient)

    code, payload = script_runner_app.poll_auto_rest_watch(_watch_payload())
    assert code == HTTPStatus.OK
    robot = payload["robots"][0]
    assert robot["ok"] is False
    assert robot["reachable"] is False
    assert robot["nao_enabled"] is False
    assert robot["virtual_robot"] is True
    assert robot["error"] == ""


def test_poll_auto_rest_watch_surfaces_active_connectivity_issues(monkeypatch):
    class FakeClient:
        def __init__(self, *_args, **_kwargs):
            pass

        def nao_command_state(self, timeout_s=None):
            assert timeout_s == 4.0
            return {
                "ok": True,
                "reachable": True,
                "awake": {"ok": True, "is_awake": True},
                "posture": {"ok": True, "posture": "Standing"},
                "auto_rest": {
                    "enabled_by_config": True,
                    "suspended": False,
                    "timer_active": True,
                    "timeout_s": 180,
                    "seconds_until_rest": 12,
                },
                "connectivity_issues": [
                    {
                        "issue_key": "dm_local:base_ping",
                        "issue_type": "dm_local",
                        "severity": "error",
                        "message": "Base connector is niet bereikbaar.",
                        "source": "runtime_health.base_ping",
                        "first_seen_at": "2026-03-20T12:00:00.000Z",
                        "active": True,
                    }
                ],
            }

    monkeypatch.setattr(script_runner_app, "DMClient", FakeClient)

    code, payload = script_runner_app.poll_auto_rest_watch(_watch_payload())
    assert code == HTTPStatus.OK
    robot = payload["robots"][0]
    assert robot["ok"] is True
    assert robot["reachable"] is True
    assert robot["error"] == "Base connector is niet bereikbaar."
    assert robot["connectivity_issues"] == [
        {
            "issue_key": "dm_local:base_ping",
            "issue_type": "dm_local",
            "severity": "error",
            "message": "Base connector is niet bereikbaar.",
            "source": "runtime_health.base_ping",
            "first_seen_at": "2026-03-20T12:00:00.000Z",
            "active": True,
        }
    ]


def test_poll_auto_rest_watch_wraps_dm_exception_as_sr_issue(monkeypatch):
    class FakeClient:
        def __init__(self, *_args, **_kwargs):
            pass

        def nao_command_state(self, timeout_s=None):
            assert timeout_s == 4.0
            raise OSError("connection refused")

    monkeypatch.setattr(script_runner_app, "DMClient", FakeClient)

    code, payload = script_runner_app.poll_auto_rest_watch(_watch_payload())
    assert code == HTTPStatus.OK
    robot = payload["robots"][0]
    assert robot["ok"] is False
    assert robot["reachable"] is False
    assert robot["error"] == "connection refused"
    assert robot["connectivity_issues"][0]["issue_key"] == "sr_dm:unreachable"
    assert robot["connectivity_issues"][0]["issue_type"] == "sr_dm"
    assert robot["connectivity_issues"][0]["message"] == "DM niet bereikbaar: connection refused"
    assert robot["connectivity_issues"][0]["active"] is True


def test_iter_script_dm_targets_skips_robots_without_dm_url():
    targets = script_runner_app._iter_script_dm_targets(
        {
            "robots": {
                "nao1": {"instance_id": "alex"},
                "nao2": {"dm_url": "http://127.0.0.1:5302", "instance_id": "renee"},
            }
        }
    )
    assert targets == [
        {
            "robot_id": "nao2",
            "dm_url": "http://127.0.0.1:5302",
            "instance_id": "renee",
        }
    ]
