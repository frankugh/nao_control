from __future__ import annotations

import threading
from types import SimpleNamespace

import requests

from dialog.interfaces import LLMResult

import webapp_server


class StubLLMBackend:
    def generate(self, messages):
        return LLMResult(reply="ok", messages=list(messages))


class StubSTTBackend:
    def transcribe(self, audio):  # pragma: no cover
        raise AssertionError("STT not used in these tests")


class StubExecutor:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def execute(self, cmd) -> None:
        self.calls.append(str(getattr(cmd, "label", "")))


class BlockingExecutor:
    def __init__(self, *, blocking_label: str) -> None:
        self.blocking_label = str(blocking_label or "").upper()
        self.calls: list[str] = []
        self.started = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()

    def execute(self, cmd) -> None:
        label = str(getattr(cmd, "label", "") or "")
        self.calls.append(label)
        if label.upper() != self.blocking_label:
            return
        self.started.set()
        try:
            if not self.release.wait(timeout=5):
                raise AssertionError("blocking executor release wait timed out")
        finally:
            self.finished.set()


class TimeoutExecutor:
    def __init__(self, *, timeout_label: str) -> None:
        self.timeout_label = str(timeout_label or "").upper()
        self.calls: list[str] = []
        self.started = threading.Event()

    def execute(self, cmd) -> None:
        label = str(getattr(cmd, "label", "") or "")
        self.calls.append(label)
        if label.upper() == self.timeout_label:
            self.started.set()
            raise RuntimeError(
                "HTTPConnectionPool(host='127.0.0.1', port=5101): "
                "Read timed out. (read timeout=12.0)"
            )


class StubCmdrec:
    def __init__(self, labels):
        self._labels = list(labels)

    def get_labels(self):
        return list(self._labels)


class _Resp:
    def __init__(self, payload, status_code=200) -> None:
        self._payload = payload
        self.status_code = status_code
        self.ok = status_code < 400

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"status={self.status_code}")


def _make_app(monkeypatch, *, executor=None, cmdrec=None):
    base_pipeline = SimpleNamespace(
        llm=StubLLMBackend(),
        output=None,
        status_to_console=True,
        system_prompt="SYSTEM",
        log_messages_path=None,
        log_meta={},
        max_history_turns=None,
        _behavior_executor=executor,
        _cmdrec=cmdrec,
        _debug_cmdrec=False,
    )
    monkeypatch.setattr(webapp_server, "build_pipeline_from_config", lambda *_a, **_k: base_pipeline)
    monkeypatch.setattr(webapp_server, "make_stt_backend_from_config", lambda *_a, **_k: StubSTTBackend())
    app, _, _ = webapp_server.create_app(cfg={}, config_path="<memory>")
    return app


def test_nao_stop_audio_prefers_behavior_endpoint(monkeypatch):
    calls = []

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        return _Resp({"status": "ok", "data": {"actions": {"tts_stop_called": True}}})

    monkeypatch.setattr(webapp_server.requests, "post", fake_post)
    app = _make_app(monkeypatch)
    client = app.test_client()

    cfg_resp = client.post(
        "/api/runtime_config",
        json={
            "config": {
                "behavior_enabled": True,
                "behavior_manager_url": "http://behavior:5001",
                "base_enabled": True,
                "nao_base_url": "http://base:5000",
            }
        },
    )
    assert cfg_resp.status_code == 200

    resp = client.post("/api/nao_stop_audio")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["mode"] == "behavior"
    assert calls[-1][0] == "http://behavior:5001/nao/stop_audio"


def test_nao_stop_audio_uses_base_endpoint_when_behavior_disabled(monkeypatch):
    calls = []

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        return _Resp({"status": "ok", "data": {"actions": {"tts_stop_called": True}}})

    monkeypatch.setattr(webapp_server.requests, "post", fake_post)
    app = _make_app(monkeypatch)
    client = app.test_client()

    cfg_resp = client.post(
        "/api/runtime_config",
        json={
            "config": {
                "behavior_enabled": False,
                "behavior_manager_url": "http://behavior:5001",
                "base_enabled": True,
                "nao_base_url": "http://base:5000",
            }
        },
    )
    assert cfg_resp.status_code == 200

    resp = client.post("/api/nao_stop_audio")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["mode"] == "base"
    assert calls[-1][0] == "http://base:5000/stop_audio"


