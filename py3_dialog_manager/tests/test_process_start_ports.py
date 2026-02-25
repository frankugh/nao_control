from __future__ import annotations

import io
import os
from types import SimpleNamespace

from dialog.interfaces import LLMResult

import webapp_server


class StubLLMBackend:
    def generate(self, messages):
        return LLMResult(reply="ok", messages=list(messages))


class StubSTTBackend:
    def transcribe(self, audio):  # pragma: no cover
        raise AssertionError("STT not used in these tests")


class _FakeProc:
    def __init__(self, pid: int = 4321) -> None:
        self.pid = pid
        self.stdout = io.StringIO("")
        self.stderr = io.StringIO("")

    def poll(self):
        return None

    def terminate(self):  # pragma: no cover
        return None

    def wait(self, timeout=None):  # pragma: no cover
        return 0

    def kill(self):  # pragma: no cover
        return None


def _make_app(monkeypatch):
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
    app, _, _ = webapp_server.create_app(cfg={}, config_path="<memory>")
    return app


def _patch_base_paths_exist(monkeypatch):
    orig_exists = os.path.exists

    def fake_exists(path):
        norm = str(path).replace("\\", "/").lower()
        if norm.endswith("/py2_nao_base_controller/venv/scripts/python.exe"):
            return True
        if norm.endswith("/py2_nao_base_controller/nao_api.py"):
            return True
        return orig_exists(path)

    monkeypatch.setattr(webapp_server.os.path, "exists", fake_exists)


def _patch_behavior_paths_exist(monkeypatch):
    orig_exists = os.path.exists

    def fake_exists(path):
        norm = str(path).replace("\\", "/").lower()
        if norm.endswith("/py3_nao_behavior_manager/venv/scripts/python.exe"):
            return True
        if norm.endswith("/py3_nao_behavior_manager/py3_server.py"):
            return True
        return orig_exists(path)

    monkeypatch.setattr(webapp_server.os.path, "exists", fake_exists)


def test_process_start_base_uses_runtime_nao_base_url_port(monkeypatch):
    app = _make_app(monkeypatch)
    client = app.test_client()
    _patch_base_paths_exist(monkeypatch)

    popen_calls = []

    def fake_popen(cmd, **kwargs):
        popen_calls.append({"cmd": list(cmd), "kwargs": kwargs})
        return _FakeProc()

    monkeypatch.setattr(webapp_server.subprocess, "Popen", fake_popen)

    cfg_resp = client.post(
        "/api/runtime_config",
        json={
            "config": {
                "nao_base_url": "http://127.0.0.1:5101",
                "nao_ip": "192.168.68.101",
                "nao_ip_enabled": True,
            }
        },
    )
    assert cfg_resp.status_code == 200

    resp = client.post("/api/process_start", json={"name": "base"})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert popen_calls
    cmd = popen_calls[-1]["cmd"]
    assert "--port" in cmd
    assert cmd[cmd.index("--port") + 1] == "5101"
    assert "--nao_ip" in cmd
    assert cmd[cmd.index("--nao_ip") + 1] == "192.168.68.101"


def test_process_start_base_defaults_to_5000_when_nao_base_url_has_no_port(monkeypatch):
    app = _make_app(monkeypatch)
    client = app.test_client()
    _patch_base_paths_exist(monkeypatch)

    popen_calls = []

    def fake_popen(cmd, **kwargs):
        popen_calls.append({"cmd": list(cmd), "kwargs": kwargs})
        return _FakeProc()

    monkeypatch.setattr(webapp_server.subprocess, "Popen", fake_popen)

    cfg_resp = client.post(
        "/api/runtime_config",
        base_url="http://127.0.0.1:5102",
        json={
            "config": {
                "nao_base_url": "http://127.0.0.1",
                "nao_ip": "192.168.68.101",
                "nao_ip_enabled": True,
            }
        },
    )
    assert cfg_resp.status_code == 200

    resp = client.post("/api/process_start", base_url="http://127.0.0.1:5102", json={"name": "base"})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    cmd = popen_calls[-1]["cmd"]
    assert "--port" in cmd
    assert cmd[cmd.index("--port") + 1] == "5000"


def test_process_start_behavior_uses_runtime_behavior_port_and_base_url(monkeypatch):
    app = _make_app(monkeypatch)
    client = app.test_client()
    _patch_behavior_paths_exist(monkeypatch)

    popen_calls = []

    def fake_popen(cmd, **kwargs):
        popen_calls.append({"cmd": list(cmd), "kwargs": kwargs})
        return _FakeProc()

    monkeypatch.setattr(webapp_server.subprocess, "Popen", fake_popen)

    cfg_resp = client.post(
        "/api/runtime_config",
        json={
            "config": {
                "behavior_manager_url": "http://127.0.0.1:5201",
                "nao_base_url": "http://127.0.0.1:5101",
            }
        },
    )
    assert cfg_resp.status_code == 200

    resp = client.post("/api/process_start", json={"name": "behavior"})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    cmd = popen_calls[-1]["cmd"]
    assert "--port" in cmd
    assert cmd[cmd.index("--port") + 1] == "5201"
    assert "--py2-api-url" in cmd
    assert cmd[cmd.index("--py2-api-url") + 1] == "http://127.0.0.1:5101"


