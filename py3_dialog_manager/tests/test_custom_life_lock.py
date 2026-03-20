from __future__ import annotations

from types import SimpleNamespace

from dialog.interfaces import CommandDecision, LLMResult, RouteDecision

import webapp_server


class StubLLMBackend:
    def __init__(self, reply: str = "ok") -> None:
        self._reply = reply

    def generate(self, messages):
        return LLMResult(reply=self._reply, messages=list(messages))


class StubSTTBackend:
    def transcribe(self, audio):  # pragma: no cover
        raise AssertionError("STT not used in these tests")


class StubExecutor:
    def __init__(self) -> None:
        self.labels: list[str] = []

    def execute(self, cmd: CommandDecision) -> None:
        self.labels.append(cmd.label)


class NoOpOutput:
    def emit(self, text: str) -> None:
        return None


class StubCmdRec:
    def __init__(self, decisions: dict[str, RouteDecision]) -> None:
        self._decisions = decisions
        self.bundle_path = None

    def route(self, text: str, mode: str, active_behavior):
        return self._decisions[text]

    def is_guarded(self, label: str) -> bool:
        return False


class FakeResponse:
    def __init__(self, payload=None, status_code: int = 200) -> None:
        self._payload = payload if payload is not None else {}
        self.status_code = status_code
        self.ok = status_code < 400

    def json(self):
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"http status {self.status_code}")


def _base_cfg():
    return {
        "confirm_method": "web",
        "output": {"type": "console"},
        "custom_life_enabled": True,
        "custom_life_settings": {"state": "disabled"},
        "nao_connection": {
            "primary": {"base_url": "http://behavior.local/nao"},
            "fallback": {"base_url": "http://base.local"},
        },
    }


def _make_app(monkeypatch, decisions, cfg=None):
    executor = StubExecutor()
    base_pipeline = SimpleNamespace(
        llm=StubLLMBackend(reply="hi"),
        output=NoOpOutput(),
        status_to_console=True,
        system_prompt="SYSTEM",
        log_messages_path=None,
        log_meta={},
        max_history_turns=None,
        _cmdrec=StubCmdRec(decisions),
        _behavior_executor=executor,
        _debug_cmdrec=False,
    )
    monkeypatch.setattr(webapp_server, "build_pipeline_from_config", lambda *_a, **_k: base_pipeline)
    monkeypatch.setattr(webapp_server, "make_stt_backend_from_config", lambda *_a, **_k: StubSTTBackend())
    app, _, _ = webapp_server.create_app(cfg=cfg or _base_cfg(), config_path="<memory>")
    return app, executor


def _make_app_without_cmdrec(monkeypatch):
    base_pipeline = SimpleNamespace(
        llm=StubLLMBackend(reply="hi"),
        output=NoOpOutput(),
        status_to_console=True,
        system_prompt="SYSTEM",
        log_messages_path=None,
        log_meta={},
        max_history_turns=None,
    )
    monkeypatch.setattr(webapp_server, "build_pipeline_from_config", lambda *_a, **_k: base_pipeline)
    monkeypatch.setattr(webapp_server, "make_stt_backend_from_config", lambda *_a, **_k: StubSTTBackend())
    app, _, _ = webapp_server.create_app(cfg=_base_cfg(), config_path="<memory>")
    return app


def _patch_requests_post(monkeypatch, calls: list[str]) -> None:
    def fake_post(url, *args, **kwargs):
        calls.append(url)
        if url.endswith("/custom_life_pause"):
            return FakeResponse({"status": "ok", "data": {"enabled": False}})
        if url.endswith("/custom_life_apply"):
            return FakeResponse({"status": "ok"})
        if url.endswith("/custom_life_resume"):
            return FakeResponse({"status": "ok"})
        if url.endswith("/do_behavior"):
            return FakeResponse({"status": "ok"})
        if url.endswith("/stop_behavior"):
            return FakeResponse({"status": "ok"})
        return FakeResponse({"status": "ok"})

    monkeypatch.setattr(webapp_server.requests, "post", fake_post)