def test_command_execute_dance_is_marked_as_stoppable(monkeypatch):
    executor = StubExecutor()
    app = _make_app(monkeypatch, executor=executor)
    client = app.test_client()

    resp = client.post(
        "/api/command_execute",
        json={"label": "DANCE", "resolved": {"dance_key": "happy", "dance_behavior": "dances/happy"}},
    )
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["command_stop_available"] is True
    assert data["command_stop_label"] == "DANCE"
    assert executor.calls == ["DANCE"]


def test_command_execute_stop_stops_audio_and_clears_stop_state(monkeypatch):
    calls = []
    executor = StubExecutor()

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        return _Resp({"status": "ok", "data": {"actions": {"tts_stop_called": True}}})

    monkeypatch.setattr(webapp_server.requests, "post", fake_post)
    app = _make_app(monkeypatch, executor=executor)
    client = app.test_client()

    cfg_resp = client.post(
        "/api/runtime_config",
        json={
            "config": {
                "behavior_enabled": False,
                "base_enabled": True,
                "nao_base_url": "http://base:5000",
            }
        },
    )
    assert cfg_resp.status_code == 200

    # Seed a stoppable command first.
    walk_resp = client.post(
        "/api/command_execute",
        json={"label": "WALK_WITH_ME"},
    )
    assert walk_resp.status_code == 200
    assert walk_resp.get_json()["command_stop_available"] is True
    assert walk_resp.get_json()["command_stop_label"] == "WALK_WITH_ME"

    stop_resp = client.post("/api/command_execute", json={"label": "STOP"})
    assert stop_resp.status_code == 200
    stop_data = stop_resp.get_json()
    assert stop_data["ok"] is True
    assert stop_data["command_stop_available"] is False
    assert stop_data["command_stop_label"] is None
    assert executor.calls[-2:] == ["STOP", "STAND_UP"]
    assert calls[-1][0] == "http://base:5000/stop_audio"


def test_manual_stop_during_blocking_walk_does_not_restore_stop_state(monkeypatch):
    executor = BlockingExecutor(blocking_label="WALK_WITH_ME")
    app = _make_app(monkeypatch, executor=executor)
    client_start = app.test_client(use_cookies=False)
    client_stop = app.test_client(use_cookies=False)
    headers = {"Cookie": "sid=stop-race"}
    result_holder: dict[str, object] = {}

    def _start_request() -> None:
        result_holder["response"] = client_start.post(
            "/api/command_execute",
            json={"label": "WALK_WITH_ME"},
            headers=headers,
        )

    worker = threading.Thread(target=_start_request)
    worker.start()
    assert executor.started.wait(timeout=2), "blocking WALK_WITH_ME did not start"

    mid_state = client_stop.get("/api/state", headers=headers)
    assert mid_state.status_code == 200
    mid_data = mid_state.get_json()
    assert mid_data["command_stop_available"] is True
    assert mid_data["command_stop_label"] == "WALK_WITH_ME"

    stop_resp = client_stop.post("/api/command_execute", json={"label": "STOP"}, headers=headers)
    assert stop_resp.status_code == 200
    stop_data = stop_resp.get_json()
    assert stop_data["ok"] is True
    assert stop_data["command_stop_available"] is False
    assert stop_data["command_stop_label"] is None

    executor.release.set()
    worker.join(timeout=5)
    assert not worker.is_alive(), "blocking WALK_WITH_ME request did not finish"

    start_resp = result_holder["response"]
    assert start_resp.status_code == 200
    start_data = start_resp.get_json()
    assert start_data["ok"] is True
    assert start_data["command_stop_available"] is False
    assert start_data["command_stop_label"] is None

    end_state = client_stop.get("/api/state", headers=headers)
    assert end_state.status_code == 200
    end_data = end_state.get_json()
    assert end_data["command_stop_available"] is False
    assert end_data["command_stop_label"] is None


def test_script_do_walk_with_me_returns_before_blocking_behavior_finishes(monkeypatch):
    executor = BlockingExecutor(blocking_label="WALK_WITH_ME")
    app = _make_app(monkeypatch, executor=executor)
    client = app.test_client(use_cookies=False)
    headers = {"Cookie": "sid=script-walk"}

    start_resp = client.post(
        "/api/script/do",
        json={"mode": "command", "label": "WALK_WITH_ME"},
        headers=headers,
    )
    assert start_resp.status_code == 200
    start_data = start_resp.get_json()
    assert start_data["ok"] is True
    assert start_data["status"] == "started_async"
    assert start_data["command_stop_available"] is True
    assert start_data["command_stop_label"] == "WALK_WITH_ME"
    assert executor.started.wait(timeout=2), "async WALK_WITH_ME did not start"

    mid_state = client.get("/api/state", headers=headers)
    assert mid_state.status_code == 200
    mid_data = mid_state.get_json()
    assert mid_data["command_stop_available"] is True
    assert mid_data["command_stop_label"] == "WALK_WITH_ME"

    stop_resp = client.post(
        "/api/script/do",
        json={"mode": "command", "label": "STOP"},
        headers=headers,
    )
    assert stop_resp.status_code == 200
    stop_data = stop_resp.get_json()
    assert stop_data["ok"] is True
    assert stop_data["status"] == "accepted"
    assert stop_data["command_stop_available"] is False
    assert stop_data["command_stop_label"] is None

    executor.release.set()
    assert executor.finished.wait(timeout=2), "async WALK_WITH_ME did not finish after release"

    end_state = client.get("/api/state", headers=headers)
    assert end_state.status_code == 200
    end_data = end_state.get_json()
    assert end_data["command_stop_available"] is False
    assert end_data["command_stop_label"] is None


