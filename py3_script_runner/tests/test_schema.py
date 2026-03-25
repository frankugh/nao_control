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
    assert validated["ppt"]["enabled"] is False
    assert validated["ppt"]["fullscreen_required"] is True
    assert validated["ppt"]["start_capture_on_run"] is True
    assert validated["defaults"]["readiness_poll_interval_s"] == 3.0
    assert validated["defaults"]["readiness_request_timeout_s"] == 3.0
    assert validated["defaults"]["readiness_timeout_s"] == 0.0


def test_validate_script_preserves_optional_meta_script_id():
    script = _base_script()
    script["meta"] = {"script_id": "demo-script-id", "label": "Workshop"}
    validated = validate_script(script)
    assert validated["meta"]["script_id"] == "demo-script-id"
    assert validated["meta"]["label"] == "Workshop"


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


def test_validate_script_rejects_robot_runtime_config():
    script = _base_script()
    script["robots"]["nao1"]["runtime_config"] = {
        "nao_ip": "192.168.68.101",
        "nao_base_url": "http://127.0.0.1:5101",
    }
    with pytest.raises(ScriptSchemaError, match="runtime_config is no longer supported"):
        validate_script(script)


def test_validate_script_accepts_robot_instance_id():
    script = _base_script()
    script["robots"]["nao1"]["instance_id"] = "alex"
    validated = validate_script(script)
    assert validated["robots"]["nao1"]["instance_id"] == "alex"


def test_validate_script_rejects_invalid_robot_instance_id():
    script = _base_script()
    script["robots"]["nao1"]["instance_id"] = "   "
    with pytest.raises(ScriptSchemaError, match="instance_id must be a non-empty string"):
        validate_script(script)


def test_validate_script_rejects_non_string_robot_instance_id():
    script = _base_script()
    script["robots"]["nao1"]["instance_id"] = 123
    with pytest.raises(ScriptSchemaError, match="instance_id must be a non-empty string"):
        validate_script(script)


def test_validate_script_rejects_non_object_robot_runtime_config():
    script = _base_script()
    script["robots"]["nao1"]["runtime_config"] = "invalid"
    with pytest.raises(ScriptSchemaError, match="runtime_config is no longer supported"):
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


def test_validate_script_accepts_with_prev_and_ppt_step():
    script = _base_script()
    script["ppt"] = {
        "enabled": True,
        "file": "C:/slides/workshop.pptx",
        "fullscreen_required": True,
        "start_capture_on_run": False,
    }
    script["steps"][0] = {
        "id": "s1",
        "start": {"mode": "with_prev"},
        "action": {"type": "ppt", "mode": "next_slide"},
    }
    validated = validate_script(script)
    assert validated["ppt"]["enabled"] is True
    assert validated["ppt"]["file"] == "C:/slides/workshop.pptx"
    assert validated["steps"][0]["start"]["mode"] == "with_prev"
    assert validated["steps"][0]["start"]["delay_s"] == 0.0
    assert validated["steps"][0]["action"]["type"] == "ppt"
    assert validated["steps"][0]["action"]["mode"] == "next_slide"


def test_validate_script_rejects_ppt_goto_without_slide():
    script = _base_script()
    script["ppt"] = {"enabled": True, "file": "C:/slides/workshop.pptx"}
    script["steps"][0] = {
        "id": "s1",
        "start": {"mode": "manual"},
        "action": {"type": "ppt", "mode": "goto"},
    }
    with pytest.raises(ScriptSchemaError, match="action.slide"):
        validate_script(script)


def test_validate_script_accepts_ppt_goto_with_click():
    script = _base_script()
    script["ppt"] = {"enabled": True, "file": "C:/slides/workshop.pptx"}
    script["steps"][0] = {
        "id": "s1",
        "start": {"mode": "manual"},
        "action": {"type": "ppt", "mode": "goto", "slide": 3, "click": 2},
    }
    validated = validate_script(script)
    assert validated["steps"][0]["action"]["mode"] == "goto"
    assert validated["steps"][0]["action"]["slide"] == 3
    assert validated["steps"][0]["action"]["click"] == 2


