from __future__ import annotations

from typing import Any, Dict, List, Tuple

import pytest

from py3_script_runner.client import DMClientError
from py3_script_runner.runner import ScriptRunner


def _script_template() -> Dict[str, Any]:
    return {
        "version": 1,
        "robots": {
            "nao1": {"dm_url": "http://dm-nao1:5301"},
            "nao2": {"dm_url": "http://dm-nao2:5302"},
        },
        "defaults": {"request_timeout_s": 12, "on_error": "prompt"},
        "steps": [],
    }


def test_preflight_checks_capabilities(tmp_path):
    calls: List[Tuple[str, str]] = []

    class FakeClient:
        def __init__(self, base_url: str, timeout_s: float) -> None:
            self.base_url = base_url

        def capabilities(self, timeout_s=None):
            calls.append(("capabilities", self.base_url))
            return {"ok": True, "supports": {"say": True}}

        def runtime_effective(self, timeout_s=None):
            return {"ok": True, "runtime_config": {}}

        def runtime_health(self, payload, timeout_s=None):
            return {
                "ok": True,
                "base": {"ping": True, "nao_ping": True},
                "behavior": {"ping": True, "nao_ping": True},
                "nao": {"ping": True},
            }

    script = _script_template()
    runner = ScriptRunner(
        script,
        client_factory=lambda url, timeout_s: FakeClient(url, timeout_s),  # type: ignore[arg-type]
        input_func=lambda _p: "",
        sleep_func=lambda _s: None,
        log_dir=tmp_path,
    )
    runner.preflight()
    assert ("capabilities", "http://dm-nao1:5301") in calls
    assert ("capabilities", "http://dm-nao2:5302") in calls


def test_preflight_applies_robot_runtime_config(tmp_path):
    calls: List[Tuple[str, str]] = []

    class FakeClient:
        def __init__(self, base_url: str, timeout_s: float) -> None:
            self.base_url = base_url

        def set_runtime_config(self, config, timeout_s=None):
            calls.append(("set_runtime_config", self.base_url))
            return {"ok": True}

        def capabilities(self, timeout_s=None):
            calls.append(("capabilities", self.base_url))
            return {"ok": True, "supports": {"say": True}}

        def runtime_effective(self, timeout_s=None):
            return {"ok": True, "runtime_config": {"nao_base_url": "http://127.0.0.1:5101"}}

        def runtime_health(self, payload, timeout_s=None):
            return {
                "ok": True,
                "base": {"ping": True, "nao_ping": True},
                "behavior": {"ping": True, "nao_ping": True},
                "nao": {"ping": True},
            }

    script = _script_template()
    script["robots"]["nao1"]["runtime_config"] = {"nao_base_url": "http://127.0.0.1:5101"}

    runner = ScriptRunner(
        script,
        client_factory=lambda url, timeout_s: FakeClient(url, timeout_s),  # type: ignore[arg-type]
        input_func=lambda _p: "",
        sleep_func=lambda _s: None,
        log_dir=tmp_path,
    )
    runner.preflight()
    assert ("set_runtime_config", "http://dm-nao1:5301") in calls
    assert ("capabilities", "http://dm-nao1:5301") in calls


def test_preflight_waits_until_all_robots_ready(tmp_path):
    sleep_calls: List[float] = []
    health_calls: Dict[str, int] = {}

    class FakeClient:
        def __init__(self, base_url: str, timeout_s: float) -> None:
            self.base_url = base_url
            health_calls[self.base_url] = 0

        def capabilities(self, timeout_s=None):
            return {"ok": True, "supports": {"say": True}}

        def runtime_effective(self, timeout_s=None):
            return {
                "ok": True,
                "runtime_config": {
                    "base_enabled": True,
                    "behavior_enabled": True,
                    "nao_ip_enabled": False,
                    "nao_base_url": "http://127.0.0.1:5101",
                    "output_target": "nao",
                    "tts_engine": "azure",
                },
            }

        def runtime_health(self, payload, timeout_s=None):
            health_calls[self.base_url] += 1
            # nao2 is initially down on base connector for one poll cycle.
            if self.base_url.endswith(":5302") and health_calls[self.base_url] == 1:
                return {
                    "ok": True,
                    "base": {"ping": False, "nao_ping": False},
                    "behavior": {"ping": True, "nao_ping": True},
                    "nao": {"ping": True},
                }
            return {
                "ok": True,
                "base": {"ping": True, "nao_ping": True},
                "behavior": {"ping": True, "nao_ping": True},
                "nao": {"ping": True},
            }

    script = _script_template()
    runner = ScriptRunner(
        script,
        client_factory=lambda url, timeout_s: FakeClient(url, timeout_s),  # type: ignore[arg-type]
        input_func=lambda _p: "",
        sleep_func=lambda s: sleep_calls.append(float(s)),
        log_dir=tmp_path,
    )
    runner.preflight()
    assert sleep_calls, "expected readiness polling to wait at least once"
    assert abs(sleep_calls[0] - 3.0) < 1e-6
    assert health_calls["http://dm-nao2:5302"] >= 2
    log_text = runner.log_path.read_text(encoding="utf-8")
    assert "Robot nao2: WAIT - base connector down" in log_text
    assert "Readiness complete: all robots ready." in log_text


