from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List


ALLOWED_START_MODES = {"manual", "after_prev", "with_prev"}
ALLOWED_ACTION_TYPES = {"say", "do", "pause", "ppt"}
ALLOWED_DO_MODES = {
    "command",
    "behavior_start",
    "behavior_stop",
    "dance",
    "nao_set_eye_color",
    "summary_start",
}
ALLOWED_PPT_MODES = {"next_slide", "previous_slide", "goto"}
ALLOWED_ON_ERROR = {"prompt", "abort", "continue"}


class ScriptSchemaError(ValueError):
    """Raised when a script file does not follow the expected schema."""


def _fail(message: str) -> None:
    raise ScriptSchemaError(message)


def _as_dict(value: Any, field: str) -> Dict[str, Any]:
    if not isinstance(value, dict):
        _fail(f"{field} must be an object")
    return value


def _as_list(value: Any, field: str) -> List[Any]:
    if not isinstance(value, list):
        _fail(f"{field} must be a list")
    return value


def _as_nonempty_str(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        _fail(f"{field} must be a non-empty string")
    return text


def _as_nonnegative_float(value: Any, field: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        _fail(f"{field} must be a number >= 0")
    if number < 0:
        _fail(f"{field} must be >= 0")
    return number


def _as_positive_float(value: Any, field: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        _fail(f"{field} must be a number > 0")
    if number <= 0:
        _fail(f"{field} must be > 0")
    return number


def _as_positive_int(value: Any, field: str) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        _fail(f"{field} must be an integer > 0")
    if number <= 0:
        _fail(f"{field} must be > 0")
    return number


def _as_nonnegative_int(value: Any, field: str) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        _fail(f"{field} must be an integer >= 0")
    if number < 0:
        _fail(f"{field} must be >= 0")
    return number


def _coerce_legacy_bool(value: Any, field: str) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        _fail(f"{field} must be a boolean")
    text = str(value).strip().lower()
    if text in {"true", "1"}:
        return True
    if text in {"false", "0", ""}:
        return False
    _fail(f"{field} must be a boolean")


def validate_script(raw: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        _fail("script root must be an object")

    version = raw.get("version")
    if version != 1:
        _fail("version must be 1")

    meta_raw = raw.get("meta")
    meta: Dict[str, Any] | None = None
    if meta_raw is not None:
        meta_in = _as_dict(meta_raw, "meta")
        meta = dict(meta_in)
        if "script_id" in meta_in:
            meta["script_id"] = _as_nonempty_str(meta_in.get("script_id"), "meta.script_id")

    robots_in = _as_dict(raw.get("robots"), "robots")
    if not robots_in:
        _fail("robots must contain at least one robot")
    robots: Dict[str, Dict[str, Any]] = {}
    for robot_id, robot_cfg_raw in robots_in.items():
        rid = _as_nonempty_str(robot_id, "robots.<id>")
        robot_cfg = _as_dict(robot_cfg_raw, f"robots.{rid}")
        dm_url = _as_nonempty_str(robot_cfg.get("dm_url"), f"robots.{rid}.dm_url")
        normalized_robot: Dict[str, Any] = {"dm_url": dm_url}
        if "instance_id" in robot_cfg:
            _fail(f"robots.{rid}.instance_id is no longer supported; use robots.{rid}.preset for local DM autostart")
        preset = robot_cfg.get("preset", None)
        if preset is not None:
            if not isinstance(preset, str) or not preset.strip():
                _fail(f"robots.{rid}.preset must be a non-empty string if provided")
            normalized_robot["preset"] = preset.strip()
        if "runtime_config" in robot_cfg:
            _fail(
                f"robots.{rid}.runtime_config is no longer supported; move summary/runtime settings to DM instead"
            )
        robots[rid] = normalized_robot

    defaults_raw = raw.get("defaults") or {}
    defaults = _as_dict(defaults_raw, "defaults")
    request_timeout_s = defaults.get("request_timeout_s", 12)
    request_timeout_s = _as_positive_float(request_timeout_s, "defaults.request_timeout_s")
    readiness_poll_interval_s = _as_positive_float(
        defaults.get("readiness_poll_interval_s", 3),
        "defaults.readiness_poll_interval_s",
    )
    readiness_request_timeout_s = _as_positive_float(
        defaults.get("readiness_request_timeout_s", 3),
        "defaults.readiness_request_timeout_s",
    )
    readiness_timeout_s = _as_nonnegative_float(
        defaults.get("readiness_timeout_s", 0),
        "defaults.readiness_timeout_s",
    )
    on_error = str(defaults.get("on_error", "prompt") or "").strip().lower()
    if on_error not in ALLOWED_ON_ERROR:
        _fail("defaults.on_error must be one of: prompt, abort, continue")

    ppt_raw = raw.get("ppt") or {}
    ppt_in = _as_dict(ppt_raw, "ppt")
    ppt_enabled = bool(ppt_in.get("enabled", False))
    ppt_fullscreen_required = bool(ppt_in.get("fullscreen_required", True))
    ppt_start_capture_on_run = bool(ppt_in.get("start_capture_on_run", True))
    ppt_cfg: Dict[str, Any] = {
        "enabled": ppt_enabled,
        "fullscreen_required": ppt_fullscreen_required,
        "start_capture_on_run": ppt_start_capture_on_run,
    }
    if "file" in ppt_in:
        ppt_cfg["file"] = _as_nonempty_str(ppt_in.get("file"), "ppt.file")
    if ppt_enabled and not str(ppt_cfg.get("file") or "").strip():
        _fail("ppt.file must be a non-empty string when ppt.enabled=true")

    steps_in = _as_list(raw.get("steps"), "steps")
    if not steps_in:
        _fail("steps must contain at least one step")

    seen_ids = set()
    steps: List[Dict[str, Any]] = []
    for idx, step_raw in enumerate(steps_in):
        step = _as_dict(step_raw, f"steps[{idx}]")
        step_id = _as_nonempty_str(step.get("id"), f"steps[{idx}].id")
        if step_id in seen_ids:
            _fail(f"duplicate step id: {step_id}")
        seen_ids.add(step_id)

        start = _as_dict(step.get("start"), f"steps[{idx}].start")
        start_mode = str(start.get("mode") or "").strip().lower()
        if start_mode not in ALLOWED_START_MODES:
            _fail(f"steps[{idx}].start.mode must be one of: manual, after_prev, with_prev")
        delay_s = 0.0
        if start_mode == "after_prev":
            delay_s = _as_nonnegative_float(start.get("delay_s", 0), f"steps[{idx}].start.delay_s")

        action = _as_dict(step.get("action"), f"steps[{idx}].action")
        action_type = str(action.get("type") or "").strip().lower()
        if action_type not in ALLOWED_ACTION_TYPES:
            _fail(f"steps[{idx}].action.type must be one of: say, do, pause, ppt")

        step_on_error = action.get("on_error", step.get("on_error", on_error))
        step_on_error = str(step_on_error or "").strip().lower()
        if step_on_error not in ALLOWED_ON_ERROR:
            _fail(f"steps[{idx}].on_error must be one of: prompt, abort, continue")
        step_timeout = _as_positive_float(
            action.get("request_timeout_s", step.get("request_timeout_s", request_timeout_s)),
            f"steps[{idx}].request_timeout_s",
        )

        normalized_step: Dict[str, Any] = {
            "id": step_id,
            "start": {"mode": start_mode, "delay_s": delay_s},
            "action": {"type": action_type},
            "on_error": step_on_error,
            "request_timeout_s": step_timeout,
        }

        if action_type in {"say", "do"}:
            robot_id = _as_nonempty_str(step.get("robot_id"), f"steps[{idx}].robot_id")
            if robot_id not in robots:
                _fail(f"steps[{idx}].robot_id references unknown robot '{robot_id}'")
            normalized_step["robot_id"] = robot_id

        if action_type == "say":
            text = _as_nonempty_str(action.get("text"), f"steps[{idx}].action.text")
            normalized_step["action"]["text"] = text
        elif action_type == "pause":
            seconds = _as_nonnegative_float(action.get("seconds"), f"steps[{idx}].action.seconds")
            normalized_step["action"]["seconds"] = seconds
        elif action_type == "do":
            do_mode = str(action.get("mode") or "").strip().lower()
            if do_mode not in ALLOWED_DO_MODES:
                _fail(
                    "steps[{idx}].action.mode must be one of: "
                    "command, behavior_start, behavior_stop, dance, nao_set_eye_color, "
                    "summary_start".format(idx=idx)
                )
            normalized_step["action"]["mode"] = do_mode
            if do_mode == "command":
                normalized_step["action"]["label"] = _as_nonempty_str(
                    action.get("label"), f"steps[{idx}].action.label"
                )
                if "resolved" in action:
                    if not isinstance(action.get("resolved"), dict):
                        _fail(f"steps[{idx}].action.resolved must be an object if provided")
                    normalized_step["action"]["resolved"] = dict(action.get("resolved") or {})
            elif do_mode == "dance":
                normalized_step["action"]["dance_key"] = _as_nonempty_str(
                    action.get("dance_key"), f"steps[{idx}].action.dance_key"
                )
            elif do_mode == "nao_set_eye_color":
                normalized_step["action"]["color"] = _as_nonempty_str(
                    action.get("color"),
                    f"steps[{idx}].action.color",
                )
                if "duration" in action and action.get("duration") is not None:
                    normalized_step["action"]["duration"] = _as_nonnegative_float(
                        action.get("duration"),
                        f"steps[{idx}].action.duration",
                    )
            elif do_mode == "summary_start":
                wait_for_complete = _coerce_legacy_bool(
                    action.get("wait_for_complete", True),
                    f"steps[{idx}].action.wait_for_complete",
                )
                normalized_step["action"]["wait_for_complete"] = wait_for_complete
                open_on_new_tab = _coerce_legacy_bool(
                    action.get("open_on_new_tab", False),
                    f"steps[{idx}].action.open_on_new_tab",
                )
                normalized_step["action"]["open_on_new_tab"] = open_on_new_tab
            else:
                normalized_step["action"]["behavior"] = _as_nonempty_str(
                    action.get("behavior"), f"steps[{idx}].action.behavior"
                )
        else:
            ppt_mode = str(action.get("mode") or "").strip().lower()
            if ppt_mode not in ALLOWED_PPT_MODES:
                _fail(f"steps[{idx}].action.mode must be one of: next_slide, previous_slide, goto")
            normalized_step["action"]["mode"] = ppt_mode
            if ppt_mode == "goto":
                normalized_step["action"]["slide"] = _as_positive_int(action.get("slide"), f"steps[{idx}].action.slide")
                click_value = action.get("click", action.get("build"))
                if click_value is not None:
                    normalized_step["action"]["click"] = _as_nonnegative_int(
                        click_value,
                        f"steps[{idx}].action.click",
                    )

        steps.append(normalized_step)

    normalized: Dict[str, Any] = {
        "version": 1,
        "robots": robots,
        "ppt": ppt_cfg,
        "defaults": {
            "request_timeout_s": request_timeout_s,
            "readiness_poll_interval_s": readiness_poll_interval_s,
            "readiness_request_timeout_s": readiness_request_timeout_s,
            "readiness_timeout_s": readiness_timeout_s,
            "on_error": on_error,
        },
        "steps": steps,
    }
    if meta is not None:
        normalized["meta"] = meta
    return normalized


def load_script(path: str | Path) -> Dict[str, Any]:
    script_path = Path(path)
    if not script_path.exists():
        _fail(f"script file not found: {script_path}")
    try:
        raw = json.loads(script_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        _fail(f"invalid JSON in script file: {exc}")
    return validate_script(raw)
