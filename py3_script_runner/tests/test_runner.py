from __future__ import annotations

from typing import Any, Dict, List, Tuple

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