def test_preflight_reports_dm_down_then_recovers(tmp_path):
    sleep_calls: List[float] = []
    cap_calls: Dict[str, int] = {}

    class FakeClient:
        def __init__(self, base_url: str, timeout_s: float) -> None:
            self.base_url = base_url
            cap_calls[self.base_url] = 0

        def capabilities(self, timeout_s=None):
            cap_calls[self.base_url] += 1
            if self.base_url.endswith(":5301") and cap_calls[self.base_url] == 1:
                raise DMClientError("connection refused")
            return {"ok": True, "supports": {"say": True}}

        def runtime_effective(self, timeout_s=None):
            return {
                "ok": True,
                "runtime_config": {
                    "base_enabled": False,
                    "behavior_enabled": False,
                    "nao_ip_enabled": False,
                },
            }

        def runtime_health(self, payload, timeout_s=None):
            return {"ok": True, "base": {"ping": True, "nao_ping": True}, "behavior": {"ping": True, "nao_ping": True}, "nao": {"ping": True}}

    script = _script_template()
    runner = ScriptRunner(
        script,
        client_factory=lambda url, timeout_s: FakeClient(url, timeout_s),  # type: ignore[arg-type]
        input_func=lambda _p: "",
        sleep_func=lambda s: sleep_calls.append(float(s)),
        log_dir=tmp_path,
    )
    runner.preflight()
    assert sleep_calls, "expected at least one readiness retry when DM is down"
    log_text = runner.log_path.read_text(encoding="utf-8")
    assert "Robot nao1: WAIT - DM down (" in log_text
    assert "Robot nao1: READY - dm+connections OK" in log_text


def test_manual_step_waits_for_enter(tmp_path):
    prompts: List[str] = []
    calls: List[Tuple[str, str, Dict[str, Any] | None]] = []

    class FakeClient:
        def __init__(self, base_url: str, timeout_s: float) -> None:
            self.base_url = base_url

        def capabilities(self, timeout_s=None):
            return {"ok": True}

        def runtime_effective(self, timeout_s=None):
            return {"ok": True, "runtime_config": {}}

        def script_say(self, text: str, timeout_s=None):
            calls.append(("say", self.base_url, {"text": text}))
            return {"ok": True}

        def script_do(self, payload: Dict[str, Any], timeout_s=None):
            calls.append(("do", self.base_url, payload))
            return {"ok": True}

    script = _script_template()
    script["steps"] = [
        {
            "id": "s1",
            "robot_id": "nao1",
            "start": {"mode": "manual", "delay_s": 0},
            "request_timeout_s": 12,
            "on_error": "prompt",
            "action": {"type": "say", "text": "hello"},
        }
    ]

    runner = ScriptRunner(
        script,
        client_factory=lambda url, timeout_s: FakeClient(url, timeout_s),  # type: ignore[arg-type]
        input_func=lambda prompt: prompts.append(prompt) or "",
        sleep_func=lambda _s: None,
        log_dir=tmp_path,
    )
    result = runner.run()
    assert result.aborted is False
    assert result.completed_steps == 1
    assert len(prompts) == 1
    assert calls == [("say", "http://dm-nao1:5301", {"text": "hello"})]


def test_manual_step_without_stdin_raises_clear_error(tmp_path):
    class FakeClient:
        def __init__(self, base_url: str, timeout_s: float) -> None:
            self.base_url = base_url

        def capabilities(self, timeout_s=None):
            return {"ok": True}

        def runtime_effective(self, timeout_s=None):
            return {"ok": True, "runtime_config": {}}

        def script_say(self, text: str, timeout_s=None):
            return {"ok": True}

        def script_do(self, payload: Dict[str, Any], timeout_s=None):
            return {"ok": True}

    script = _script_template()
    script["steps"] = [
        {
            "id": "s1",
            "robot_id": "nao1",
            "start": {"mode": "manual", "delay_s": 0},
            "request_timeout_s": 12,
            "on_error": "prompt",
            "action": {"type": "say", "text": "hello"},
        }
    ]

    def _input(_prompt: str) -> str:
        raise EOFError()

    runner = ScriptRunner(
        script,
        client_factory=lambda url, timeout_s: FakeClient(url, timeout_s),  # type: ignore[arg-type]
        input_func=_input,
        sleep_func=lambda _s: None,
        log_dir=tmp_path,
    )
    try:
        runner.run()
        assert False, "expected RuntimeError"
    except RuntimeError as exc:
        assert "manual step requires interactive stdin" in str(exc)


def test_after_prev_delay_is_applied(tmp_path):
    sleep_calls: List[float] = []
    calls: List[Tuple[str, str, Dict[str, Any]]] = []

    class FakeClient:
        def __init__(self, base_url: str, timeout_s: float) -> None:
            self.base_url = base_url

        def capabilities(self, timeout_s=None):
            return {"ok": True}

        def runtime_effective(self, timeout_s=None):
            return {"ok": True, "runtime_config": {}}

        def script_say(self, text: str, timeout_s=None):
            calls.append(("say", self.base_url, {"text": text}))
            return {"ok": True}

        def script_do(self, payload: Dict[str, Any], timeout_s=None):
            calls.append(("do", self.base_url, payload))
            return {"ok": True}

    script = _script_template()
    script["steps"] = [
        {
            "id": "s1",
            "robot_id": "nao1",
            "start": {"mode": "after_prev", "delay_s": 0},
            "request_timeout_s": 12,
            "on_error": "prompt",
            "action": {"type": "say", "text": "intro"},
        },
        {
            "id": "s2",
            "robot_id": "nao2",
            "start": {"mode": "after_prev", "delay_s": 2},
            "request_timeout_s": 12,
            "on_error": "prompt",
            "action": {"type": "do", "mode": "command", "label": "STOP"},
        },
    ]

    runner = ScriptRunner(
        script,
        client_factory=lambda url, timeout_s: FakeClient(url, timeout_s),  # type: ignore[arg-type]
        input_func=lambda _p: "",
        sleep_func=lambda s: sleep_calls.append(float(s)),
        log_dir=tmp_path,
    )
    result = runner.run()
    assert result.aborted is False
    assert result.completed_steps == 2
    assert 2.0 in sleep_calls


