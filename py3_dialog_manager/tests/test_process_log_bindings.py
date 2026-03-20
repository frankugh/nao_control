from __future__ import annotations

import io
import os
import time
from pathlib import Path
from types import SimpleNamespace

from dialog.interfaces import CommandDecision, LLMResult, RouteDecision

import webapp_server


class StubLLMBackend:
    def generate(self, messages):
        return LLMResult(reply="ok", messages=list(messages))


class StubSTTBackend:
    def transcribe(self, _audio):  # pragma: no cover
        raise AssertionError("STT not used in process log binding tests")


class _FakeProc:
    def __init__(self, *, pid: int, stdout_text: str = "", stderr_text: str = "") -> None:
        self.pid = pid
        self.stdout = io.StringIO(stdout_text)
        self.stderr = io.StringIO(stderr_text)
        self._running = True

    def poll(self):
        return None if self._running else 0

    def terminate(self):  # pragma: no cover
        self._running = False

    def wait(self, timeout=None):  # pragma: no cover
        self._running = False
        return 0

    def kill(self):  # pragma: no cover
        self._running = False


def _make_app(monkeypatch, *, instance_id: str, cmdrec=None):
    base_pipeline = SimpleNamespace(
        llm=StubLLMBackend(),
        output=SimpleNamespace(emit=lambda _text: None),
        status_to_console=True,
        system_prompt="SYSTEM",
        log_messages_path=None,
        log_meta={},
        max_history_turns=None,
        _behavior_executor=SimpleNamespace(execute=lambda _cmd: None),
        _cmdrec=cmdrec,
        _debug_cmdrec=False,
    )
    monkeypatch.setattr(webapp_server, "build_pipeline_from_config", lambda *_a, **_k: base_pipeline)
    monkeypatch.setattr(webapp_server, "make_stt_backend_from_config", lambda *_a, **_k: StubSTTBackend())
    app, _, _ = webapp_server.create_app(cfg={}, config_path="<memory>", instance_id=instance_id)
    proc_log_dir = Path(webapp_server.__file__).resolve().parent / "logs" / "process"
    return app, proc_log_dir


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


def _patch_process_spawning(monkeypatch):
    spawned = []

    def fake_popen(cmd, **kwargs):
        cmd_list = list(cmd)
        port_hint = cmd_list[cmd_list.index("--port") + 1] if "--port" in cmd_list else "unknown"
        proc = _FakeProc(
            pid=4100 + len(spawned),
            stdout_text=f"stdout port={port_hint}\n",
            stderr_text=f"stderr port={port_hint}\n",
        )
        spawned.append({"cmd": cmd_list, "kwargs": kwargs, "proc": proc})
        return proc

    monkeypatch.setattr(webapp_server.subprocess, "Popen", fake_popen)
    return spawned


def _wait_for_file_contains(path: Path, needle: str, *, timeout_s: float = 1.5) -> str:
    deadline = time.time() + timeout_s
    last_text = ""
    while time.time() < deadline:
        if path.exists():
            last_text = path.read_text(encoding="utf-8", errors="replace")
            if needle in last_text:
                return last_text
        time.sleep(0.02)
    if path.exists():
        last_text = path.read_text(encoding="utf-8", errors="replace")
    raise AssertionError(f"{needle!r} not found in {path}: {last_text!r}")


def _remove_log_artifacts(*paths: Path) -> None:
    for path in paths:
        for candidate in (path, Path(str(path) + ".1")):
            try:
                if candidate.exists():
                    candidate.unlink()
            except OSError:
                pass


