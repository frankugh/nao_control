from __future__ import annotations

from typing import Any, Dict, List, Tuple
import threading
import time

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


def test_preflight_ignores_robot_runtime_config_if_raw_script_bypasses_schema(tmp_path):
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
    assert ("set_runtime_config", "http://dm-nao1:5301") not in calls
    assert ("capabilities", "http://dm-nao1:5301") in calls


def test_preflight_acquires_auto_rest_suspend_and_run_releases_it(tmp_path):
    calls: List[Tuple[str, str, str]] = []

    class FakeClient:
        def __init__(self, base_url: str, timeout_s: float) -> None:
            self.base_url = base_url

        def auto_rest_suspend_acquire(self, *, lease_id, owner, reason, ttl_s, timeout_s=None):
            calls.append(("acquire", self.base_url, str(lease_id)))
            return {"ok": True}

        def auto_rest_suspend_renew(self, *, lease_id, owner, ttl_s, timeout_s=None):
            return {"ok": True}

        def auto_rest_suspend_release(self, *, lease_id, timeout_s=None):
            calls.append(("release", self.base_url, str(lease_id)))
            return {"ok": True}

        def capabilities(self, timeout_s=None):
            return {"ok": True, "supports": {"say": True}}

        def runtime_effective(self, timeout_s=None):
            return {"ok": True, "runtime_config": {"base_enabled": False, "behavior_enabled": False, "nao_ip_enabled": False}}

        def runtime_health(self, payload, timeout_s=None):
            return {"ok": True, "base": {"ping": True, "nao_ping": True}, "behavior": {"ping": True, "nao_ping": True}, "nao": {"ping": True}}

    script = _script_template()
    runner = ScriptRunner(
        script,
        client_factory=lambda url, timeout_s: FakeClient(url, timeout_s),  # type: ignore[arg-type]
        input_func=lambda _p: "",
        sleep_func=lambda _s: None,
        log_dir=tmp_path,
    )
    runner.preflight()
    result = runner.run()

    assert result.aborted is False
    assert [action for (action, _url, _lease_id) in calls] == ["acquire", "acquire", "release", "release"]


def test_preflight_releases_acquired_auto_rest_suspend_on_acquire_failure(tmp_path):
    calls: List[Tuple[str, str]] = []

    class FakeClient:
        def __init__(self, base_url: str, timeout_s: float) -> None:
            self.base_url = base_url

        def auto_rest_suspend_acquire(self, *, lease_id, owner, reason, ttl_s, timeout_s=None):
            calls.append(("acquire", self.base_url))
            if self.base_url.endswith(":5302"):
                raise DMClientError("409 conflict")
            return {"ok": True}

        def auto_rest_suspend_release(self, *, lease_id, timeout_s=None):
            calls.append(("release", self.base_url))
            return {"ok": True}

        def capabilities(self, timeout_s=None):
            return {"ok": True, "supports": {"say": True}}

        def runtime_effective(self, timeout_s=None):
            return {"ok": True, "runtime_config": {"base_enabled": False, "behavior_enabled": False, "nao_ip_enabled": False}}

        def runtime_health(self, payload, timeout_s=None):
            return {"ok": True, "base": {"ping": True, "nao_ping": True}, "behavior": {"ping": True, "nao_ping": True}, "nao": {"ping": True}}

    script = _script_template()
    runner = ScriptRunner(
        script,
        client_factory=lambda url, timeout_s: FakeClient(url, timeout_s),  # type: ignore[arg-type]
        input_func=lambda _p: "",
        sleep_func=lambda _s: None,
        log_dir=tmp_path,
    )

    with pytest.raises(RuntimeError, match="auto-rest suspend kon niet worden geactiveerd"):
        runner.preflight()

    assert ("acquire", "http://dm-nao1:5301") in calls
    assert ("acquire", "http://dm-nao2:5302") in calls
    assert ("release", "http://dm-nao1:5301") in calls


def test_run_fails_when_auto_rest_suspend_lease_cannot_be_renewed(tmp_path):
    class FakeClient:
        def __init__(self, base_url: str, timeout_s: float) -> None:
            self.base_url = base_url

        def auto_rest_suspend_acquire(self, *, lease_id, owner, reason, ttl_s, timeout_s=None):
            return {"ok": True}

        def auto_rest_suspend_renew(self, *, lease_id, owner, ttl_s, timeout_s=None):
            raise DMClientError("renew timeout")

        def auto_rest_suspend_release(self, *, lease_id, timeout_s=None):
            return {"ok": True}

    script = _script_template()
    script["robots"] = {"nao1": {"dm_url": "http://dm-nao1:5301"}}
    script["defaults"]["control_poll_interval_s"] = 0.01
    script["steps"] = [
        {
            "id": "pause_1",
            "robot_id": "nao1",
            "start": {"mode": "after_prev", "delay_s": 0},
            "action": {"type": "pause", "seconds": 0.2},
        }
    ]
    runner = ScriptRunner(
        script,
        client_factory=lambda url, timeout_s: FakeClient(url, timeout_s),  # type: ignore[arg-type]
        input_func=lambda _p: "",
        sleep_func=time.sleep,
        log_dir=tmp_path,
    )
    runner.auto_rest_suspend_ttl_s = 0.05
    runner.auto_rest_suspend_renew_interval_s = 0.01

    with pytest.raises(RuntimeError, match="auto-rest suspend lease ging verloren"):
        runner.run()