def test_error_prompt_retry_retries_current_step(tmp_path):
    call_count = {"say": 0}
    prompts: List[str] = []

    class FakeClient:
        def __init__(self, base_url: str, timeout_s: float) -> None:
            self.base_url = base_url

        def capabilities(self, timeout_s=None):
            return {"ok": True}

        def runtime_effective(self, timeout_s=None):
            return {"ok": True, "runtime_config": {}}

        def script_say(self, text: str, timeout_s=None):
            call_count["say"] += 1
            if call_count["say"] == 1:
                raise DMClientError("temporary failure")
            return {"ok": True}

        def script_do(self, payload: Dict[str, Any], timeout_s=None):
            return {"ok": True}

    script = _script_template()
    script["steps"] = [
        {
            "id": "s1",
            "robot_id": "nao1",
            "start": {"mode": "after_prev", "delay_s": 0},
            "request_timeout_s": 12,
            "on_error": "prompt",
            "action": {"type": "say", "text": "retry me"},
        }
    ]

    answers = iter(["r"])

    def _input(prompt: str) -> str:
        prompts.append(prompt)
        return next(answers)

    runner = ScriptRunner(
        script,
        client_factory=lambda url, timeout_s: FakeClient(url, timeout_s),  # type: ignore[arg-type]
        input_func=_input,
        sleep_func=lambda _s: None,
        log_dir=tmp_path,
    )
    result = runner.run()
    assert result.aborted is False
    assert result.completed_steps == 1
    assert call_count["say"] == 2
    assert any("Step failed" in p for p in prompts)


def test_error_prompt_next_skips_failed_step(tmp_path):
    call_count = {"say": 0}
    prompts: List[str] = []

    class FakeClient:
        def __init__(self, base_url: str, timeout_s: float) -> None:
            self.base_url = base_url

        def capabilities(self, timeout_s=None):
            return {"ok": True}

        def runtime_effective(self, timeout_s=None):
            return {"ok": True, "runtime_config": {}}

        def script_say(self, text: str, timeout_s=None):
            call_count["say"] += 1
            raise DMClientError("still failing")

        def script_do(self, payload: Dict[str, Any], timeout_s=None):
            return {"ok": True}

    script = _script_template()
    script["steps"] = [
        {
            "id": "s1",
            "robot_id": "nao1",
            "start": {"mode": "after_prev", "delay_s": 0},
            "request_timeout_s": 12,
            "on_error": "prompt",
            "action": {"type": "say", "text": "will fail"},
        },
        {
            "id": "s2",
            "start": {"mode": "after_prev", "delay_s": 0},
            "request_timeout_s": 12,
            "on_error": "prompt",
            "action": {"type": "pause", "seconds": 0},
        },
    ]

    answers = iter(["n"])

    def _input(prompt: str) -> str:
        prompts.append(prompt)
        return next(answers)

    runner = ScriptRunner(
        script,
        client_factory=lambda url, timeout_s: FakeClient(url, timeout_s),  # type: ignore[arg-type]
        input_func=_input,
        sleep_func=lambda _s: None,
        log_dir=tmp_path,
    )
    result = runner.run()
    assert result.aborted is False
    assert result.completed_steps == 1
    assert call_count["say"] == 1
    assert any("Step failed" in p for p in prompts)


def test_timeout_error_defaults_to_next_without_prompt(tmp_path):
    prompts: List[str] = []

    class FakeClient:
        def __init__(self, base_url: str, timeout_s: float) -> None:
            self.base_url = base_url

        def capabilities(self, timeout_s=None):
            return {"ok": True}

        def runtime_effective(self, timeout_s=None):
            return {"ok": True, "runtime_config": {}}

        def script_say(self, text: str, timeout_s=None):
            raise DMClientError("Read timed out. (read timeout=45.0)")

        def script_do(self, payload: Dict[str, Any], timeout_s=None):
            return {"ok": True}

    script = _script_template()
    script["steps"] = [
        {
            "id": "s1",
            "robot_id": "nao1",
            "start": {"mode": "after_prev", "delay_s": 0},
            "request_timeout_s": 12,
            "on_error": "prompt",
            "action": {"type": "say", "text": "will timeout"},
        }
    ]

    def _input(prompt: str) -> str:
        prompts.append(prompt)
        return ""

    runner = ScriptRunner(
        script,
        client_factory=lambda url, timeout_s: FakeClient(url, timeout_s),  # type: ignore[arg-type]
        input_func=_input,
        sleep_func=lambda _s: None,
        log_dir=tmp_path,
    )
    result = runner.run()
    assert result.aborted is False
    assert result.completed_steps == 0
    assert all("Step failed. Choose" not in prompt for prompt in prompts)