def test_process_start_behavior_defaults_when_urls_missing_ports(monkeypatch):
    app = _make_app(monkeypatch)
    client = app.test_client()
    _patch_behavior_paths_exist(monkeypatch)

    popen_calls = []

    def fake_popen(cmd, **kwargs):
        popen_calls.append({"cmd": list(cmd), "kwargs": kwargs})
        return _FakeProc()

    monkeypatch.setattr(webapp_server.subprocess, "Popen", fake_popen)

    cfg_resp = client.post(
        "/api/runtime_config",
        base_url="http://127.0.0.1:5102",
        json={
            "config": {
                "behavior_manager_url": "http://127.0.0.1",
                "nao_base_url": "",
            }
        },
    )
    assert cfg_resp.status_code == 200

    resp = client.post("/api/process_start", base_url="http://127.0.0.1:5102", json={"name": "behavior"})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    cmd = popen_calls[-1]["cmd"]
    assert "--port" in cmd
    assert cmd[cmd.index("--port") + 1] == "5001"
    assert "--py2-api-url" in cmd
    assert cmd[cmd.index("--py2-api-url") + 1] == "http://127.0.0.1:5000"


def test_process_start_base_accepts_payload_nao_base_url_override(monkeypatch):
    app = _make_app(monkeypatch)
    client = app.test_client()
    _patch_base_paths_exist(monkeypatch)

    popen_calls = []

    def fake_popen(cmd, **kwargs):
        popen_calls.append({"cmd": list(cmd), "kwargs": kwargs})
        return _FakeProc()

    monkeypatch.setattr(webapp_server.subprocess, "Popen", fake_popen)

    cfg_resp = client.post(
        "/api/runtime_config",
        json={
            "config": {
                "nao_base_url": "http://127.0.0.1:5000",
                "nao_ip": "192.168.68.101",
                "nao_ip_enabled": True,
            }
        },
    )
    assert cfg_resp.status_code == 200

    resp = client.post(
        "/api/process_start",
        json={
            "name": "base",
            "nao_base_url": "http://127.0.0.1:5101",
        },
    )
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    cmd = popen_calls[-1]["cmd"]
    assert "--port" in cmd
    assert cmd[cmd.index("--port") + 1] == "5101"

    runtime = client.get("/api/runtime_config").get_json()
    assert runtime["ok"] is True
    assert runtime["config"]["nao_base_url"] == "http://127.0.0.1:5101"


def test_process_start_behavior_accepts_payload_url_overrides(monkeypatch):
    app = _make_app(monkeypatch)
    client = app.test_client()
    _patch_behavior_paths_exist(monkeypatch)

    popen_calls = []

    def fake_popen(cmd, **kwargs):
        popen_calls.append({"cmd": list(cmd), "kwargs": kwargs})
        return _FakeProc()

    monkeypatch.setattr(webapp_server.subprocess, "Popen", fake_popen)

    cfg_resp = client.post(
        "/api/runtime_config",
        json={
            "config": {
                "behavior_manager_url": "http://127.0.0.1:5001",
                "nao_base_url": "http://127.0.0.1:5000",
            }
        },
    )
    assert cfg_resp.status_code == 200

    resp = client.post(
        "/api/process_start",
        json={
            "name": "behavior",
            "behavior_manager_url": "http://127.0.0.1:5201",
            "nao_base_url": "http://127.0.0.1:5101",
        },
    )
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    cmd = popen_calls[-1]["cmd"]
    assert "--port" in cmd
    assert cmd[cmd.index("--port") + 1] == "5201"
    assert "--py2-api-url" in cmd
    assert cmd[cmd.index("--py2-api-url") + 1] == "http://127.0.0.1:5101"

    runtime = client.get("/api/runtime_config").get_json()
    assert runtime["ok"] is True
    assert runtime["config"]["behavior_manager_url"] == "http://127.0.0.1:5201"
    assert runtime["config"]["nao_base_url"] == "http://127.0.0.1:5101"


def test_process_start_base_rejects_webapp_port_conflict(monkeypatch):
    app = _make_app(monkeypatch)
    client = app.test_client()

    cfg_resp = client.post(
        "/api/runtime_config",
        base_url="http://127.0.0.1:5102",
        json={
            "config": {
                "nao_base_url": "http://127.0.0.1:5101",
                "nao_ip": "192.168.68.101",
                "nao_ip_enabled": True,
            }
        },
    )
    assert cfg_resp.status_code == 200

    def fake_popen(_cmd, **_kwargs):  # pragma: no cover
        raise AssertionError("process start should be rejected before spawning a process")

    monkeypatch.setattr(webapp_server.subprocess, "Popen", fake_popen)

    resp = client.post(
        "/api/process_start",
        base_url="http://127.0.0.1:5102",
        json={"name": "base", "nao_base_url": "http://127.0.0.1:5102"},
    )
    assert resp.status_code == 400
    data = resp.get_json()
    assert data["ok"] is False
    assert data["field"] == "nao_base_url"
