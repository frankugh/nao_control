from __future__ import annotations

from types import SimpleNamespace

from dialog.behavior_executor import BehaviorExecutor
from dialog.interfaces import LLMResult

import webapp_server


class StubLLMBackend:
    def generate(self, messages):
        return LLMResult(reply="ok", messages=list(messages))


class StubSTTBackend:
    def transcribe(self, audio):  # pragma: no cover
        raise AssertionError("STT not used in these tests")


class _Resp:
    def __init__(self, payload, status_code=200) -> None:
        self._payload = payload
        self.status_code = status_code
        self.ok = status_code < 400
        self.content = b"{}"

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"status={self.status_code}")


def _make_app(monkeypatch, *, behavior_executor=None):
    base_pipeline = SimpleNamespace(
        llm=StubLLMBackend(),
        output=None,
        status_to_console=True,
        system_prompt="SYSTEM",
        log_messages_path=None,
        log_meta={},
        max_history_turns=None,
        _behavior_executor=behavior_executor,
        _cmdrec=None,
        _debug_cmdrec=False,
    )
    monkeypatch.setattr(webapp_server, "build_pipeline_from_config", lambda *_a, **_k: base_pipeline)
    monkeypatch.setattr(webapp_server, "make_stt_backend_from_config", lambda *_a, **_k: StubSTTBackend())
    app, _, _ = webapp_server.create_app(cfg={}, config_path="<memory>")
    return app


def _dm_events(client, *, event=None):
    endpoint = "/api/dm_events?limit=200"
    if event:
        endpoint += f"&event={event}"
    return client.get(endpoint).get_json()["events"]


def test_auto_rest_suspend_acquire_conflict_release_and_runtime_health(monkeypatch):
    app = _make_app(monkeypatch)
    client = app.test_client()

    acquire = client.post(
        "/api/auto_rest_suspend/acquire",
        json={"lease_id": "lease-1", "owner": "script_runner", "reason": "script_run", "ttl_s": 30},
    )
    assert acquire.status_code == 200
    acquire_payload = acquire.get_json()
    assert acquire_payload["ok"] is True
    assert acquire_payload["lease_id"] == "lease-1"

    conflict = client.post(
        "/api/auto_rest_suspend/acquire",
        json={"lease_id": "lease-2", "owner": "other_runner", "reason": "script_run", "ttl_s": 30},
    )
    assert conflict.status_code == 409
    conflict_payload = conflict.get_json()
    assert conflict_payload["ok"] is False
    assert conflict_payload["active_lease"]["owner"] == "script_runner"

    health = client.post(
        "/api/runtime_health",
        json={
            "nao_auto_rest_after_s": 180,
            "base_enabled": False,
            "behavior_enabled": False,
            "nao_ip_enabled": False,
        },
    )
    assert health.status_code == 200
    auto_rest = health.get_json()["auto_rest"]
    assert auto_rest["enabled_by_config"] is True
    assert auto_rest["suspended"] is True
    assert auto_rest["suspend_owner"] == "script_runner"
    assert auto_rest["lease_expires_at"]

    release = client.post("/api/auto_rest_suspend/release", json={"lease_id": "lease-1"})
    assert release.status_code == 200
    assert release.get_json()["timer_reset"] is True

    health_after = client.post(
        "/api/runtime_health",
        json={
            "nao_auto_rest_after_s": 180,
            "base_enabled": False,
            "behavior_enabled": False,
            "nao_ip_enabled": False,
        },
    )
    assert health_after.get_json()["auto_rest"]["suspended"] is False


def test_auto_rest_suspend_renew_failure_is_logged(monkeypatch):
    app = _make_app(monkeypatch)
    client = app.test_client()
    client.post("/api/dm_events_clear", json={})

    resp = client.post(
        "/api/auto_rest_suspend/renew",
        json={"lease_id": "missing", "owner": "script_runner", "ttl_s": 30},
    )
    assert resp.status_code == 409

    events = _dm_events(client, event="auto_rest_suspend_renew_failed")
    assert len(events) == 1
    assert events[0]["data"]["reason"] == "lease_not_active"