def test_error_prompt_abort_stops_run(tmp_path):
    call_count = {"say": 0}

    class FakeClient:
        def __init__(self, base_url: str, timeout_s: float) -> None:
            self.base_url = base_url

        def capabilities(self, timeout_s=None):
            return {"ok": True}

        def runtime_effective(self, timeout_s=None):
            return {"ok": True, "runtime_config": {}}

        def script_say(self, text: str, timeout_s=None):
            call_count["say"] += 1
            raise DMClientError("hard failure")

        def script_do(self, payload: Dict[str, Any], timeout_s=None):
            return {"ok": True}

    script = _script_template()
    script["steps"] = [
        {
            "id": "s1",
            "robot_id": "nao1",
            "start": {"mode": "after_prev", "delay_s": 0},
            "request_timeout_s": 12,
            "on_error": "prompt",
            "action": {"type": "say", "text": "fail"},
        },
        {
            "id": "s2",
            "robot_id": "nao2",
            "start": {"mode": "after_prev", "delay_s": 0},
            "request_timeout_s": 12,
            "on_error": "prompt",
            "action": {"type": "do", "mode": "command", "label": "STOP"},
        },
    ]

    answers = iter(["a"])
    runner = ScriptRunner(
        script,
        client_factory=lambda url, timeout_s: FakeClient(url, timeout_s),  # type: ignore[arg-type]
        input_func=lambda _p: next(answers),
        sleep_func=lambda _s: None,
        log_dir=tmp_path,
    )
    result = runner.run()
    assert result.aborted is True
    assert result.completed_steps == 0
    assert call_count["say"] == 1


def test_pause_step_makes_no_dm_action_calls(tmp_path):
    calls: List[Tuple[str, str]] = []

    class FakeClient:
        def __init__(self, base_url: str, timeout_s: float) -> None:
            self.base_url = base_url

        def capabilities(self, timeout_s=None):
            calls.append(("capabilities", self.base_url))
            return {"ok": True}

        def script_say(self, text: str, timeout_s=None):
            calls.append(("say", self.base_url))
            return {"ok": True}

        def script_do(self, payload: Dict[str, Any], timeout_s=None):
            calls.append(("do", self.base_url))
            return {"ok": True}

    script = _script_template()
    script["steps"] = [
        {
            "id": "s1",
            "start": {"mode": "after_prev", "delay_s": 0},
            "request_timeout_s": 12,
            "on_error": "prompt",
            "action": {"type": "pause", "seconds": 0},
        }
    ]
    runner = ScriptRunner(
        script,
        client_factory=lambda url, timeout_s: FakeClient(url, timeout_s),  # type: ignore[arg-type]
        input_func=lambda _p: "",
        sleep_func=lambda _s: None,
        log_dir=tmp_path,
    )
    result = runner.run()
    assert result.aborted is False
    assert result.completed_steps == 1
    assert calls == []


def test_multi_robot_steps_route_to_correct_dm_urls(tmp_path):
    calls: List[Tuple[str, str, Dict[str, Any]]] = []

    class FakeClient:
        def __init__(self, base_url: str, timeout_s: float) -> None:
            self.base_url = base_url

        def capabilities(self, timeout_s=None):
            return {"ok": True}

        def runtime_effective(self, timeout_s=None):
            return {"ok": True, "runtime_config": {}}

        def script_say(self, text: str, timeout_s=None):
            calls.append(("say", self.base_url, {"text": text}))
            return {"ok": True}

        def script_do(self, payload: Dict[str, Any], timeout_s=None):
            calls.append(("do", self.base_url, dict(payload)))
            return {"ok": True}

    script = _script_template()
    script["steps"] = [
        {
            "id": "s1",
            "robot_id": "nao1",
            "start": {"mode": "after_prev", "delay_s": 0},
            "request_timeout_s": 12,
            "on_error": "prompt",
            "action": {"type": "say", "text": "hello nao1"},
        },
        {
            "id": "s2",
            "robot_id": "nao2",
            "start": {"mode": "after_prev", "delay_s": 0},
            "request_timeout_s": 12,
            "on_error": "prompt",
            "action": {"type": "do", "mode": "dance", "dance_key": "happy"},
        },
    ]
    runner = ScriptRunner(
        script,
        client_factory=lambda url, timeout_s: FakeClient(url, timeout_s),  # type: ignore[arg-type]
        input_func=lambda _p: "",
        sleep_func=lambda _s: None,
        log_dir=tmp_path,
    )
    result = runner.run()
    assert result.aborted is False
    assert result.completed_steps == 2
    assert calls[0] == ("say", "http://dm-nao1:5301", {"text": "hello nao1"})
    assert calls[1][0] == "do"
    assert calls[1][1] == "http://dm-nao2:5302"
    assert calls[1][2]["mode"] == "dance"
    assert calls[1][2]["dance_key"] == "happy"