def test_script_do_walk_with_me_timeout_keeps_stop_available(monkeypatch):
    executor = TimeoutExecutor(timeout_label="WALK_WITH_ME")
    app = _make_app(monkeypatch, executor=executor)
    client = app.test_client(use_cookies=False)
    headers = {"Cookie": "sid=script-walk-timeout"}

    start_resp = client.post(
        "/api/script/do",
        json={"mode": "command", "label": "WALK_WITH_ME"},
        headers=headers,
    )
    assert start_resp.status_code == 200
    start_data = start_resp.get_json()
    assert start_data["ok"] is True
    assert start_data["status"] == "started_async"
    assert start_data["command_stop_available"] is True
    assert start_data["command_stop_label"] == "WALK_WITH_ME"
    assert executor.started.wait(timeout=2), "async WALK_WITH_ME did not start"

    mid_state = client.get("/api/state", headers=headers)
    assert mid_state.status_code == 200
    mid_data = mid_state.get_json()
    assert mid_data["command_stop_available"] is True
    assert mid_data["command_stop_label"] == "WALK_WITH_ME"

    stop_resp = client.post(
        "/api/script/do",
        json={"mode": "command", "label": "STOP"},
        headers=headers,
    )
    assert stop_resp.status_code == 200
    stop_data = stop_resp.get_json()
    assert stop_data["ok"] is True
    assert stop_data["command_stop_available"] is False
    assert stop_data["command_stop_label"] is None


def test_nao_wake_up_timeout_returns_pending(monkeypatch):
    def fake_post(url, **kwargs):
        raise requests.exceptions.ReadTimeout("HTTPConnectionPool(host='127.0.0.1', port=5000): Read timed out. (read timeout=3.0)")

    monkeypatch.setattr(webapp_server.requests, "post", fake_post)
    app = _make_app(monkeypatch)
    client = app.test_client()

    cfg_resp = client.post(
        "/api/runtime_config",
        json={
            "config": {
                "behavior_enabled": False,
                "base_enabled": True,
                "nao_base_url": "http://base:5000",
            }
        },
    )
    assert cfg_resp.status_code == 200

    resp = client.post("/api/nao_wake_up")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["pending"] is True
    assert data["is_awake"] is None


def test_command_catalog_excludes_non_manual_labels(monkeypatch):
    cmdrec = StubCmdrec(
        [
            "REST",
            "DANCE",
            "WALK_WITH_ME",
            "LOCOMOTION_REQUEST",
            "STOP",
            "WALK_FORWARD",
            "WALK_BACKWARD",
            "WALK_LEFT",
            "WALK_RIGHT",
            "TURN_LEFT",
            "TURN_RIGHT",
            "NONE",
        ]
    )
    app = _make_app(monkeypatch, cmdrec=cmdrec)
    client = app.test_client()

    resp = client.get("/api/command_catalog")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    labels = [item["label"] for item in data.get("commands", [])]
    assert "DANCE" in labels
    assert "WALK_WITH_ME" in labels
    assert "NONE" not in labels
    assert "REST" not in labels
    assert "STOP" not in labels
    assert "LOCOMOTION_REQUEST" not in labels
    assert "WALK_FORWARD" not in labels
    assert "WALK_BACKWARD" not in labels
    assert "WALK_LEFT" not in labels
    assert "WALK_RIGHT" not in labels
    assert "TURN_LEFT" not in labels
    assert "TURN_RIGHT" not in labels