def test_auto_rest_suspend_ttl_can_be_configured_from_script_defaults(tmp_path):
    acquire_ttls: List[float] = []

    class FakeClient:
        def __init__(self, base_url: str, timeout_s: float) -> None:
            self.base_url = base_url

        def auto_rest_suspend_acquire(self, *, lease_id, owner, reason, ttl_s, timeout_s=None):
            acquire_ttls.append(float(ttl_s))
            return {"ok": True}

        def auto_rest_suspend_renew(self, *, lease_id, owner, ttl_s, timeout_s=None):
            return {"ok": True}

        def auto_rest_suspend_release(self, *, lease_id, timeout_s=None):
            return {"ok": True}

        def capabilities(self, timeout_s=None):
            return {"ok": True, "supports": {"say": True}}

        def runtime_effective(self, timeout_s=None):
            return {"ok": True, "runtime_config": {"base_enabled": False, "behavior_enabled": False, "nao_ip_enabled": False}}

        def runtime_health(self, payload, timeout_s=None):
            return {"ok": True, "base": {"ping": True, "nao_ping": True}, "behavior": {"ping": True, "nao_ping": True}, "nao": {"ping": True}}

    script = _script_template()
    script["robots"] = {"nao1": {"dm_url": "http://dm-nao1:5301"}}
    script["defaults"]["auto_rest_suspend_ttl_s"] = 123.0
    runner = ScriptRunner(
        script,
        client_factory=lambda url, timeout_s: FakeClient(url, timeout_s),  # type: ignore[arg-type]
        input_func=lambda _p: "",
        sleep_func=lambda _s: None,
        log_dir=tmp_path,
    )

    runner.preflight()

    assert runner.auto_rest_suspend_ttl_s == 123.0
    assert acquire_ttls == [123.0]


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
    assert "Robot nao2: WAIT - base connector down (" in log_text
    assert "nao_base_url=http://127.0.0.1:5101" in log_text
    assert "nao_ip_enabled=false" in log_text
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


def test_preflight_acquires_auto_rest_only_after_readiness_recovers(tmp_path):
    sleep_calls: List[float] = []
    call_order: List[Tuple[str, Any]] = []
    cap_calls = 0

    class FakeClient:
        def __init__(self, base_url: str, timeout_s: float) -> None:
            self.base_url = base_url

        def capabilities(self, timeout_s=None):
            nonlocal cap_calls
            cap_calls += 1
            call_order.append(("capabilities", cap_calls))
            if cap_calls == 1:
                raise DMClientError("connection refused")
            return {"ok": True, "supports": {"say": True}}

        def runtime_effective(self, timeout_s=None):
            call_order.append(("runtime_effective", self.base_url))
            return {
                "ok": True,
                "runtime_config": {
                    "base_enabled": False,
                    "behavior_enabled": False,
                    "nao_ip_enabled": False,
                },
            }

        def runtime_health(self, payload, timeout_s=None):
            call_order.append(("runtime_health", self.base_url))
            return {
                "ok": True,
                "base": {"ping": True, "nao_ping": True},
                "behavior": {"ping": True, "nao_ping": True},
                "nao": {"ping": True},
            }

        def auto_rest_suspend_acquire(self, *, lease_id, owner, reason, ttl_s, timeout_s=None):
            call_order.append(("acquire", self.base_url))
            assert cap_calls >= 2
            return {"ok": True}

        def auto_rest_suspend_renew(self, *, lease_id, owner, ttl_s, timeout_s=None):
            return {"ok": True}

        def auto_rest_suspend_release(self, *, lease_id, timeout_s=None):
            call_order.append(("release", self.base_url))
            return {"ok": True}

    script = _script_template()
    script["robots"] = {"nao1": {"dm_url": "http://dm-nao1:5301"}}
    runner = ScriptRunner(
        script,
        client_factory=lambda url, timeout_s: FakeClient(url, timeout_s),  # type: ignore[arg-type]
        input_func=lambda _p: "",
        sleep_func=lambda s: sleep_calls.append(float(s)),
        log_dir=tmp_path,
    )

    runner.preflight()
    runner.close()

    assert sleep_calls, "expected readiness polling to wait at least once"
    assert call_order == [
        ("capabilities", 1),
        ("capabilities", 2),
        ("runtime_effective", "http://dm-nao1:5301"),
        ("runtime_health", "http://dm-nao1:5301"),
        ("acquire", "http://dm-nao1:5301"),
        ("release", "http://dm-nao1:5301"),
    ]


