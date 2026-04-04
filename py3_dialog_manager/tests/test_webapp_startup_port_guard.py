from __future__ import annotations

import socket
from pathlib import Path

import pytest

import webapp_server


def _free_local_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
    finally:
        sock.close()


def test_assert_web_port_available_allows_free_port() -> None:
    port = _free_local_port()

    webapp_server._assert_web_port_available("127.0.0.1", port)


def test_assert_web_port_available_fails_when_port_is_occupied() -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        port = int(sock.getsockname()[1])

        with pytest.raises(SystemExit) as exc_info:
            webapp_server._assert_web_port_available("127.0.0.1", port)

        assert f"Poort {port} is al in gebruik op 127.0.0.1" in str(exc_info.value)
    finally:
        sock.close()


def test_reset_runtime_state_for_startup_preset_removes_port_runtime_file(tmp_path, monkeypatch) -> None:
    fake_server_file = tmp_path / "py3_dialog_manager" / "webapp_server.py"
    runtime_file = tmp_path / "py3_dialog_manager" / "configs" / "runtime" / "runtime_port_5301.json"
    fake_server_file.parent.mkdir(parents=True, exist_ok=True)
    runtime_file.parent.mkdir(parents=True, exist_ok=True)
    fake_server_file.write_text("# fake", encoding="utf-8")
    runtime_file.write_text('{"robot_name": "Alex"}', encoding="utf-8")

    monkeypatch.setattr(webapp_server, "__file__", str(fake_server_file))

    runtime_state_path = webapp_server._reset_runtime_state_for_startup_preset(5301)

    assert runtime_state_path == str(runtime_file)
    assert not runtime_file.exists()


def test_resolve_config_path_uses_module_dir_for_relative_paths(tmp_path, monkeypatch) -> None:
    fake_server_file = tmp_path / "py3_dialog_manager" / "webapp_server.py"
    fake_config_file = tmp_path / "py3_dialog_manager" / "configs" / "default.json"
    fake_server_file.parent.mkdir(parents=True, exist_ok=True)
    fake_config_file.parent.mkdir(parents=True, exist_ok=True)
    fake_server_file.write_text("# fake", encoding="utf-8")
    fake_config_file.write_text("{}", encoding="utf-8")

    monkeypatch.setattr(webapp_server, "__file__", str(fake_server_file))

    config_path = webapp_server._resolve_config_path("configs/default.json")

    assert config_path == str(fake_config_file)


def test_resolve_config_path_keeps_absolute_paths(tmp_path) -> None:
    config_file = tmp_path / "custom.json"
    config_file.write_text("{}", encoding="utf-8")

    config_path = webapp_server._resolve_config_path(str(config_file))

    assert config_path == str(config_file)
