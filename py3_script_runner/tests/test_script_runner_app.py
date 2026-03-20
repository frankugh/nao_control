from __future__ import annotations

from http import HTTPStatus

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
