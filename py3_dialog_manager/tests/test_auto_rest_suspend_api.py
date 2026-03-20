from __future__ import annotations

from types import SimpleNamespace

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

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"status={self.status_code}")


def _make_app(monkeypatch):
    base_pipeline = SimpleNamespace(
        llm=StubLLMBackend(),
        output=None,
        status_to_console=True,
        system_prompt="SYSTEM",
        log_messages_path=None,
        log_meta={},
        max_history_turns=None,
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

    acquire = client.post(
        "/api/auto_rest_suspend/acquire",
        json={"lease_id": "lease-1", "owner": "script_runner", "reason": "script_run", "ttl_s": 30},
    )
    assert acquire.status_code == 200

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

    app._auto_rest_debug_set_last_activity_ago(20)
    app._auto_rest_debug_tick()
    app._auto_rest_debug_tick()

    assert post_calls == []
    assert len(awake_calls) == 2
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

    app._auto_rest_debug_set_last_activity_ago(20)
    app._auto_rest_debug_tick()
    app._auto_rest_debug_tick()
    app._auto_rest_debug_tick()

    assert post_calls == ["http://base:5000/rest"]
