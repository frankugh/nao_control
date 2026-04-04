from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType


_PACKAGE_ROOT = Path(__file__).resolve().parents[1]


def _load_script_module(filename: str) -> ModuleType:
    module_path = _PACKAGE_ROOT / "scripts" / filename
    spec = importlib.util.spec_from_file_location(f"_test_{module_path.stem}", module_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_run_from_json_resolves_relative_config_path_to_package_root(tmp_path: Path) -> None:
    module = _load_script_module("run_from_json.py")

    resolved = module._resolve_config_path("configs/default.json")

    assert resolved == str((_PACKAGE_ROOT / "configs" / "default.json").resolve())
    assert module._resolve_config_path(str(tmp_path / "cfg.json")) == str(tmp_path / "cfg.json")


def test_perf_set_baseline_resolves_relative_paths_to_package_root(tmp_path: Path) -> None:
    module = _load_script_module("perf_set_baseline.py")

    resolved = module._resolve_repo_path("logs/perf/perf_metrics.jsonl")

    assert resolved == (_PACKAGE_ROOT / "logs" / "perf" / "perf_metrics.jsonl").resolve()
    assert module._resolve_repo_path(str(tmp_path / "baseline.txt")) == tmp_path / "baseline.txt"


def test_perf_compare_latest_resolves_relative_paths_to_package_root(tmp_path: Path) -> None:
    module = _load_script_module("perf_compare_latest.py")

    resolved = module._resolve_repo_path("tests")

    assert resolved == (_PACKAGE_ROOT / "tests").resolve()
    assert module._resolve_repo_path(str(tmp_path)) == tmp_path