def _dm_events(client, *, event: str):
    resp = client.get(f"/api/dm_events?event={event}&limit=50")
    assert resp.status_code == 200
    return resp.get_json()["events"]


def test_short_behavior_unlocks_on_next_user_turn(monkeypatch):
    decisions = {
        "dans": RouteDecision(
            is_command=True,
            command=CommandDecision(
                label="DANCE",
                confidence=0.9,
                raw_text="dans",
                resolved={"dance_key": "happy"},
            ),
            top3=[("DANCE", 0.9)],
        ),
        "hallo": RouteDecision(is_command=False, reason="disabled"),
    }
    app, executor = _make_app(monkeypatch, decisions)
    calls: list[str] = []
    _patch_requests_post(monkeypatch, calls)
    client = app.test_client()

    resp1 = client.post("/api/send", json={"text": "dans"})
    assert resp1.status_code == 200
    data1 = resp1.get_json()
    assert data1["ok"] is True
    assert data1["command_stop_available"] is True
    assert data1["command_stop_label"] == "DANCE"
    assert executor.labels == ["DANCE"]
    assert calls == ["http://behavior.local/nao/custom_life_pause"]

    resp2 = client.post("/api/send", json={"text": "hallo", "emit": "none"})
    assert resp2.status_code == 200
    data2 = resp2.get_json()
    assert data2["command_stop_available"] is False
    assert data2["command_stop_label"] is None
    assert calls == [
        "http://behavior.local/nao/custom_life_pause",
        "http://behavior.local/nao/custom_life_apply",
    ]


def test_short_behavior_stop_state_clears_without_custom_life_lock(monkeypatch):
    decisions = {
        "dans": RouteDecision(
            is_command=True,
            command=CommandDecision(
                label="DANCE",
                confidence=0.9,
                raw_text="dans",
                resolved={"dance_key": "happy"},
            ),
            top3=[("DANCE", 0.9)],
        ),
        "hallo": RouteDecision(is_command=False, reason="disabled"),
    }
    cfg = _base_cfg()
    cfg["custom_life_enabled"] = False
    app, executor = _make_app(monkeypatch, decisions, cfg=cfg)
    calls: list[str] = []
    _patch_requests_post(monkeypatch, calls)
    client = app.test_client()

    resp1 = client.post("/api/send", json={"text": "dans"})
    assert resp1.status_code == 200
    data1 = resp1.get_json()
    assert data1["command_stop_available"] is True
    assert data1["command_stop_label"] == "DANCE"
    assert executor.labels == ["DANCE"]
    assert calls == []

    resp2 = client.post("/api/send", json={"text": "hallo", "emit": "none"})
    assert resp2.status_code == 200
    data2 = resp2.get_json()
    assert data2["command_stop_available"] is False
    assert data2["command_stop_label"] is None
    assert calls == []


def test_walk_with_me_stays_locked_until_stop(monkeypatch):
    decisions = {
        "loop mee": RouteDecision(
            is_command=True,
            command=CommandDecision(label="WALK_WITH_ME", confidence=0.9, raw_text="loop mee"),
            top3=[("WALK_WITH_ME", 0.9)],
        ),
        "praat": RouteDecision(is_command=False, reason="disabled"),
        "stop": RouteDecision(
            is_command=True,
            command=CommandDecision(label="STOP", confidence=0.9, raw_text="stop"),
            top3=[("STOP", 0.9)],
        ),
    }
    app, executor = _make_app(monkeypatch, decisions)
    calls: list[str] = []
    _patch_requests_post(monkeypatch, calls)
    client = app.test_client()

    resp1 = client.post("/api/send", json={"text": "loop mee"})
    assert resp1.status_code == 200
    data1 = resp1.get_json()
    assert data1["ok"] is True
    assert data1["command_stop_available"] is True
    assert data1["command_stop_label"] == "WALK_WITH_ME"
    assert calls == ["http://behavior.local/nao/custom_life_pause"]

    resp2 = client.post("/api/send", json={"text": "praat", "emit": "none"})
    assert resp2.status_code == 200
    assert calls == ["http://behavior.local/nao/custom_life_pause"]

    resp3 = client.post("/api/send", json={"text": "stop"})
    assert resp3.status_code == 200
    assert calls == [
        "http://behavior.local/nao/custom_life_pause",
        "http://behavior.local/nao/custom_life_apply",
        "http://behavior.local/nao/stop_audio",
    ]
    assert executor.labels == ["WALK_WITH_ME", "STOP", "STAND_UP"]


