from __future__ import annotations

import pytest

from py3_script_runner.schema import ScriptSchemaError, validate_script


def _base_script():
    return {
        "version": 1,
        "robots": {
            "nao1": {"dm_url": "http://127.0.0.1:5301"},
            "nao2": {"dm_url": "http://127.0.0.1:5302"},
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


def test_validate_script_ok():
    script = _base_script()
    validated = validate_script(script)
    assert validated["version"] == 1
    assert validated["steps"][0]["action"]["type"] == "say"
    assert validated["defaults"]["readiness_poll_interval_s"] == 3.0
    assert validated["defaults"]["readiness_request_timeout_s"] == 3.0
    assert validated["defaults"]["readiness_timeout_s"] == 0.0


def test_validate_script_rejects_invalid_version():
    script = _base_script()
    script["version"] = 2
    with pytest.raises(ScriptSchemaError, match="version must be 1"):
        validate_script(script)


def test_validate_script_rejects_duplicate_step_ids():
    script = _base_script()
    script["steps"].append(
        {
            "id": "s1",
            "robot_id": "nao2",
            "start": {"mode": "manual"},
            "action": {"type": "say", "text": "hi"},
        }
    )
    with pytest.raises(ScriptSchemaError, match="duplicate step id"):
        validate_script(script)


def test_validate_script_rejects_unknown_robot_id():
    script = _base_script()
    script["steps"][0]["robot_id"] = "unknown"
    with pytest.raises(ScriptSchemaError, match="unknown robot"):
        validate_script(script)


def test_validate_script_rejects_invalid_do_mode():
    script = _base_script()
    script["steps"][0] = {
        "id": "s1",
        "robot_id": "nao1",
        "start": {"mode": "manual"},
        "action": {"type": "do", "mode": "nope"},
    }
    with pytest.raises(ScriptSchemaError, match="action.mode must be one of"):
        validate_script(script)


def test_validate_script_accepts_pause_without_robot():
    script = _base_script()
    script["steps"][0] = {
        "id": "s1",
        "start": {"mode": "after_prev", "delay_s": 1},
        "action": {"type": "pause", "seconds": 2},
    }
    validated = validate_script(script)
    assert validated["steps"][0]["action"]["type"] == "pause"
    assert "robot_id" not in validated["steps"][0]


def test_validate_script_accepts_robot_runtime_config():
    script = _base_script()
    script["robots"]["nao1"]["runtime_config"] = {
        "nao_ip": "192.168.68.101",
        "nao_base_url": "http://127.0.0.1:5101",
    }
    validated = validate_script(script)
    assert "runtime_config" in validated["robots"]["nao1"]
    assert validated["robots"]["nao1"]["runtime_config"]["nao_ip"] == "192.168.68.101"


def test_validate_script_rejects_non_object_robot_runtime_config():
    script = _base_script()
    script["robots"]["nao1"]["runtime_config"] = "invalid"
    with pytest.raises(ScriptSchemaError, match="runtime_config must be an object"):
        validate_script(script)


def test_validate_script_accepts_readiness_defaults():
    script = _base_script()
    script["defaults"]["readiness_poll_interval_s"] = 1.5
    script["defaults"]["readiness_request_timeout_s"] = 4
    script["defaults"]["readiness_timeout_s"] = 120
    validated = validate_script(script)
    defaults = validated["defaults"]
    assert defaults["readiness_poll_interval_s"] == 1.5
    assert defaults["readiness_request_timeout_s"] == 4.0
    assert defaults["readiness_timeout_s"] == 120.0
