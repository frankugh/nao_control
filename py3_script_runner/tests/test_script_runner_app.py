from __future__ import annotations

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