def test_auto_rest_tick_is_suppressed_once_and_release_resets_timer(monkeypatch):
    app = _make_app(monkeypatch)
    client = app.test_client()
    client.post("/api/dm_events_clear", json={})

    post_calls = []

    def fake_get(url, timeout=0, **_kwargs):
        if url.endswith("/ping"):
            return _Resp({"status": "ok"})
        return _Resp({"status": "ok"})

    def fake_post(url, json=None, timeout=0, **_kwargs):
        post_calls.append(url)
        return _Resp({"status": "ok"})

    monkeypatch.setattr(webapp_server.requests, "get", fake_get)
    monkeypatch.setattr(webapp_server.requests, "post", fake_post)

    cfg_resp = client.post(
        "/api/runtime_config",
        json={
            "config": {
                "nao_auto_rest_after_s": 10,
                "nao_base_url": "http://base:5000",
                "base_enabled": True,
                "behavior_enabled": False,
            }
        },
    )
    assert cfg_resp.status_code == 200
    post_calls.clear()

    acquire = client.post(
        "/api/auto_rest_suspend/acquire",
        json={"lease_id": "lease-1", "owner": "script_runner", "reason": "script_run", "ttl_s": 30},
    )
    assert acquire.status_code == 200

    app._auto_rest_debug_touch_activity(activate_auto_rest=True)
    app._auto_rest_debug_set_last_activity_ago(20)
    app._auto_rest_debug_tick()
    app._auto_rest_debug_tick()

    assert post_calls == []
    suppressed = _dm_events(client, event="auto_rest_idle_suppressed")
    assert len(suppressed) == 1

    release = client.post("/api/auto_rest_suspend/release", json={"lease_id": "lease-1"})
    assert release.status_code == 200

    app._auto_rest_debug_tick()
    assert post_calls == []

    app._auto_rest_debug_set_last_activity_ago(20)
    app._auto_rest_debug_tick()
    assert post_calls == ["http://base:5000/rest"]


def test_auto_rest_tick_does_not_repeat_rest_when_robot_is_already_resting(monkeypatch):
    app = _make_app(monkeypatch)
    client = app.test_client()
    client.post("/api/dm_events_clear", json={})

    post_calls = []
    awake_calls = []

    def fake_get(url, timeout=0, **_kwargs):
        if url.endswith("/ping"):
            return _Resp({"status": "ok"})
        if url.endswith("/is_awake"):
            awake_calls.append(url)
            return _Resp({"status": "ok", "data": {"is_awake": False}})
        return _Resp({"status": "ok"})

    def fake_post(url, json=None, timeout=0, **_kwargs):
        post_calls.append(url)
        return _Resp({"status": "ok", "data": "NAO already resting"})

    monkeypatch.setattr(webapp_server.requests, "get", fake_get)
    monkeypatch.setattr(webapp_server.requests, "post", fake_post)

    cfg_resp = client.post(
        "/api/runtime_config",
        json={
            "config": {
                "nao_auto_rest_after_s": 10,
                "nao_base_url": "http://base:5000",
                "base_enabled": True,
                "behavior_enabled": False,
            }
        },
    )
    assert cfg_resp.status_code == 200
    post_calls.clear()

    app._auto_rest_debug_touch_activity(activate_auto_rest=True)
    app._auto_rest_debug_set_last_activity_ago(20)
    app._auto_rest_debug_tick()
    app._auto_rest_debug_tick()

    assert post_calls == []
    assert len(awake_calls) == 1
    events = _dm_events(client, event="auto_rest_idle_already_resting")
    assert len(events) == 1