def test_summary_stop_and_draft_builds_payload(tmp_path):
    calls: List[Tuple[str, Dict[str, Any]]] = []

    class FakeClient:
        def __init__(self, base_url: str, timeout_s: float) -> None:
            self.base_url = base_url

        def script_say(self, text: str, timeout_s=None):
            return {"ok": True}

        def script_do(self, payload: Dict[str, Any], timeout_s=None):
            calls.append((self.base_url, dict(payload)))
            if payload.get("mode") == "summary_capture_stop_and_draft":
                return {"ok": True, "draft": "samenvatting", "transcript": ["een", "twee"]}
            return {"ok": True}

    script = _script_template()
    script["steps"] = [
        {
            "id": "s1",
            "robot_id": "nao1",
            "start": {"mode": "after_prev", "delay_s": 0},
            "request_timeout_s": 12,
            "on_error": "prompt",
            "action": {"type": "do", "mode": "summary_capture_start", "hold_until_continue": False},
        },
        {
            "id": "s2",
            "robot_id": "nao1",
            "start": {"mode": "after_prev", "delay_s": 0},
            "request_timeout_s": 12,
            "on_error": "prompt",
            "action": {
                "type": "do",
                "mode": "summary_capture_stop_and_draft",
                "input_prompt_template": "Transcript:\n{transcript}\nInstruction:\n{instruction}",
                "instruction": "Maak een compacte samenvatting",
                "system_prompt_file": "master_prompts/workshop_summary_system.txt",
            },
        },
    ]

    def _input(prompt: str) -> str:
        if prompt.startswith("[SUMMARY] Draft review"):
            return "p"
        if prompt.startswith("Step failed. Choose"):
            return "n"
        return ""

    runner = ScriptRunner(
        script,
        client_factory=lambda url, timeout_s: FakeClient(url, timeout_s),  # type: ignore[arg-type]
        input_func=_input,
        sleep_func=lambda _s: None,
        log_dir=tmp_path,
    )
    result = runner.run()
    assert result.aborted is False
    assert result.completed_steps == 2
    assert calls[0][1]["mode"] == "summary_capture_start"
    assert calls[1][1]["mode"] == "summary_capture_stop_and_draft"
    assert calls[1][1]["input_prompt_template"].startswith("Transcript:")
    assert calls[1][1]["instruction"] == "Maak een compacte samenvatting"
    assert calls[1][1]["system_prompt_file"] == "master_prompts/workshop_summary_system.txt"


def test_summary_draft_is_logged_before_publish_prompt(tmp_path):
    prompts: List[str] = []
    draft_logged_before_prompt: List[bool] = []
    runner_holder: Dict[str, Any] = {}

    class FakeClient:
        def __init__(self, base_url: str, timeout_s: float) -> None:
            self.base_url = base_url

        def script_say(self, text: str, timeout_s=None):
            return {"ok": True}

        def script_do(self, payload: Dict[str, Any], timeout_s=None):
            if payload.get("mode") == "summary_capture_stop_and_draft":
                return {"ok": True, "draft": "samenvatting voor review", "transcript": ["een", "twee"]}
            return {"ok": True}

    def _input(prompt: str) -> str:
        prompts.append(prompt)
        if prompt.startswith("[SUMMARY] Draft review"):
            runner = runner_holder["runner"]
            log_text = runner.log_path.read_text(encoding="utf-8")
            draft_logged_before_prompt.append("[SUMMARY] Draft:" in log_text and "[SUMMARY] samenvatting voor review" in log_text)
            return "p"
        return ""

    script = _script_template()
    script["steps"] = [
        {
            "id": "s1",
            "robot_id": "nao1",
            "start": {"mode": "after_prev", "delay_s": 0},
            "request_timeout_s": 12,
            "on_error": "prompt",
            "action": {"type": "do", "mode": "summary_capture_start", "hold_until_continue": False},
        },
        {
            "id": "s2",
            "robot_id": "nao1",
            "start": {"mode": "after_prev", "delay_s": 0},
            "request_timeout_s": 12,
            "on_error": "prompt",
            "action": {
                "type": "do",
                "mode": "summary_capture_stop_and_draft",
                "input_prompt_template": "Transcript:\n{transcript}",
            },
        },
    ]
    runner = ScriptRunner(
        script,
        client_factory=lambda url, timeout_s: FakeClient(url, timeout_s),  # type: ignore[arg-type]
        input_func=_input,
        sleep_func=lambda _s: None,
        log_dir=tmp_path,
    )
    runner_holder["runner"] = runner
    result = runner.run()
    assert result.aborted is False
    assert result.completed_steps == 2
    assert any(p.startswith("[SUMMARY] Draft review") for p in prompts)
    assert draft_logged_before_prompt == [True]


def test_summary_capture_start_holds_until_operator_continue(tmp_path):
    calls: List[Dict[str, Any]] = []
    answers = iter(["x", ""])

    class FakeClient:
        def __init__(self, base_url: str, timeout_s: float) -> None:
            self.base_url = base_url

        def script_say(self, text: str, timeout_s=None):
            return {"ok": True}

        def script_do(self, payload: Dict[str, Any], timeout_s=None):
            calls.append(dict(payload))
            return {"ok": True}

    script = _script_template()
    script["steps"] = [
        {
            "id": "s1",
            "robot_id": "nao1",
            "start": {"mode": "after_prev", "delay_s": 0},
            "request_timeout_s": 12,
            "on_error": "prompt",
            "action": {"type": "do", "mode": "summary_capture_start"},
        }
    ]
    runner = ScriptRunner(
        script,
        client_factory=lambda url, timeout_s: FakeClient(url, timeout_s),  # type: ignore[arg-type]
        input_func=lambda _p: next(answers),
        sleep_func=lambda _s: None,
        log_dir=tmp_path,
    )
    result = runner.run()
    assert result.aborted is False
    assert result.completed_steps == 1
    assert calls == [{"mode": "summary_capture_start"}]


