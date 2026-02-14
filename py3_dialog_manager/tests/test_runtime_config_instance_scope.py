from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from dialog.interfaces import LLMResult

import webapp_server


class StubLLMBackend:
    def generate(self, messages):
        return LLMResult(reply="ok", messages=list(messages))


class StubSTTBackend:
    def transcribe(self, audio):  # pragma: no cover
        raise AssertionError("STT not used in these tests")


def _make_app(
    monkeypatch,
    *,
    cfg=None,
    config_path="<memory>",
    instance_id=None,
    runtime_state_dir=None,
):
    base_pipeline = SimpleNamespace(
        llm=StubLLMBackend(),
        output=SimpleNamespace(emit=lambda _text: None),
        status_to_console=True,
        system_prompt="SYSTEM",
        log_messages_path=None,
        log_meta={},
        max_history_turns=None,
        _behavior_executor=None,
        _cmdrec=None,
        _debug_cmdrec=False,
    )
    monkeypatch.setattr(webapp_server, "build_pipeline_from_config", lambda *_a, **_k: base_pipeline)
    monkeypatch.setattr(webapp_server, "make_stt_backend_from_config", lambda *_a, **_k: StubSTTBackend())
    app, _, _ = webapp_server.create_app(
        cfg=cfg or {},
        config_path=config_path,
        instance_id=instance_id,
        runtime_state_dir=runtime_state_dir,
    )
    return app


def test_runtime_config_is_instance_global_not_sid_scoped(monkeypatch):
    app = _make_app(monkeypatch)
    c1 = app.test_client()
    c2 = app.test_client()

    set_resp = c1.post("/api/runtime_config", json={"config": {"wake_timeout_s": 33}})
    assert set_resp.status_code == 200
    assert set_resp.get_json()["ok"] is True

    get_resp = c2.get("/api/runtime_config")
    assert get_resp.status_code == 200
    cfg = get_resp.get_json()["config"]
    assert cfg["wake_timeout_s"] == 33


def test_runtime_config_write_ignored_for_client_mode(monkeypatch):
    app = _make_app(monkeypatch)
    server_client = app.test_client()
    viewer_client = app.test_client()
    viewer_client.set_cookie("ui_mode", "client")

    baseline = server_client.post("/api/runtime_config", json={"config": {"wake_timeout_s": 21}})
    assert baseline.status_code == 200

    ignored = viewer_client.post("/api/runtime_config", json={"config": {"wake_timeout_s": 99}})
    assert ignored.status_code == 200
    payload = ignored.get_json()
    assert payload["ok"] is True
    assert payload.get("client_ignored") is True

    after = server_client.get("/api/runtime_config")
    assert after.status_code == 200
    cfg = after.get_json()["config"]
    assert cfg["wake_timeout_s"] == 21


def test_runtime_config_persists_per_instance_id(monkeypatch, tmp_path: Path):
    cfg = {"output": {"type": "none"}}
    cfg_path = str(tmp_path / "cfg.json")
    state_dir = str(tmp_path / "runtime_state")
    instance_id = "demo_5301"

    app1 = _make_app(
        monkeypatch,
        cfg=cfg,
        config_path=cfg_path,
        instance_id=instance_id,
        runtime_state_dir=state_dir,
    )
    c1 = app1.test_client()
    set_resp = c1.post("/api/runtime_config", json={"config": {"wake_timeout_s": 47}})
    assert set_resp.status_code == 200

    app2 = _make_app(
        monkeypatch,
        cfg=cfg,
        config_path=cfg_path,
        instance_id=instance_id,
        runtime_state_dir=state_dir,
    )
    c2 = app2.test_client()
    get_resp = c2.get("/api/runtime_config")
    assert get_resp.status_code == 200
    cfg2 = get_resp.get_json()["config"]
    assert cfg2["wake_timeout_s"] == 47