def test_preflight_timeout_when_dm_unreachable_does_not_try_auto_rest_acquire(tmp_path, monkeypatch):
    sleep_calls: List[float] = []
    calls: List[Tuple[str, str]] = []
    clock = {"now": 100.0}

    def fake_monotonic() -> float:
        return float(clock["now"])

    monkeypatch.setattr("py3_script_runner.runner.time.monotonic", fake_monotonic)

    class FakeClient:
        def __init__(self, base_url: str, timeout_s: float) -> None:
            self.base_url = base_url

        def capabilities(self, timeout_s=None):
            calls.append(("capabilities", self.base_url))
            raise DMClientError("connection refused")

        def auto_rest_suspend_acquire(self, *, lease_id, owner, reason, ttl_s, timeout_s=None):
            calls.append(("acquire", self.base_url))
            return {"ok": True}

    script = _script_template()
    script["robots"] = {"nao1": {"dm_url": "http://dm-nao1:5301"}}
    script["defaults"]["readiness_timeout_s"] = 5.0
    script["defaults"]["readiness_poll_interval_s"] = 1.0
    runner = ScriptRunner(
        script,
        client_factory=lambda url, timeout_s: FakeClient(url, timeout_s),  # type: ignore[arg-type]
        input_func=lambda _p: "",
        sleep_func=lambda s: sleep_calls.append(float(s)) or clock.__setitem__("now", clock["now"] + float(s)),
        log_dir=tmp_path,
    )

    with pytest.raises(RuntimeError, match="Readiness timeout"):
        runner.preflight()

    assert sleep_calls, "expected readiness retry loop before timing out"
    assert ("capabilities", "http://dm-nao1:5301") in calls
    assert ("acquire", "http://dm-nao1:5301") not in calls
    log_text = runner.log_path.read_text(encoding="utf-8")
    assert "Robot nao1: WAIT - DM down (" in log_text