def test_summary_decision_cancel_after_draft_skips_publish_step(tmp_path):
    calls: List[Dict[str, Any]] = []

    class FakeClient:
        def __init__(self, base_url: str, timeout_s: float) -> None:
            self.base_url = base_url

        def script_say(self, text: str, timeout_s=None):
            return {"ok": True}

        def script_do(self, payload: Dict[str, Any], timeout_s=None):
            calls.append(dict(payload))
            if payload.get("mode") == "summary_capture_stop_and_draft":
                return {"ok": True, "draft": "samenvatting", "transcript": ["een", "twee"]}
            return {"ok": True}

    script = _script_template()
    script["steps"] = [
        {
            "id": "s1",
            "robot_id": "nao1",
            "start": {"mode": "after_prev", "delay_s": 0},
            "request_timeout_s": 12,
            "on_error": "prompt",
            "action": {"type": "do", "mode": "summary_capture_start", "hold_until_continue": False},
        },
        {
            "id": "s2",
            "robot_id": "nao1",
            "start": {"mode": "after_prev", "delay_s": 0},
            "request_timeout_s": 12,
            "on_error": "prompt",
            "action": {
                "type": "do",
                "mode": "summary_capture_stop_and_draft",
                "input_prompt_template": "Transcript:\n{transcript}",
            },
        },
        {
            "id": "s3",
            "robot_id": "nao1",
            "start": {"mode": "after_prev", "delay_s": 0},
            "request_timeout_s": 12,
            "on_error": "prompt",
            "action": {"type": "do", "mode": "summary_publish"},
        },
    ]
    runner = ScriptRunner(
        script,
        client_factory=lambda url, timeout_s: FakeClient(url, timeout_s),  # type: ignore[arg-type]
        input_func=lambda _p: "c",
        sleep_func=lambda _s: None,
        log_dir=tmp_path,
    )
    result = runner.run()
    assert result.aborted is False
    assert result.completed_steps == 3
    assert calls == [
        {"mode": "summary_capture_start"},
        {"mode": "summary_capture_stop_and_draft", "input_prompt_template": "Transcript:\n{transcript}"},
        {"mode": "summary_cancel"},
    ]


def test_summary_decision_publish_after_draft_runs_publish_step(tmp_path):
    calls: List[Dict[str, Any]] = []

    class FakeClient:
        def __init__(self, base_url: str, timeout_s: float) -> None:
            self.base_url = base_url

        def script_say(self, text: str, timeout_s=None):
            return {"ok": True}

        def script_do(self, payload: Dict[str, Any], timeout_s=None):
            calls.append(dict(payload))
            if payload.get("mode") == "summary_capture_stop_and_draft":
                return {"ok": True, "draft": "samenvatting", "transcript": ["een", "twee"]}
            return {"ok": True}

    script = _script_template()
    script["steps"] = [
        {
            "id": "s1",
            "robot_id": "nao1",
            "start": {"mode": "after_prev", "delay_s": 0},
            "request_timeout_s": 12,
            "on_error": "prompt",
            "action": {"type": "do", "mode": "summary_capture_start", "hold_until_continue": False},
        },
        {
            "id": "s2",
            "robot_id": "nao1",
            "start": {"mode": "after_prev", "delay_s": 0},
            "request_timeout_s": 12,
            "on_error": "prompt",
            "action": {
                "type": "do",
                "mode": "summary_capture_stop_and_draft",
                "input_prompt_template": "Transcript:\n{transcript}",
            },
        },
        {
            "id": "s3",
            "robot_id": "nao1",
            "start": {"mode": "after_prev", "delay_s": 0},
            "request_timeout_s": 12,
            "on_error": "prompt",
            "action": {"type": "do", "mode": "summary_publish"},
        },
    ]
    runner = ScriptRunner(
        script,
        client_factory=lambda url, timeout_s: FakeClient(url, timeout_s),  # type: ignore[arg-type]
        input_func=lambda _p: "",
        sleep_func=lambda _s: None,
        log_dir=tmp_path,
    )
    result = runner.run()
    assert result.aborted is False
    assert result.completed_steps == 3
    assert calls == [
        {"mode": "summary_capture_start"},
        {"mode": "summary_capture_stop_and_draft", "input_prompt_template": "Transcript:\n{transcript}"},
        {"mode": "summary_publish"},
    ]


def test_summary_publish_prompt_cancel_routes_to_summary_cancel(tmp_path):
    calls: List[Dict[str, Any]] = []

    class FakeClient:
        def __init__(self, base_url: str, timeout_s: float) -> None:
            self.base_url = base_url

        def script_say(self, text: str, timeout_s=None):
            return {"ok": True}

        def script_do(self, payload: Dict[str, Any], timeout_s=None):
            calls.append(dict(payload))
            return {"ok": True}

    script = _script_template()
    script["steps"] = [
        {
            "id": "s1",
            "robot_id": "nao1",
            "start": {"mode": "after_prev", "delay_s": 0},
            "request_timeout_s": 12,
            "on_error": "prompt",
            "action": {"type": "do", "mode": "summary_publish"},
        }
    ]
    runner = ScriptRunner(
        script,
        client_factory=lambda url, timeout_s: FakeClient(url, timeout_s),  # type: ignore[arg-type]
        input_func=lambda _p: "c",
        sleep_func=lambda _s: None,
        log_dir=tmp_path,
    )
    result = runner.run()
    assert result.aborted is False
    assert result.completed_steps == 1
    assert calls == [{"mode": "summary_cancel"}]


def test_summary_publish_prompt_publish_keeps_summary_publish_mode(tmp_path):
    calls: List[Dict[str, Any]] = []

    class FakeClient:
        def __init__(self, base_url: str, timeout_s: float) -> None:
            self.base_url = base_url

        def script_say(self, text: str, timeout_s=None):
            return {"ok": True}

        def script_do(self, payload: Dict[str, Any], timeout_s=None):
            calls.append(dict(payload))
            return {"ok": True}

    script = _script_template()
    script["steps"] = [
        {
            "id": "s1",
            "robot_id": "nao1",
            "start": {"mode": "after_prev", "delay_s": 0},
            "request_timeout_s": 12,
            "on_error": "prompt",
            "action": {"type": "do", "mode": "summary_publish"},
        }
    ]
    runner = ScriptRunner(
        script,
        client_factory=lambda url, timeout_s: FakeClient(url, timeout_s),  # type: ignore[arg-type]
        input_func=lambda _p: "p",
        sleep_func=lambda _s: None,
        log_dir=tmp_path,
    )
    result = runner.run()
    assert result.aborted is False
    assert result.completed_steps == 1
    assert calls == [{"mode": "summary_publish"}]


