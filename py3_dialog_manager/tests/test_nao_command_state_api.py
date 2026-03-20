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


def test_nao_command_state_includes_posture_auto_rest_and_warning(monkeypatch):
    app = _make_app(monkeypatch)
    client = app.test_client()

    def fake_get(url, timeout=0, **_kwargs):
        if url.endswith("/is_awake"):
            return _Resp({"status": "ok", "data": {"is_awake": False}})
        if url.endswith("/custom_life_state"):
            return _Resp(
                {
                    "status": "ok",
                    "data": {
                        "life_state": "solitary",
                        "modules": {
                            "basic_awareness": True,
                            "background_movement": True,
                            "breathing": True,
                        },
                    },
                }
            )
        if url.endswith("/posture"):
            return _Resp({"status": "ok", "data": {"posture": "Sitting", "is_sitting": True, "is_standing": False}})
        raise AssertionError(f"unexpected GET {url}")

    monkeypatch.setattr(webapp_server.requests, "get", fake_get)

    cfg_resp = client.post(
        "/api/runtime_config",
        json={
            "config": {
                "nao_base_url": "http://base:5000",
                "base_enabled": True,
                "behavior_enabled": False,
                "custom_life_enabled": True,
                "nao_auto_rest_after_s": 180,
            }
        },
    )
    assert cfg_resp.status_code == 200
    app._auto_rest_debug_touch_activity(activate_auto_rest=True)
    app._auto_rest_debug_set_last_activity_ago(3)

    resp = client.get("/api/nao_command_state")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["reachable"] is True
    assert data["awake"]["is_awake"] is False
    assert data["custom_life"]["enabled"] is True
    assert data["posture"]["posture"] == "Sitting"
    assert data["auto_rest"]["enabled_by_config"] is True
    assert data["auto_rest"]["timeout_s"] == 180.0
    assert data["auto_rest"]["timer_active"] is True
    assert 176 <= data["auto_rest"]["seconds_until_rest"] <= 177
    assert data["warnings"]
    assert "posture" in data["warnings"][0].lower()


def test_nao_command_state_reports_suspended_auto_rest(monkeypatch):
    app = _make_app(monkeypatch)
    client = app.test_client()

    def fake_get(url, timeout=0, **_kwargs):
        if url.endswith("/is_awake"):
            return _Resp({"status": "ok", "data": {"is_awake": True}})
        if url.endswith("/custom_life_state"):
            return _Resp(
                {
                    "status": "ok",
                    "data": {
                        "life_state": "disabled",
                        "modules": {
                            "basic_awareness": False,
                            "background_movement": False,
                            "breathing": False,
                        },
                    },
                }
            )
        if url.endswith("/posture"):
            return _Resp({"status": "ok", "data": {"posture": "Stand", "is_sitting": False, "is_standing": True}})
        raise AssertionError(f"unexpected GET {url}")

    monkeypatch.setattr(webapp_server.requests, "get", fake_get)

    cfg_resp = client.post(
        "/api/runtime_config",
        json={
            "config": {
                "nao_base_url": "http://base:5000",
                "base_enabled": True,
                "behavior_enabled": False,
                "custom_life_enabled": False,
                "nao_auto_rest_after_s": 180,
            }
        },
    )
    assert cfg_resp.status_code == 200
    acquire = client.post(
        "/api/auto_rest_suspend/acquire",
        json={"lease_id": "lease-1", "owner": "script_runner", "reason": "script_run", "ttl_s": 30},
    )
    assert acquire.status_code == 200

    resp = client.get("/api/nao_command_state")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["auto_rest"]["suspended"] is True
    assert data["auto_rest"]["suspend_owner"] == "script_runner"
    assert data["auto_rest"]["seconds_until_rest"] is None


def test_nao_command_state_starts_with_disarmed_auto_rest_timer(monkeypatch):
    app = _make_app(monkeypatch)
    client = app.test_client()

    def fake_get(url, timeout=0, **_kwargs):
        if url.endswith("/is_awake"):
            return _Resp({"status": "ok", "data": {"is_awake": False}})
        if url.endswith("/custom_life_state"):
            return _Resp({"status": "ok", "data": {"life_state": "disabled", "modules": {}}})
        if url.endswith("/posture"):
            return _Resp({"status": "ok", "data": {"posture": "Sitting", "is_sitting": True, "is_standing": False}})
        raise AssertionError(f"unexpected GET {url}")

    monkeypatch.setattr(webapp_server.requests, "get", fake_get)

    cfg_resp = client.post(
        "/api/runtime_config",
        json={
            "config": {
                "nao_base_url": "http://base:5000",
                "base_enabled": True,
                "behavior_enabled": False,
                "nao_auto_rest_after_s": 180,
            }
        },
    )
    assert cfg_resp.status_code == 200

    resp = client.get("/api/nao_command_state")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["auto_rest"]["timer_active"] is False
    assert data["auto_rest"]["seconds_until_rest"] == 180