def test_preflight_does_not_require_base_nao_ping_when_nao_is_disabled(tmp_path):
    class FakeClient:
        def __init__(self, base_url: str, timeout_s: float) -> None:
            self.base_url = base_url

        def capabilities(self, timeout_s=None):
            return {"ok": True, "supports": {"say": True}}

        def runtime_effective(self, timeout_s=None):
            return {
                "ok": True,
                "runtime_config": {
                    "base_enabled": True,
                    "behavior_enabled": False,
                    "nao_ip_enabled": False,
                    "nao_base_url": "http://127.0.0.1:5101",
                },
            }

        def runtime_health(self, payload, timeout_s=None):
            return {
                "ok": True,
                "base": {"ping": True, "nao_ping": False},
                "behavior": {"ping": False, "nao_ping": False},
                "nao": {"ping": False},
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

    log_text = runner.log_path.read_text(encoding="utf-8")
    assert "base->NAO down" not in log_text
    assert "Readiness complete: all robots ready." in log_text


def test_preflight_reports_runtime_health_timeout_with_enabled_dependency_context(tmp_path):
    class FakeClient:
        def __init__(self, base_url: str, timeout_s: float) -> None:
            self.base_url = base_url

        def capabilities(self, timeout_s=None):
            return {"ok": True, "supports": {"say": True}}

        def runtime_effective(self, timeout_s=None):
            return {
                "ok": True,
                "runtime_config": {
                    "base_enabled": True,
                    "behavior_enabled": False,
                    "nao_ip_enabled": False,
                    "nao_base_url": "http://127.0.0.1:5101",
                },
            }

        def runtime_health(self, payload, timeout_s=None):
            raise DMClientError(
                "POST http://127.0.0.1:5301/api/runtime_health request failed: "
                "HTTPConnectionPool(host='127.0.0.1', port=5301): Read timed out. (read timeout=3.0)"
            )

    script = _script_template()
    runner = ScriptRunner(
        script,
        client_factory=lambda url, timeout_s: FakeClient(url, timeout_s),  # type: ignore[arg-type]
        input_func=lambda _p: "",
        sleep_func=lambda _s: None,
        log_dir=tmp_path,
    )

    ready, status, _info = runner._check_robot_ready("nao1")

    assert ready is False
    assert "runtime_health timed out while checking base connector" in status
    assert "nao_base_url=http://127.0.0.1:5101" in status
    assert "NAO TCP is disabled, but base connector is still enabled" in status


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


def test_runner_can_suppress_stdout_but_keep_log_file_and_events(tmp_path, capsys):
    events: List[Dict[str, Any]] = []

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

    runner = ScriptRunner(
        script,
        client_factory=lambda url, timeout_s: FakeClient(url, timeout_s),  # type: ignore[arg-type]
        input_func=lambda _prompt: "",
        sleep_func=lambda _s: None,
        log_dir=tmp_path,
        event_sink=lambda event: events.append(dict(event)),
        log_to_stdout=False,
    )
    result = runner.run()

    captured = capsys.readouterr()
    log_text = result.log_path.read_text(encoding="utf-8")
    assert captured.out == ""
    assert "[1/1] s1: executing" in log_text
    assert any(str(event.get("type") or "") == "log" for event in events)


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


def test_summary_start_can_run_async_and_emits_summary_state(tmp_path):
    events: List[Dict[str, Any]] = []
    calls: List[Tuple[str, str]] = []

    class FakeClient:
        def __init__(self, base_url: str, timeout_s: float) -> None:
            self.base_url = base_url

        def summary_page_url(self):
            return self.base_url + "/summary"

        def summary_start(self, timeout_s=None):
            calls.append(("summary_start", self.base_url))
            return {
                "ok": True,
                "session": {
                    "session_id": "summary-123",
                    "status": "capturing",
                    "last_error": None,
                },
            }

        def script_say(self, text: str, timeout_s=None):
            return {"ok": True}

    script = _script_template()
    script["steps"] = [
        {
            "id": "summary_async",
            "robot_id": "nao1",
            "start": {"mode": "after_prev", "delay_s": 0},
            "request_timeout_s": 12,
            "on_error": "prompt",
            "action": {
                "type": "do",
                "mode": "summary_start",
                "wait_for_complete": False,
                "open_on_new_tab": True,
            },
        }
    ]
    runner = ScriptRunner(
        script,
        client_factory=lambda url, timeout_s: FakeClient(url, timeout_s),  # type: ignore[arg-type]
        input_func=lambda _p: "",
        sleep_func=lambda _s: None,
        log_dir=tmp_path,
        event_sink=lambda event: events.append(dict(event)),
    )
    result = runner.run()
    assert result.aborted is False
    assert result.completed_steps == 1
    assert calls == [("summary_start", "http://dm-nao1:5301")]
    summary_event = next(event for event in events if event.get("type") == "summary_state")
    assert summary_event["summary_active"] is True
    assert summary_event["summary_waiting"] is False
    assert summary_event["summary_url"] == "http://dm-nao1:5301/summary"
    assert summary_event["summary_session_id"] == "summary-123"
    assert summary_event["summary_status"] == "capturing"
    assert summary_event["summary_open_nonce"] == 1


def test_summary_start_waits_until_completed(tmp_path):
    events: List[Dict[str, Any]] = []
    summary_get_calls: List[str] = []

    class FakeClient:
        def __init__(self, base_url: str, timeout_s: float) -> None:
            self.base_url = base_url
            self._poll_count = 0

        def summary_page_url(self):
            return self.base_url + "/summary"

        def summary_start(self, timeout_s=None):
            return {
                "ok": True,
                "session": {
                    "session_id": "summary-123",
                    "status": "capturing",
                    "last_error": None,
                },
            }

        def summary_get(self, timeout_s=None):
            summary_get_calls.append(self.base_url)
            self._poll_count += 1
            status = "completed" if self._poll_count >= 2 else "capturing"
            return {
                "ok": True,
                "session": {
                    "session_id": "summary-123",
                    "status": status,
                    "last_error": None,
                },
            }

        def script_say(self, text: str, timeout_s=None):
            return {"ok": True}

    script = _script_template()
    script["steps"] = [
        {
            "id": "summary_wait",
            "robot_id": "nao1",
            "start": {"mode": "after_prev", "delay_s": 0},
            "request_timeout_s": 12,
            "on_error": "prompt",
            "action": {"type": "do", "mode": "summary_start", "wait_for_complete": True},
        }
    ]
    runner = ScriptRunner(
        script,
        client_factory=lambda url, timeout_s: FakeClient(url, timeout_s),  # type: ignore[arg-type]
        input_func=lambda _p: "",
        sleep_func=lambda _s: None,
        log_dir=tmp_path,
        event_sink=lambda event: events.append(dict(event)),
    )
    result = runner.run()
    assert result.aborted is False
    assert result.completed_steps == 1
    assert len(summary_get_calls) >= 2
    waiting_events = [event for event in events if event.get("type") == "waiting"]
    assert waiting_events[-1]["waiting_reason"] == "summary_wait"
    summary_events = [event for event in events if event.get("type") == "summary_state"]
    assert summary_events[-1]["summary_active"] is False
    assert summary_events[-1]["summary_status"] == "completed"


def test_summary_start_aborted_is_handled_by_on_error_policy(tmp_path):
    say_calls: List[str] = []

    class FakeClient:
        def __init__(self, base_url: str, timeout_s: float) -> None:
            self.base_url = base_url

        def summary_page_url(self):
            return self.base_url + "/summary"

        def summary_start(self, timeout_s=None):
            return {"ok": True, "session": {"session_id": "summary-123", "status": "capturing", "last_error": None}}

        def summary_get(self, timeout_s=None):
            return {"ok": True, "session": {"session_id": "summary-123", "status": "aborted", "last_error": None}}

        def script_say(self, text: str, timeout_s=None):
            say_calls.append(text)
            return {"ok": True}

    script = _script_template()
    script["steps"] = [
        {
            "id": "summary_wait",
            "robot_id": "nao1",
            "start": {"mode": "after_prev", "delay_s": 0},
            "request_timeout_s": 12,
            "on_error": "continue",
            "action": {"type": "do", "mode": "summary_start", "wait_for_complete": True},
        },
        {
            "id": "after_summary",
            "robot_id": "nao1",
            "start": {"mode": "after_prev", "delay_s": 0},
            "request_timeout_s": 12,
            "on_error": "prompt",
            "action": {"type": "say", "text": "na summary"},
        },
    ]
    runner = ScriptRunner(
        script,
        client_factory=lambda url, timeout_s: FakeClient(url, timeout_s),  # type: ignore[arg-type]
        input_func=lambda _p: "",
        sleep_func=lambda _s: None,
        log_dir=tmp_path,
        on_error_prompt_policy="next",
    )
    result = runner.run()
    assert result.aborted is False
    assert result.completed_steps == 1
    assert say_calls == ["na summary"]


def test_summary_start_wait_survives_poll_connection_failures(tmp_path):
    events: List[Dict[str, Any]] = []

    class FakeClient:
        def __init__(self, base_url: str, timeout_s: float) -> None:
            self.base_url = base_url
            self._poll_count = 0

        def summary_page_url(self):
            return self.base_url + "/summary"

        def summary_start(self, timeout_s=None):
            return {"ok": True, "session": {"session_id": "summary-123", "status": "capturing", "last_error": None}}

        def summary_get(self, timeout_s=None):
            self._poll_count += 1
            if self._poll_count == 1:
                raise DMClientError("temporary connection lost")
            return {"ok": True, "session": {"session_id": "summary-123", "status": "completed", "last_error": None}}

        def script_say(self, text: str, timeout_s=None):
            return {"ok": True}

    script = _script_template()
    script["steps"] = [
        {
            "id": "summary_wait",
            "robot_id": "nao1",
            "start": {"mode": "after_prev", "delay_s": 0},
            "request_timeout_s": 12,
            "on_error": "prompt",
            "action": {"type": "do", "mode": "summary_start", "wait_for_complete": True},
        }
    ]
    runner = ScriptRunner(
        script,
        client_factory=lambda url, timeout_s: FakeClient(url, timeout_s),  # type: ignore[arg-type]
        input_func=lambda _p: "",
        sleep_func=lambda _s: None,
        log_dir=tmp_path,
        event_sink=lambda event: events.append(dict(event)),
    )
    result = runner.run()
    assert result.aborted is False
    assert result.completed_steps == 1
    summary_events = [event for event in events if event.get("type") == "summary_state"]
    assert any(event["summary_connection_ok"] is False for event in summary_events)
    assert summary_events[-1]["summary_status"] == "completed"


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

        def next_slide(self) -> None:
            self.next_calls += 1
            self.position["build"] += 1

        def previous_slide(self) -> None:
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
    assert "[PPT] operator next slide -> slide=1 build=1" in log_text


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

        def next_slide(self) -> None:
            self.calls.append(("next_slide", self.position["slide"], self.position["build"]))
            self.position["build"] += 1

        def previous_slide(self) -> None:
            self.calls.append(("previous_slide", self.position["slide"], self.position["build"]))
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
        {"id": "p1", "start": {"mode": "after_prev", "delay_s": 0}, "request_timeout_s": 12, "on_error": "prompt", "action": {"type": "ppt", "mode": "next_slide"}},
        {"id": "p2", "start": {"mode": "with_prev", "delay_s": 0}, "request_timeout_s": 12, "on_error": "prompt", "action": {"type": "ppt", "mode": "previous_slide"}},
        {"id": "p3", "start": {"mode": "after_prev", "delay_s": 0}, "request_timeout_s": 12, "on_error": "prompt", "action": {"type": "ppt", "mode": "goto", "slide": 3, "click": 2}},
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
    assert fake_ppt.calls[0] == ("goto", 1, 0)
    assert fake_ppt.calls[1][0] == "next_slide"
    assert fake_ppt.calls[2][0] == "previous_slide"
    assert fake_ppt.calls[3] == ("goto", 3, 2)


def test_new_run_resets_ppt_to_start_before_first_step(tmp_path):
    class FakeClient:
        def __init__(self, base_url: str, timeout_s: float) -> None:
            self.base_url = base_url

        def script_say(self, text: str, timeout_s=None):
            return {"ok": True}

        def script_do(self, payload: Dict[str, Any], timeout_s=None):
            return {"ok": True}

    class FakePpt:
        def __init__(self) -> None:
            self.position = {"slide": 4, "build": 2}
            self.calls: List[Tuple[str, int, int | None]] = []

        def open_and_start_slideshow(self, file_path: str, fullscreen_required: bool = True) -> None:
            return None

        def next_slide(self) -> None:
            self.calls.append(("next_slide", self.position["slide"], self.position["build"]))
            self.position["slide"] += 1
            self.position["build"] = 0

        def previous_slide(self) -> None:
            self.calls.append(("previous_slide", self.position["slide"], self.position["build"]))
            self.position["slide"] = max(1, self.position["slide"] - 1)
            self.position["build"] = 0

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
        {
            "id": "p1",
            "start": {"mode": "after_prev", "delay_s": 0},
            "request_timeout_s": 12,
            "on_error": "prompt",
            "action": {"type": "ppt", "mode": "next_slide"},
        }
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
    assert result.completed_steps == 1
    assert fake_ppt.calls[0] == ("goto", 1, 0)
    assert fake_ppt.calls[1] == ("next_slide", 1, 0)


def test_with_prev_group_keeps_ppt_on_runner_thread(tmp_path):
    say_calls: List[Tuple[str, int]] = []

    class FakeClient:
        def __init__(self, base_url: str, timeout_s: float) -> None:
            self.base_url = base_url

        def script_say(self, text: str, timeout_s=None):
            say_calls.append((text, threading.get_ident()))
            return {"ok": True}

        def script_do(self, payload: Dict[str, Any], timeout_s=None):
            return {"ok": True}

    class FakePpt:
        def __init__(self) -> None:
            self.position = {"slide": 1, "build": 0}
            self.owner_thread: int | None = None
            self.calls: List[Tuple[str, int]] = []

        def open_and_start_slideshow(self, file_path: str, fullscreen_required: bool = True) -> None:
            self.owner_thread = threading.get_ident()

        def next_slide(self) -> None:
            current_thread = threading.get_ident()
            if current_thread != self.owner_thread:
                raise RuntimeError(
                    f"ppt thread mismatch: owner={self.owner_thread} current={current_thread}"
                )
            self.calls.append(("next_slide", current_thread))
            self.position["slide"] += 1
            self.position["build"] = 0

        def previous_slide(self) -> None:
            self.position["slide"] = max(1, self.position["slide"] - 1)
            self.position["build"] = 0

        def goto(self, slide: int, build=None) -> None:
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
        {
            "id": "p1",
            "start": {"mode": "after_prev", "delay_s": 0},
            "request_timeout_s": 12,
            "on_error": "prompt",
            "action": {"type": "ppt", "mode": "next_slide"},
        },
        {
            "id": "r1",
            "robot_id": "nao1",
            "start": {"mode": "with_prev"},
            "request_timeout_s": 12,
            "on_error": "prompt",
            "action": {"type": "say", "text": "hello"},
        },
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
    assert result.completed_steps == 2
    assert fake_ppt.owner_thread is not None
    assert fake_ppt.calls == [("next_slide", fake_ppt.owner_thread)]
    assert len(say_calls) == 1
    assert say_calls[0][0] == "hello"


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

        def next_slide(self) -> None:
            self.position["build"] += 1

        def previous_slide(self) -> None:
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


def test_do_mode_nao_set_eye_color_uses_nao_endpoint(tmp_path):
    eye_calls: List[Dict[str, Any]] = []

    class FakeClient:
        def __init__(self, base_url: str, timeout_s: float) -> None:
            self.base_url = base_url

        def script_say(self, text: str, timeout_s=None):
            return {"ok": True}

        def script_do(self, payload: Dict[str, Any], timeout_s=None):
            return {"ok": True}

        def nao_set_eye_color(self, color: str, duration=None, timeout_s=None):
            eye_calls.append(
                {
                    "base_url": self.base_url,
                    "color": color,
                    "duration": duration,
                    "timeout_s": timeout_s,
                }
            )
            return {"ok": True, "status": "accepted", "action": "nao_set_eye_color"}

    script = _script_template()
    script["steps"] = [
        {
            "id": "eye1",
            "robot_id": "nao1",
            "start": {"mode": "after_prev", "delay_s": 0},
            "request_timeout_s": 12,
            "on_error": "prompt",
            "action": {
                "type": "do",
                "mode": "nao_set_eye_color",
                "color": "#00AEEF",
                "duration": 0.35,
            },
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
    assert eye_calls == [
        {
            "base_url": "http://dm-nao1:5301",
            "color": "#00AEEF",
            "duration": 0.35,
            "timeout_s": 12.0,
        }
    ]


def test_on_error_prompt_policy_next_skips_without_stdin_prompt(tmp_path):
    prompts: List[str] = []

    class FakeClient:
        def __init__(self, base_url: str, timeout_s: float) -> None:
            self.base_url = base_url

        def runtime_effective(self, timeout_s=None):
            return {"ok": True, "runtime_config": {}}

        def script_say(self, text: str, timeout_s=None):
            raise DMClientError("boom")

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
            "action": {"type": "say", "text": "fails"},
        }
    ]

    runner = ScriptRunner(
        script,
        client_factory=lambda url, timeout_s: FakeClient(url, timeout_s),  # type: ignore[arg-type]
        input_func=lambda prompt: prompts.append(prompt) or "",
        sleep_func=lambda _s: None,
        log_dir=tmp_path,
        on_error_prompt_policy="next",
    )
    result = runner.run()
    assert result.aborted is False
    assert result.completed_steps == 0
    assert prompts == []


def test_summary_start_without_open_tab_does_not_emit_open_nonce(tmp_path):
    events: List[Dict[str, Any]] = []

    class FakeClient:
        def __init__(self, base_url: str, timeout_s: float) -> None:
            self.base_url = base_url

        def summary_page_url(self):
            return self.base_url + "/summary"

        def summary_start(self, timeout_s=None):
            return {"ok": True, "session": {"session_id": "summary-123", "status": "capturing", "last_error": None}}

        def script_say(self, text: str, timeout_s=None):
            return {"ok": True}

    script = _script_template()
    script["steps"] = [
        {
            "id": "summary_async",
            "robot_id": "nao1",
            "start": {"mode": "after_prev", "delay_s": 0},
            "request_timeout_s": 12,
            "on_error": "prompt",
            "action": {"type": "do", "mode": "summary_start", "wait_for_complete": False, "open_on_new_tab": False},
        }
    ]

    runner = ScriptRunner(
        script,
        client_factory=lambda url, timeout_s: FakeClient(url, timeout_s),  # type: ignore[arg-type]
        input_func=lambda _p: "",
        sleep_func=lambda _s: None,
        log_dir=tmp_path,
        event_sink=lambda event: events.append(dict(event)),
    )
    result = runner.run()
    assert result.aborted is False
    assert result.completed_steps == 1
    summary_event = next(event for event in events if event.get("type") == "summary_state")
    assert "summary_open_nonce" not in summary_event


def test_ppt_mismatch_policy_defer_snapback(tmp_path):
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
            self.position_reads = 0
            self.goto_calls: List[Tuple[int, int | None]] = []

        def open_and_start_slideshow(self, file_path: str, fullscreen_required: bool = True) -> None:
            return None

        def next_slide(self) -> None:
            self.position["build"] += 1

        def previous_slide(self) -> None:
            self.position["build"] = max(0, self.position["build"] - 1)

        def goto(self, slide: int, build=None) -> None:
            self.goto_calls.append((int(slide), int(build) if build is not None else None))
            self.position["slide"] = int(slide)
            self.position["build"] = int(build) if build is not None else 0

        def get_position(self):
            self.position_reads += 1
            if self.position_reads == 2:
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
    fake_ppt = FakePpt()
    runner = ScriptRunner(
        script,
        client_factory=lambda url, timeout_s: FakeClient(url, timeout_s),  # type: ignore[arg-type]
        input_func=lambda _p: "",
        sleep_func=lambda _s: None,
        log_dir=tmp_path,
        ppt_controller=fake_ppt,
        ppt_mismatch_policy="defer_snapback",
    )
    result = runner.run()
    assert result.aborted is False
    assert result.completed_steps == 1
    assert fake_ppt.goto_calls == [(1, 0), (1, 0)]
    log_text = result.log_path.read_text(encoding="utf-8")
    assert "defer snapback at next script step" in log_text
    assert "applying deferred snapback before next step" in log_text


def test_abort_event_stops_manual_wait_with_continue_event(tmp_path):
    class FakeClient:
        def __init__(self, base_url: str, timeout_s: float) -> None:
            self.base_url = base_url

        def script_say(self, text: str, timeout_s=None):
            return {"ok": True}

        def script_do(self, payload: Dict[str, Any], timeout_s=None):
            return {"ok": True}

    script = _script_template()
    script["defaults"]["control_poll_interval_s"] = 0.01
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
    continue_event = threading.Event()
    abort_event = threading.Event()
    runner = ScriptRunner(
        script,
        client_factory=lambda url, timeout_s: FakeClient(url, timeout_s),  # type: ignore[arg-type]
        input_func=lambda _p: "",
        sleep_func=time.sleep,
        log_dir=tmp_path,
        continue_event=continue_event,
        abort_event=abort_event,
    )

    result_box: Dict[str, Any] = {}

    def _run() -> None:
        result_box["result"] = runner.run()

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    time.sleep(0.05)
    abort_event.set()
    thread.join(timeout=2.0)
    assert not thread.is_alive()
    result = result_box["result"]
    assert result.aborted is True
    assert result.completed_steps == 0


def test_say_step_uses_preloaded_audio_when_available(tmp_path):
    calls: List[Dict[str, Any]] = []
    preload_root = tmp_path / "preloaded"
    clip_path = preload_root / "clips" / "fp" / "demo.wav"
    clip_path.parent.mkdir(parents=True)
    clip_path.write_bytes(b"RIFFdemo")

    class FakeClient:
        def __init__(self, base_url: str, timeout_s: float) -> None:
            self.base_url = base_url

        def script_say(self, text: str, *, timeout_s=None, preloaded_audio_b64=None, preloaded_audio_format=None):
            calls.append(
                {
                    "text": text,
                    "timeout_s": timeout_s,
                    "preloaded_audio_b64": preloaded_audio_b64,
                    "preloaded_audio_format": preloaded_audio_format,
                }
            )
            return {"ok": True, "status": "accepted", "action": "say", "preloaded_audio": bool(preloaded_audio_b64)}

        def script_do(self, payload: Dict[str, Any], timeout_s=None):
            return {"ok": True}

    script = _script_template()
    script["steps"] = [
        {
            "id": "say_1",
            "robot_id": "nao1",
            "start": {"mode": "after_prev", "delay_s": 0},
            "request_timeout_s": 12,
            "on_error": "prompt",
            "action": {"type": "say", "text": "offline welkom"},
        }
    ]

    runner = ScriptRunner(
        script,
        client_factory=lambda url, timeout_s: FakeClient(url, timeout_s),  # type: ignore[arg-type]
        input_func=lambda _p: "",
        sleep_func=lambda _s: None,
        log_dir=tmp_path,
        tts_preload_root=preload_root,
        tts_preload_step_audio={
            "say_1": {
                "clip_id": "clip-1",
                "clip_rel_path": "clips/fp/demo.wav",
                "profile_fingerprint": "fp",
            }
        },
        tts_preload_robot_modes={"nao1": "preloaded_current"},
    )

    result = runner.run()
    assert result.completed_steps == 1
    assert len(calls) == 1
    assert calls[0]["text"] == "offline welkom"
    assert calls[0]["preloaded_audio_b64"]
    assert calls[0]["preloaded_audio_format"] == "wav"


def test_say_step_falls_back_to_live_when_preloaded_playback_fails(tmp_path):
    calls: List[Dict[str, Any]] = []
    preload_root = tmp_path / "preloaded"
    clip_path = preload_root / "clips" / "fp" / "demo.wav"
    clip_path.parent.mkdir(parents=True)
    clip_path.write_bytes(b"RIFFdemo")

    class FakeClient:
        def __init__(self, base_url: str, timeout_s: float) -> None:
            self.base_url = base_url

        def script_say(self, text: str, *, timeout_s=None, preloaded_audio_b64=None, preloaded_audio_format=None):
            calls.append({"text": text, "preloaded": bool(preloaded_audio_b64)})
            if preloaded_audio_b64:
                raise DMClientError("preloaded playback failed")
            return {"ok": True, "status": "accepted", "action": "say"}

        def script_do(self, payload: Dict[str, Any], timeout_s=None):
            return {"ok": True}

    script = _script_template()
    script["steps"] = [
        {
            "id": "say_1",
            "robot_id": "nao1",
            "start": {"mode": "after_prev", "delay_s": 0},
            "request_timeout_s": 12,
            "on_error": "prompt",
            "action": {"type": "say", "text": "fallback welkom"},
        }
    ]

    runner = ScriptRunner(
        script,
        client_factory=lambda url, timeout_s: FakeClient(url, timeout_s),  # type: ignore[arg-type]
        input_func=lambda _p: "",
        sleep_func=lambda _s: None,
        log_dir=tmp_path,
        tts_preload_root=preload_root,
        tts_preload_step_audio={
            "say_1": {
                "clip_id": "clip-1",
                "clip_rel_path": "clips/fp/demo.wav",
                "profile_fingerprint": "fp",
            }
        },
        tts_preload_robot_modes={"nao1": "preloaded_current"},
    )

    result = runner.run()
    assert result.completed_steps == 1
    assert calls == [
        {"text": "fallback welkom", "preloaded": True},
        {"text": "fallback welkom", "preloaded": False},
    ]