def test_with_prev_mode_executes_without_sleep(tmp_path):
    sleep_calls: List[float] = []
    say_calls: List[str] = []
    did_overlap: List[bool] = []

    class FakeClient:
        def __init__(self, base_url: str, timeout_s: float) -> None:
            self.base_url = base_url

        def script_say(self, text: str, timeout_s=None):
            say_calls.append(text)
            # If with_prev is truly parallel, do_called should be set while say is waiting.
            did_overlap.append(do_called.wait(timeout=0.2))
            return {"ok": True}

        def script_do(self, payload: Dict[str, Any], timeout_s=None):
            do_called.set()
            return {"ok": True}

    import threading

    do_called = threading.Event()

    script = _script_template()
    script["steps"] = [
        {
            "id": "s1",
            "robot_id": "nao1",
            "start": {"mode": "after_prev", "delay_s": 0},
            "request_timeout_s": 12,
            "on_error": "prompt",
            "action": {"type": "say", "text": "hello"},
        },
        {
            "id": "s2",
            "robot_id": "nao1",
            "start": {"mode": "with_prev", "delay_s": 0},
            "request_timeout_s": 12,
            "on_error": "prompt",
            "action": {"type": "do", "mode": "command", "label": "WAVE"},
        }
    ]

    runner = ScriptRunner(
        script,
        client_factory=lambda url, timeout_s: FakeClient(url, timeout_s),  # type: ignore[arg-type]
        input_func=lambda _p: "",
        sleep_func=lambda s: sleep_calls.append(float(s)),
        log_dir=tmp_path,
    )
    result = runner.run()
    assert result.aborted is False
    assert result.completed_steps == 2
    assert say_calls == ["hello"]
    assert sleep_calls == []
    assert did_overlap == [True]


def test_capture_off_pauses_execution_and_snaps_back_on_resume(tmp_path):
    say_calls: List[str] = []

    class FakeClient:
        def __init__(self, base_url: str, timeout_s: float) -> None:
            self.base_url = base_url

        def script_say(self, text: str, timeout_s=None):
            say_calls.append(text)
            return {"ok": True}

        def script_do(self, payload: Dict[str, Any], timeout_s=None):
            return {"ok": True}

    class FakePpt:
        def __init__(self) -> None:
            self.position = {"slide": 1, "build": 0}
            self.next_calls = 0
            self.prev_calls = 0
            self.goto_calls: List[Tuple[int, int | None]] = []

        def open_and_start_slideshow(self, file_path: str, fullscreen_required: bool = True) -> None:
            return None

        def next_build(self) -> None:
            self.next_calls += 1
            self.position["build"] += 1

        def prev_build(self) -> None:
            self.prev_calls += 1
            self.position["build"] = max(0, self.position["build"] - 1)

        def goto(self, slide: int, build=None) -> None:
            self.goto_calls.append((int(slide), int(build) if build is not None else None))
            self.position["slide"] = int(slide)
            self.position["build"] = int(build) if build is not None else 0

        def get_position(self):
            return dict(self.position)

        def is_fullscreen_slideshow(self) -> bool:
            return True

    script = _script_template()
    script["ppt"] = {
        "enabled": True,
        "file": "C:/slides/demo.pptx",
        "fullscreen_required": True,
        "start_capture_on_run": False,
    }
    script["steps"] = [
        {
            "id": "s1",
            "robot_id": "nao1",
            "start": {"mode": "manual", "delay_s": 0},
            "request_timeout_s": 12,
            "on_error": "prompt",
            "action": {"type": "say", "text": "hello"},
        }
    ]

    answers = iter(["", "c", ""])
    runner = ScriptRunner(
        script,
        client_factory=lambda url, timeout_s: FakeClient(url, timeout_s),  # type: ignore[arg-type]
        input_func=lambda _p: next(answers),
        sleep_func=lambda _s: None,
        log_dir=tmp_path,
        ppt_controller=FakePpt(),
    )
    result = runner.run()
    assert result.aborted is False
    assert result.completed_steps == 1
    assert say_calls == ["hello"]

    log_text = result.log_path.read_text(encoding="utf-8")
    assert "[CAPTURE] OFF" in log_text
    assert "[CAPTURE] ON" in log_text
    assert "[PPT] snapback to slide=1 build=0" in log_text
    assert "[PPT] operator next -> slide=1 build=1" in log_text