def test_auto_rest_tick_does_not_repeat_rest_if_awake_probe_fails_after_successful_idle_rest(monkeypatch):
    app = _make_app(monkeypatch)
    client = app.test_client()

    post_calls = []
    awake_probe_count = {"value": 0}

    def fake_get(url, timeout=0, **_kwargs):
        if url.endswith("/ping"):
            return _Resp({"status": "ok"})
        if url.endswith("/is_awake"):
            awake_probe_count["value"] += 1
            if awake_probe_count["value"] == 1:
                return _Resp({"status": "ok", "data": {"is_awake": True}})
            raise RuntimeError("is_awake tijdelijk niet beschikbaar")
        return _Resp({"status": "ok"})

    def fake_post(url, json=None, timeout=0, **_kwargs):
        post_calls.append(url)
        return _Resp({"status": "ok", "data": "NAO resting"})

    monkeypatch.setattr(webapp_server.requests, "get", fake_get)
    monkeypatch.setattr(webapp_server.requests, "post", fake_post)

    cfg_resp = client.post(
        "/api/runtime_config",
        json={
            "config": {
                "nao_auto_rest_after_s": 10,
                "nao_base_url": "http://base:5000",
                "base_enabled": True,
                "behavior_enabled": False,
            }
        },
    )
    assert cfg_resp.status_code == 200
    post_calls.clear()

    app._auto_rest_debug_touch_activity(activate_auto_rest=True)
    app._auto_rest_debug_set_last_activity_ago(20)
    app._auto_rest_debug_tick()
    app._auto_rest_debug_tick()
    app._auto_rest_debug_tick()

    assert post_calls == ["http://base:5000/rest"]


def test_explicit_rest_disarms_timer_and_explicit_wake_reactivates_it(monkeypatch):
    app = _make_app(monkeypatch)
    client = app.test_client()

    robot_state = {"is_awake": True}
    post_calls = []

    def fake_get(url, timeout=0, **_kwargs):
        if url.endswith("/ping"):
            return _Resp({"status": "ok"})
        if url.endswith("/is_awake"):
            return _Resp({"status": "ok", "data": {"is_awake": robot_state["is_awake"]}})
        return _Resp({"status": "ok"})

    def fake_post(url, json=None, timeout=0, **_kwargs):
        post_calls.append(url)
        if url.endswith("/wake_up"):
            robot_state["is_awake"] = True
            return _Resp({"status": "ok", "data": "NAO woken up"})
        if url.endswith("/rest"):
            robot_state["is_awake"] = False
            return _Resp({"status": "ok", "data": "NAO resting"})
        return _Resp({"status": "ok"})

    monkeypatch.setattr(webapp_server.requests, "get", fake_get)
    monkeypatch.setattr(webapp_server.requests, "post", fake_post)

    cfg_resp = client.post(
        "/api/runtime_config",
        json={
            "config": {
                "nao_auto_rest_after_s": 10,
                "nao_base_url": "http://base:5000",
                "base_enabled": True,
                "behavior_enabled": False,
            }
        },
    )
    assert cfg_resp.status_code == 200
    post_calls.clear()

    rest_resp = client.post("/api/nao_rest")
    assert rest_resp.status_code == 200
    health_rest = client.post(
        "/api/runtime_health",
        json={"nao_auto_rest_after_s": 10, "base_enabled": True, "behavior_enabled": False, "nao_base_url": "http://base:5000"},
    )
    auto_rest_rest = health_rest.get_json()["auto_rest"]
    assert auto_rest_rest["timer_active"] is False
    assert auto_rest_rest["seconds_until_rest"] == 10

    app._auto_rest_debug_set_last_activity_ago(20)
    app._auto_rest_debug_tick()
    assert post_calls == ["http://base:5000/rest"]

    wake_resp = client.post("/api/nao_wake_up")
    assert wake_resp.status_code == 200
    health_wake = client.post(
        "/api/runtime_health",
        json={"nao_auto_rest_after_s": 10, "base_enabled": True, "behavior_enabled": False, "nao_base_url": "http://base:5000"},
    )
    auto_rest_wake = health_wake.get_json()["auto_rest"]
    assert auto_rest_wake["timer_active"] is True
    assert 9 <= auto_rest_wake["seconds_until_rest"] <= 10

    app._auto_rest_debug_set_last_activity_ago(20)
    app._auto_rest_debug_tick()
    assert post_calls == [
        "http://base:5000/rest",
        "http://base:5000/wake_up",
        "http://base:5000/rest",
    ]