def test_command_catalog_normalizes_escaped_labels_before_filtering(monkeypatch):
    cmdrec = StubCmdrec(
        [
            r"WALK\_WITH\_ME",
            r"WALK\_FORWARD",
            r"WALK\_BACKWARD",
            r"WALK\_LEFT",
            r"WALK\_RIGHT",
            r"TURN\_LEFT",
            r"TURN\_RIGHT",
            r"LOCOMOTION\_REQUEST",
            r"STOP",
            r"NONE",
        ]
    )
    app = _make_app(monkeypatch, cmdrec=cmdrec)
    client = app.test_client()

    resp = client.get("/api/command_catalog")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    labels = [item["label"] for item in data.get("commands", [])]
    assert r"WALK\_WITH\_ME" in labels
    assert r"WALK\_FORWARD" not in labels
    assert r"WALK\_BACKWARD" not in labels
    assert r"WALK\_LEFT" not in labels
    assert r"WALK\_RIGHT" not in labels
    assert r"TURN\_LEFT" not in labels
    assert r"TURN\_RIGHT" not in labels
    assert r"LOCOMOTION\_REQUEST" not in labels
    assert r"STOP" not in labels


def test_nao_custom_life_set_uses_custom_life_path_and_updates_runtime(monkeypatch):
    post_calls = []

    def fake_post(url, **kwargs):
        post_calls.append((url, kwargs))
        if url.endswith("/custom_life_apply"):
            return _Resp({"status": "ok", "data": {"prev_state": {"life_state": "solitary"}}})
        return _Resp({"status": "ok", "data": {}})

    def fake_get(url, **kwargs):
        if url.endswith("/custom_life_state"):
            return _Resp(
                {
                    "status": "ok",
                    "data": {
                        "modules": {
                            "basic_awareness": True,
                            "background_movement": False,
                            "breathing": True,
                        },
                        "life_state": "disabled",
                        "is_awake": True,
                    },
                }
            )
        return _Resp({"status": "ok"})

    monkeypatch.setattr(webapp_server.requests, "post", fake_post)
    monkeypatch.setattr(webapp_server.requests, "get", fake_get)
    app = _make_app(monkeypatch)
    client = app.test_client()

    cfg_resp = client.post(
        "/api/runtime_config",
        json={
            "config": {
                "behavior_enabled": False,
                "base_enabled": True,
                "nao_base_url": "http://base:5000",
                "custom_life_enabled": False,
            }
        },
    )
    assert cfg_resp.status_code == 200

    resp = client.post("/api/nao_custom_life_set", json={"enabled": True})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["enabled"] is True
    assert data["active_modules"] is True
    assert any(url.endswith("/custom_life_apply") for url, _ in post_calls)

    runtime_resp = client.get("/api/runtime_config")
    runtime_data = runtime_resp.get_json()
    assert runtime_data["ok"] is True
    assert runtime_data["config"]["custom_life_enabled"] is True


def test_nao_custom_life_set_disable_turns_underlying_services_off(monkeypatch):
    post_calls = []

    def fake_post(url, **kwargs):
        post_calls.append((url, kwargs))
        return _Resp({"status": "ok", "data": {}})

    def fake_get(url, **kwargs):
        if url.endswith("/custom_life_state"):
            return _Resp(
                {
                    "status": "ok",
                    "data": {
                        "modules": {
                            "basic_awareness": False,
                            "background_movement": False,
                            "breathing": False,
                        },
                        "is_awake": True,
                    },
                }
            )
        return _Resp({"status": "ok"})

    monkeypatch.setattr(webapp_server.requests, "post", fake_post)
    monkeypatch.setattr(webapp_server.requests, "get", fake_get)
    app = _make_app(monkeypatch)
    client = app.test_client()

    cfg_resp = client.post(
        "/api/runtime_config",
        json={
            "config": {
                "behavior_enabled": False,
                "base_enabled": True,
                "nao_base_url": "http://base:5000",
                "custom_life_enabled": True,
            }
        },
    )
    assert cfg_resp.status_code == 200

    resp = client.post("/api/nao_custom_life_set", json={"enabled": False})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["enabled"] is False
    assert data["active_modules"] is False
    assert any(url.endswith("/custom_life_apply") for url, _ in post_calls)
    assert not any(url.endswith("/custom_life_restore") for url, _ in post_calls)
    apply_calls = [kwargs for url, kwargs in post_calls if url.endswith("/custom_life_apply")]
    assert apply_calls
    assert apply_calls[-1]["json"] == {
        "settings": {
            "basic_awareness": False,
            "background_movement": False,
            "breathing": False,
        }
    }

    runtime_resp = client.get("/api/runtime_config")
    runtime_data = runtime_resp.get_json()
    assert runtime_data["ok"] is True
    assert runtime_data["config"]["custom_life_enabled"] is False