def test_ppt_actions_call_controller_methods(tmp_path):
    class FakeClient:
        def __init__(self, base_url: str, timeout_s: float) -> None:
            self.base_url = base_url

        def script_say(self, text: str, timeout_s=None):
            return {"ok": True}

        def script_do(self, payload: Dict[str, Any], timeout_s=None):
            return {"ok": True}

    class FakePpt:
        def __init__(self) -> None:
            self.position = {"slide": 1, "build": 0}
            self.calls: List[Tuple[str, int, int | None]] = []

        def open_and_start_slideshow(self, file_path: str, fullscreen_required: bool = True) -> None:
            return None

        def next_build(self) -> None:
            self.calls.append(("next_build", self.position["slide"], self.position["build"]))
            self.position["build"] += 1

        def prev_build(self) -> None:
            self.calls.append(("prev_build", self.position["slide"], self.position["build"]))
            self.position["build"] = max(0, self.position["build"] - 1)

        def goto(self, slide: int, build=None) -> None:
            self.calls.append(("goto", int(slide), int(build) if build is not None else None))
            self.position["slide"] = int(slide)
            self.position["build"] = int(build) if build is not None else 0

        def get_position(self):
            return dict(self.position)

        def is_fullscreen_slideshow(self) -> bool:
            return True

    script = _script_template()
    script["ppt"] = {
        "enabled": True,
        "file": "C:/slides/demo.pptx",
        "fullscreen_required": True,
        "start_capture_on_run": True,
    }
    script["steps"] = [
        {"id": "p1", "start": {"mode": "after_prev", "delay_s": 0}, "request_timeout_s": 12, "on_error": "prompt", "action": {"type": "ppt", "mode": "next_build"}},
        {"id": "p2", "start": {"mode": "with_prev", "delay_s": 0}, "request_timeout_s": 12, "on_error": "prompt", "action": {"type": "ppt", "mode": "prev_build"}},
        {"id": "p3", "start": {"mode": "after_prev", "delay_s": 0}, "request_timeout_s": 12, "on_error": "prompt", "action": {"type": "ppt", "mode": "goto", "slide": 3, "build": 2}},
    ]

    fake_ppt = FakePpt()
    runner = ScriptRunner(
        script,
        client_factory=lambda url, timeout_s: FakeClient(url, timeout_s),  # type: ignore[arg-type]
        input_func=lambda _p: "",
        sleep_func=lambda _s: None,
        log_dir=tmp_path,
        ppt_controller=fake_ppt,
    )
    result = runner.run()
    assert result.aborted is False
    assert result.completed_steps == 3
    assert fake_ppt.calls[0][0] == "next_build"
    assert fake_ppt.calls[1][0] == "prev_build"
    assert fake_ppt.calls[2] == ("goto", 3, 2)


def test_sync_mismatch_triggers_hard_pause_and_resume(tmp_path):
    say_calls: List[str] = []

    class FakeClient:
        def __init__(self, base_url: str, timeout_s: float) -> None:
            self.base_url = base_url

        def script_say(self, text: str, timeout_s=None):
            say_calls.append(text)
            return {"ok": True}

        def script_do(self, payload: Dict[str, Any], timeout_s=None):
            return {"ok": True}

    class FakePpt:
        def __init__(self) -> None:
            self.position = {"slide": 1, "build": 0}
            self._position_reads = 0

        def open_and_start_slideshow(self, file_path: str, fullscreen_required: bool = True) -> None:
            return None

        def next_build(self) -> None:
            self.position["build"] += 1

        def prev_build(self) -> None:
            self.position["build"] = max(0, self.position["build"] - 1)

        def goto(self, slide: int, build=None) -> None:
            self.position["slide"] = int(slide)
            self.position["build"] = int(build) if build is not None else 0

        def get_position(self):
            self._position_reads += 1
            if self._position_reads == 2:
                return {"slide": 2, "build": 0}
            return dict(self.position)

        def is_fullscreen_slideshow(self) -> bool:
            return True

    script = _script_template()
    script["ppt"] = {
        "enabled": True,
        "file": "C:/slides/demo.pptx",
        "fullscreen_required": True,
        "start_capture_on_run": True,
    }
    script["steps"] = [
        {
            "id": "s1",
            "robot_id": "nao1",
            "start": {"mode": "after_prev", "delay_s": 0},
            "request_timeout_s": 12,
            "on_error": "prompt",
            "action": {"type": "say", "text": "hello"},
        }
    ]

    answers = iter(["c"])
    runner = ScriptRunner(
        script,
        client_factory=lambda url, timeout_s: FakeClient(url, timeout_s),  # type: ignore[arg-type]
        input_func=lambda _p: next(answers),
        sleep_func=lambda _s: None,
        log_dir=tmp_path,
        ppt_controller=FakePpt(),
    )
    result = runner.run()
    assert result.aborted is False
    assert result.completed_steps == 1
    assert say_calls == ["hello"]

    log_text = result.log_path.read_text(encoding="utf-8")
    assert "[SYNC] mismatch detected -> hard pause" in log_text
    assert "[PPT] snapback to slide=1 build=0" in log_text


def test_run_with_ppt_on_non_windows_fails_with_clear_message(tmp_path, monkeypatch):
    class FakeClient:
        def __init__(self, base_url: str, timeout_s: float) -> None:
            self.base_url = base_url

        def script_say(self, text: str, timeout_s=None):
            return {"ok": True}

        def script_do(self, payload: Dict[str, Any], timeout_s=None):
            return {"ok": True}

    script = _script_template()
    script["ppt"] = {
        "enabled": True,
        "file": "C:/slides/demo.pptx",
        "fullscreen_required": True,
        "start_capture_on_run": True,
    }
    monkeypatch.setattr("py3_script_runner.runner.sys.platform", "linux")

    runner = ScriptRunner(
        script,
        client_factory=lambda url, timeout_s: FakeClient(url, timeout_s),  # type: ignore[arg-type]
        input_func=lambda _p: "",
        sleep_func=lambda _s: None,
        log_dir=tmp_path,
    )
    with pytest.raises(RuntimeError, match="PPT feature requires Windows \\+ PowerPoint COM"):
        runner.run()