def test_touch_activity_during_rest_does_not_reactivate_timer(monkeypatch):
    app = _make_app(monkeypatch)
    client = app.test_client()

    robot_state = {"is_awake": True}
    post_calls = []

    def fake_get(url, timeout=0, **_kwargs):
        if url.endswith("/ping"):
            return _Resp({"status": "ok"})
        if url.endswith("/is_awake"):
            return _Resp({"status": "ok", "data": {"is_awake": robot_state["is_awake"]}})
        return _Resp({"status": "ok"})

    def fake_post(url, json=None, timeout=0, **_kwargs):
        post_calls.append(url)
        if url.endswith("/rest"):
            robot_state["is_awake"] = False
            return _Resp({"status": "ok", "data": "NAO resting"})
        return _Resp({"status": "ok"})

    monkeypatch.setattr(webapp_server.requests, "get", fake_get)
    monkeypatch.setattr(webapp_server.requests, "post", fake_post)

    cfg_resp = client.post(
        "/api/runtime_config",
        json={
            "config": {
                "nao_auto_rest_after_s": 10,
                "nao_base_url": "http://base:5000",
                "base_enabled": True,
                "behavior_enabled": False,
            }
        },
    )
    assert cfg_resp.status_code == 200
    post_calls.clear()

    rest_resp = client.post("/api/nao_rest")
    assert rest_resp.status_code == 200

    app._auto_rest_debug_touch_activity(activate_auto_rest=False)
    health_after_touch = client.post(
        "/api/runtime_health",
        json={"nao_auto_rest_after_s": 10, "base_enabled": True, "behavior_enabled": False, "nao_base_url": "http://base:5000"},
    )
    auto_rest_after_touch = health_after_touch.get_json()["auto_rest"]
    assert auto_rest_after_touch["timer_active"] is False
    assert auto_rest_after_touch["seconds_until_rest"] == 10

    app._auto_rest_debug_set_last_activity_ago(20)
    app._auto_rest_debug_tick()
    assert post_calls == ["http://base:5000/rest"]


def test_manual_rest_uses_dm_awake_helper_when_base_endpoint_exists(monkeypatch):
    class FailingExecutor:
        def execute(self, cmd):  # pragma: no cover - should not run
            raise AssertionError(f"Behavior executor should not handle {cmd.label}")

    app = _make_app(monkeypatch, behavior_executor=FailingExecutor())
    client = app.test_client()

    post_calls = []

    def fake_get(url, timeout=0, **_kwargs):
        if url.endswith("/ping"):
            return _Resp({"status": "ok"})
        if url.endswith("/is_awake"):
            return _Resp({"status": "ok", "data": {"is_awake": False}})
        return _Resp({"status": "ok"})

    def fake_post(url, json=None, timeout=0, **_kwargs):
        post_calls.append(url)
        if url.endswith("/rest"):
            return _Resp({"status": "ok", "data": "NAO resting"})
        return _Resp({"status": "ok"})

    monkeypatch.setattr(webapp_server.requests, "get", fake_get)
    monkeypatch.setattr(webapp_server.requests, "post", fake_post)

    cfg_resp = client.post(
        "/api/runtime_config",
        json={
            "config": {
                "nao_auto_rest_after_s": 10,
                "nao_base_url": "http://base:5000",
                "base_enabled": True,
                "behavior_enabled": False,
            }
        },
    )
    assert cfg_resp.status_code == 200
    post_calls.clear()

    resp = client.post("/api/command_execute", json={"label": "REST"})
    assert resp.status_code == 200
    assert post_calls == ["http://base:5000/rest"]

    health = client.post(
        "/api/runtime_health",
        json={"nao_auto_rest_after_s": 10, "base_enabled": True, "behavior_enabled": False, "nao_base_url": "http://base:5000"},
    )
    auto_rest = health.get_json()["auto_rest"]
    assert auto_rest["timer_active"] is False
    assert auto_rest["seconds_until_rest"] == 10