def test_manual_walk_behavior_unlocks_on_behavior_stop(monkeypatch):
    app = _make_app_without_cmdrec(monkeypatch)
    calls: list[str] = []
    _patch_requests_post(monkeypatch, calls)
    client = app.test_client()

    start_resp = client.post("/api/nao_behavior_start", json={"behavior": "walkwithme/walkwithme"})
    assert start_resp.status_code == 200
    assert start_resp.get_json()["ok"] is True

    stop_resp = client.post("/api/nao_behavior_stop", json={"behavior": "walkwithme/walkwithme"})
    assert stop_resp.status_code == 200
    assert stop_resp.get_json()["ok"] is True
    assert calls == [
        "http://behavior.local/nao/custom_life_pause",
        "http://behavior.local/nao/do_behavior",
        "http://behavior.local/nao/stop_behavior",
        "http://behavior.local/nao/do_behavior",
        "http://behavior.local/nao/custom_life_apply",
    ]


def test_manual_command_logs_automatic_custom_life_pause_and_release(monkeypatch):
    app, executor = _make_app(monkeypatch, decisions={})
    calls: list[str] = []
    _patch_requests_post(monkeypatch, calls)
    client = app.test_client()
    clear_resp = client.post("/api/dm_events_clear", json={})
    assert clear_resp.status_code == 200

    start_resp = client.post("/api/command_execute", json={"label": "WALK_WITH_ME"})
    assert start_resp.status_code == 200
    assert start_resp.get_json()["ok"] is True

    stop_resp = client.post("/api/command_execute", json={"label": "STOP"})
    assert stop_resp.status_code == 200
    assert stop_resp.get_json()["ok"] is True

    pause_events = _dm_events(client, event="custom_life_lock_pause")
    release_events = _dm_events(client, event="custom_life_lock_release")

    assert pause_events
    assert release_events
    assert pause_events[-1]["source"] == "manual_command_ui"
    assert pause_events[-1]["data"] == {"ok": True, "lock_mode": "until_stop"}
    assert release_events[-1]["source"] == "manual_command_ui"
    assert release_events[-1]["data"] == {"ok": True, "lock_mode": "until_stop", "action": "apply"}
    assert executor.labels == ["WALK_WITH_ME", "STOP", "STAND_UP"]


def test_manual_command_stop_after_walk_stands_up_without_custom_life_lock(monkeypatch):
    cfg = _base_cfg()
    cfg["custom_life_enabled"] = False
    app, executor = _make_app(monkeypatch, decisions={}, cfg=cfg)
    calls: list[str] = []
    _patch_requests_post(monkeypatch, calls)
    client = app.test_client()

    start_resp = client.post("/api/command_execute", json={"label": "WALK_WITH_ME"})
    assert start_resp.status_code == 200
    assert start_resp.get_json()["ok"] is True

    stop_resp = client.post("/api/command_execute", json={"label": "STOP"})
    assert stop_resp.status_code == 200
    assert stop_resp.get_json()["ok"] is True

    assert executor.labels == ["WALK_WITH_ME", "STOP", "STAND_UP"]
    assert calls == ["http://behavior.local/nao/stop_audio"]
