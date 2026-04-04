from __future__ import annotations

from pathlib import Path

from dialog import pipeline_builder


def test_resolve_log_dir_anchors_relative_path_to_config_package_dir(tmp_path: Path) -> None:
    config_path = tmp_path / "py3_dialog_manager" / "configs" / "default.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text("{}", encoding="utf-8")

    log_dir = pipeline_builder._resolve_log_dir(str(config_path), "logs")

    assert log_dir == str(tmp_path / "py3_dialog_manager" / "logs")


def test_resolve_log_dir_keeps_absolute_log_dir(tmp_path: Path) -> None:
    target = tmp_path / "custom_logs"

    log_dir = pipeline_builder._resolve_log_dir("<memory>", str(target))

    assert log_dir == str(target)