def test_manual_stand_up_from_rest_uses_dm_wake_helper_once(monkeypatch):
    executor = BehaviorExecutor(base_url="http://base:5000", timeout_s=1.0)
    app = _make_app(monkeypatch, behavior_executor=executor)
    client = app.test_client()

    robot_state = {"is_awake": False}
    post_calls = []

    def fake_get(url, timeout=0, **_kwargs):
        if url.endswith("/ping"):
            return _Resp({"status": "ok"})
        if url.endswith("/is_awake"):
            return _Resp({"status": "ok", "data": {"is_awake": robot_state["is_awake"]}})
        return _Resp({"status": "ok"})

    def fake_post(url, json=None, timeout=0, **_kwargs):
        post_calls.append(url)
        if url.endswith("/wake_up"):
            robot_state["is_awake"] = True
            return _Resp({"status": "ok", "data": "NAO woken up"})
        if url.endswith("/do_behavior"):
            return _Resp({"status": "ok", "data": {"behavior": "basic/standup", "ran": True}})
        return _Resp({"status": "ok"})

    monkeypatch.setattr(webapp_server.requests, "get", fake_get)
    monkeypatch.setattr(webapp_server.requests, "post", fake_post)
    monkeypatch.setattr("dialog.behavior_executor.requests.post", fake_post)

    cfg_resp = client.post(
        "/api/runtime_config",
        json={
            "config": {
                "nao_auto_rest_after_s": 10,
                "nao_base_url": "http://base:5000",
                "base_enabled": True,
                "behavior_enabled": False,
            }
        },
    )
    assert cfg_resp.status_code == 200
    post_calls.clear()

    resp = client.post("/api/command_execute", json={"label": "STAND_UP"})
    assert resp.status_code == 200
    assert post_calls == [
        "http://base:5000/wake_up",
        "http://base:5000/do_behavior",
    ]

    health = client.post(
        "/api/runtime_health",
        json={"nao_auto_rest_after_s": 10, "base_enabled": True, "behavior_enabled": False, "nao_base_url": "http://base:5000"},
    )
    auto_rest = health.get_json()["auto_rest"]
    assert auto_rest["timer_active"] is True
    assert 9 <= auto_rest["seconds_until_rest"] <= 10

def test_idle_auto_rest_appends_operator_message_to_existing_chat_sessions(monkeypatch):
    app = _make_app(monkeypatch)
    client = app.test_client()

    robot_state = {"is_awake": True}

    def fake_get(url, timeout=0, **_kwargs):
        if url.endswith("/ping"):
            return _Resp({"status": "ok"})
        if url.endswith("/is_awake"):
            return _Resp({"status": "ok", "data": {"is_awake": robot_state["is_awake"]}})
        return _Resp({"status": "ok"})

    def fake_post(url, json=None, timeout=0, **_kwargs):
        if url.endswith("/rest"):
            robot_state["is_awake"] = False
            return _Resp({"status": "ok", "data": "NAO resting"})
        return _Resp({"status": "ok"})

    monkeypatch.setattr(webapp_server.requests, "get", fake_get)
    monkeypatch.setattr(webapp_server.requests, "post", fake_post)

    cfg_resp = client.post(
        "/api/runtime_config",
        json={
            "config": {
                "nao_auto_rest_after_s": 10,
                "nao_base_url": "http://base:5000",
                "base_enabled": True,
                "behavior_enabled": False,
            }
        },
    )
    assert cfg_resp.status_code == 200

    state_before = client.get("/api/state")
    assert state_before.status_code == 200
    assert state_before.get_json()["history"] == []

    app._auto_rest_debug_touch_activity(activate_auto_rest=True)
    app._auto_rest_debug_set_last_activity_ago(20)
    app._auto_rest_debug_tick()

    state_after = client.get("/api/state")
    history = state_after.get_json()["history"]
    assert history
    assert history[-1]["role"] == "assistant"
    assert history[-1]["content"] == "NAO ging automatisch in ruststand ter bescherming van de motoren."