def test_process_start_base_writes_instance_and_port_specific_logfile(monkeypatch):
    app, proc_log_dir = _make_app(monkeypatch, instance_id="test_log_base")
    client = app.test_client()
    _patch_base_paths_exist(monkeypatch)
    _patch_process_spawning(monkeypatch)
    target = proc_log_dir / "base_test_log_base_5101.log"
    _remove_log_artifacts(target)

    cfg_resp = client.post(
        "/api/runtime_config",
        json={"config": {"nao_base_url": "http://127.0.0.1:5101", "nao_ip": "192.168.68.101", "nao_ip_enabled": True}},
    )
    assert cfg_resp.status_code == 200

    resp = client.post("/api/process_start", json={"name": "base"})
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True

    text = _wait_for_file_contains(target, "stdout port=5101")
    assert "--- started ---" in text
    assert "stderr port=5101" in _wait_for_file_contains(target, "stderr port=5101")
    _remove_log_artifacts(target)


def test_process_start_behavior_writes_instance_and_port_specific_logfile(monkeypatch):
    app, proc_log_dir = _make_app(monkeypatch, instance_id="test_log_behavior")
    client = app.test_client()
    _patch_behavior_paths_exist(monkeypatch)
    _patch_process_spawning(monkeypatch)
    target = proc_log_dir / "behavior_test_log_behavior_5201.log"
    _remove_log_artifacts(target)

    cfg_resp = client.post(
        "/api/runtime_config",
        json={"config": {"behavior_manager_url": "http://127.0.0.1:5201", "nao_base_url": "http://127.0.0.1:5101"}},
    )
    assert cfg_resp.status_code == 200

    resp = client.post("/api/process_start", json={"name": "behavior"})
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True

    text = _wait_for_file_contains(target, "stdout port=5201")
    assert "--- started ---" in text
    assert "stderr port=5201" in _wait_for_file_contains(target, "stderr port=5201")
    _remove_log_artifacts(target)


def test_process_logs_are_separated_per_instance(monkeypatch):
    app_alex, proc_log_dir = _make_app(monkeypatch, instance_id="test_sep_alex")
    app_renee, _ = _make_app(monkeypatch, instance_id="test_sep_renee")
    client_alex = app_alex.test_client()
    client_renee = app_renee.test_client()
    _patch_base_paths_exist(monkeypatch)
    _patch_process_spawning(monkeypatch)
    alex_log = proc_log_dir / "base_test_sep_alex_5101.log"
    renee_log = proc_log_dir / "base_test_sep_renee_5103.log"
    _remove_log_artifacts(alex_log, renee_log)

    assert client_alex.post(
        "/api/runtime_config",
        json={"config": {"nao_base_url": "http://127.0.0.1:5101", "nao_ip": "192.168.68.101", "nao_ip_enabled": True}},
    ).status_code == 200
    assert client_renee.post(
        "/api/runtime_config",
        json={"config": {"nao_base_url": "http://127.0.0.1:5103", "nao_ip": "192.168.68.102", "nao_ip_enabled": True}},
    ).status_code == 200

    assert client_alex.post("/api/process_start", json={"name": "base"}).status_code == 200
    assert client_renee.post("/api/process_start", json={"name": "base"}).status_code == 200

    assert alex_log != renee_log
    assert "stdout port=5101" in _wait_for_file_contains(alex_log, "stdout port=5101")
    assert "stdout port=5103" in _wait_for_file_contains(renee_log, "stdout port=5103")

    alex_lines = client_alex.get("/api/process_logs?name=base").get_json()["lines"]
    renee_lines = client_renee.get("/api/process_logs?name=base").get_json()["lines"]
    assert any("5101" in line for line in alex_lines)
    assert all("5103" not in line for line in alex_lines)
    assert any("5103" in line for line in renee_lines)
    assert all("5101" not in line for line in renee_lines)
    _remove_log_artifacts(alex_log, renee_log)