def test_validate_script_accepts_legacy_ppt_goto_build_alias():
    script = _base_script()
    script["ppt"] = {"enabled": True, "file": "C:/slides/workshop.pptx"}
    script["steps"][0] = {
        "id": "s1",
        "start": {"mode": "manual"},
        "action": {"type": "ppt", "mode": "goto", "slide": 3, "build": 2},
    }
    validated = validate_script(script)
    assert validated["steps"][0]["action"]["click"] == 2
    assert "build" not in validated["steps"][0]["action"]


def test_validate_script_rejects_invalid_ppt_mode():
    script = _base_script()
    script["steps"][0] = {
        "id": "s1",
        "start": {"mode": "manual"},
        "action": {"type": "ppt", "mode": "jump"},
    }
    with pytest.raises(ScriptSchemaError, match="action.mode must be one of: next_slide, previous_slide, goto"):
        validate_script(script)


def test_validate_script_rejects_enabled_ppt_without_file():
    script = _base_script()
    script["ppt"] = {"enabled": True}
    with pytest.raises(ScriptSchemaError, match="ppt.file must be a non-empty string"):
        validate_script(script)


def test_validate_script_accepts_summary_start_mode():
    script = _base_script()
    script["steps"] = [
        {
            "id": "s1",
            "robot_id": "nao1",
            "start": {"mode": "manual"},
            "action": {
                "type": "do",
                "mode": "summary_start",
                "wait_for_complete": False,
                "open_on_new_tab": True,
            },
        },
    ]
    validated = validate_script(script)
    assert validated["steps"][0]["action"]["mode"] == "summary_start"
    assert validated["steps"][0]["action"]["wait_for_complete"] is False
    assert validated["steps"][0]["action"]["open_on_new_tab"] is True


def test_validate_script_defaults_summary_start_flags():
    script = _base_script()
    script["steps"][0]["action"] = {"type": "do", "mode": "summary_start"}
    validated = validate_script(script)
    assert validated["steps"][0]["action"]["wait_for_complete"] is True
    assert validated["steps"][0]["action"]["open_on_new_tab"] is False


def test_validate_script_rejects_non_boolean_summary_wait_flag():
    script = _base_script()
    script["steps"][0] = {
        "id": "s1",
        "robot_id": "nao1",
        "start": {"mode": "manual"},
        "action": {"type": "do", "mode": "summary_start", "wait_for_complete": "yes"},
    }
    with pytest.raises(ScriptSchemaError, match="wait_for_complete"):
        validate_script(script)


def test_validate_script_rejects_non_boolean_summary_open_tab_flag():
    script = _base_script()
    script["steps"][0] = {
        "id": "s1",
        "robot_id": "nao1",
        "start": {"mode": "manual"},
        "action": {"type": "do", "mode": "summary_start", "open_on_new_tab": "yes"},
    }
    with pytest.raises(ScriptSchemaError, match="open_on_new_tab"):
        validate_script(script)


def test_validate_script_accepts_legacy_summary_boolean_strings():
    script = _base_script()
    script["steps"][0] = {
        "id": "summary",
        "robot_id": "nao1",
        "start": {"mode": "manual"},
        "action": {
            "type": "do",
            "mode": "summary_start",
            "wait_for_complete": "true",
            "open_on_new_tab": "",
        },
    }

    validated = validate_script(script)

    assert validated["steps"][0]["action"]["wait_for_complete"] is True
    assert validated["steps"][0]["action"]["open_on_new_tab"] is False


def test_validate_script_accepts_nao_set_eye_color_mode():
    script = _base_script()
    script["steps"][0] = {
        "id": "s1",
        "robot_id": "nao1",
        "start": {"mode": "manual"},
        "action": {"type": "do", "mode": "nao_set_eye_color", "color": "#00AEEF", "duration": 0.35},
    }
    validated = validate_script(script)
    assert validated["steps"][0]["action"]["mode"] == "nao_set_eye_color"
    assert validated["steps"][0]["action"]["color"] == "#00AEEF"
    assert validated["steps"][0]["action"]["duration"] == 0.35


def test_validate_script_rejects_nao_set_eye_color_without_color():
    script = _base_script()
    script["steps"][0] = {
        "id": "s1",
        "robot_id": "nao1",
        "start": {"mode": "manual"},
        "action": {"type": "do", "mode": "nao_set_eye_color", "duration": 0.35},
    }
    with pytest.raises(ScriptSchemaError, match="action.color"):
        validate_script(script)