def test_process_logs_clear_only_clears_active_bound_logfile(monkeypatch):
    app, proc_log_dir = _make_app(monkeypatch, instance_id="test_clear")
    client = app.test_client()
    _patch_base_paths_exist(monkeypatch)
    _patch_process_spawning(monkeypatch)
    active_log = proc_log_dir / "base_test_clear_5101.log"
    old_log = proc_log_dir / "base_test_clear_5000.log"
    _remove_log_artifacts(active_log, old_log)

    assert client.post(
        "/api/runtime_config",
        json={"config": {"nao_base_url": "http://127.0.0.1:5101", "nao_ip": "192.168.68.101", "nao_ip_enabled": True}},
    ).status_code == 200
    assert client.post("/api/process_start", json={"name": "base"}).status_code == 200

    _wait_for_file_contains(active_log, "--- started ---")
    active_backup = Path(str(active_log) + ".1")
    old_backup = Path(str(old_log) + ".1")
    active_backup.write_text("active backup\n", encoding="utf-8")
    old_log.write_text("legacy log\n", encoding="utf-8")
    old_backup.write_text("legacy backup\n", encoding="utf-8")

    resp = client.post("/api/process_logs_clear", json={"name": "base"})
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True

    assert not active_log.exists()
    assert not active_backup.exists()
    assert old_log.exists()
    assert old_backup.exists()
    assert client.get("/api/process_logs?name=base").get_json()["lines"] == []
    _remove_log_artifacts(active_log, old_log)


def test_process_start_after_port_change_switches_active_logfile(monkeypatch):
    app, proc_log_dir = _make_app(monkeypatch, instance_id="test_rebind")
    client = app.test_client()
    _patch_base_paths_exist(monkeypatch)
    _patch_process_spawning(monkeypatch)
    first_log = proc_log_dir / "base_test_rebind_5101.log"
    second_log = proc_log_dir / "base_test_rebind_5105.log"
    _remove_log_artifacts(first_log, second_log)

    assert client.post(
        "/api/runtime_config",
        json={
            "config": {
                "nao_base_url": "http://127.0.0.1:5101",
                "nao_ip": "192.168.68.101",
                "nao_ip_enabled": True,
                "base_enabled": False,
            }
        },
    ).status_code == 200
    assert client.post("/api/process_start", json={"name": "base"}).status_code == 200
    assert "stdout port=5101" in _wait_for_file_contains(first_log, "stdout port=5101")

    stop_resp = client.post("/api/process_stop", json={"name": "base"})
    assert stop_resp.status_code == 200

    assert client.post(
        "/api/runtime_config",
        json={
            "config": {
                "nao_base_url": "http://127.0.0.1:5105",
                "nao_ip": "192.168.68.101",
                "nao_ip_enabled": True,
                "base_enabled": False,
            }
        },
    ).status_code == 200
    assert client.post("/api/process_start", json={"name": "base"}).status_code == 200

    assert "stdout port=5105" in _wait_for_file_contains(second_log, "stdout port=5105")
    assert first_log.exists()
    assert second_log.exists()

    lines = client.get("/api/process_logs?name=base").get_json()["lines"]
    assert any("5105" in line for line in lines)
    assert all("5101" not in line for line in lines)
    _remove_log_artifacts(first_log, second_log)


def test_dm_state_lines_use_active_base_log_binding(monkeypatch):
    class _FakeCmdRec:
        def route(self, _text, _mode, _active_behavior):
            return RouteDecision(
                is_command=True,
                command=CommandDecision(label="WAVE", confidence=1.0, raw_text="zwaai", resolved={}),
                reason="test_command",
                top3=[("WAVE", 1.0)],
            )

        def is_guarded(self, _label):
            return False

    app, proc_log_dir = _make_app(monkeypatch, instance_id="test_state", cmdrec=_FakeCmdRec())
    client = app.test_client()
    target = proc_log_dir / "base_test_state_5101.log"
    _remove_log_artifacts(target)

    assert client.post(
        "/api/runtime_config",
        json={"config": {"nao_base_url": "http://127.0.0.1:5101"}},
    ).status_code == 200

    resp = client.post("/api/send", json={"text": "zwaai"})
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True

    text = _wait_for_file_contains(target, "[DM][STATE]")
    assert '"command": "WAVE"' in text
    _remove_log_artifacts(target)
