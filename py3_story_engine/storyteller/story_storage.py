from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


def _safe_key(raw: Any, *, default: str = "default") -> str:
    value = re.sub(r"[^a-zA-Z0-9_.-]+", "_", str(raw or "").strip())
    return value or default


def sanitize_save_name(raw: Any) -> Optional[str]:
    value = str(raw or "").strip()
    if not value:
        return None
    value = re.sub(r"\.json$", "", value, flags=re.IGNORECASE)
    value = re.sub(r"[^a-zA-Z0-9_.-]+", "_", value).strip("._-")
    if not value:
        return None
    return value[:80]


def build_story_runtime_dir(repo_root: str) -> str:
    return os.path.join(repo_root, "runtime", "story")


def autosave_path(base_dir: str, *, instance_id: str, sid: str) -> str:
    return os.path.join(
        base_dir,
        f"autosave_{_safe_key(instance_id)}_{_safe_key(sid)}.json",
    )


def manual_save_path(base_dir: str, *, instance_id: str, sid: str, save_name: str) -> str:
    return os.path.join(
        base_dir,
        f"save_{_safe_key(instance_id)}_{_safe_key(sid)}_{_safe_key(save_name)}.json",
    )


def _write_json(path: str, payload: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)


def _read_json(path: str) -> Optional[Dict[str, Any]]:
    if not os.path.isfile(path):
        return None
    with open(path, "r", encoding="utf-8") as fh:
        raw = json.load(fh)
    if not isinstance(raw, dict):
        return None
    return raw


def save_autosave(base_dir: str, *, instance_id: str, sid: str, payload: Dict[str, Any]) -> str:
    path = autosave_path(base_dir, instance_id=instance_id, sid=sid)
    _write_json(path, payload)
    return path


def load_autosave(base_dir: str, *, instance_id: str, sid: str) -> Optional[Dict[str, Any]]:
    return _read_json(autosave_path(base_dir, instance_id=instance_id, sid=sid))


def save_named(base_dir: str, *, instance_id: str, sid: str, save_name: str, payload: Dict[str, Any]) -> Tuple[str, str]:
    normalized = sanitize_save_name(save_name)
    if not normalized:
        raise ValueError("Ongeldige save naam.")
    path = manual_save_path(base_dir, instance_id=instance_id, sid=sid, save_name=normalized)
    _write_json(path, payload)
    return normalized, path


def load_named(base_dir: str, *, instance_id: str, sid: str, save_name: str) -> Optional[Dict[str, Any]]:
    normalized = sanitize_save_name(save_name)
    if not normalized:
        return None
    return _read_json(manual_save_path(base_dir, instance_id=instance_id, sid=sid, save_name=normalized))


def list_named_saves(base_dir: str, *, instance_id: str, sid: str) -> List[str]:
    root = Path(base_dir)
    if not root.is_dir():
        return []
    prefix = f"save_{_safe_key(instance_id)}_{_safe_key(sid)}_"
    out: List[str] = []
    for path in sorted(root.glob(f"{prefix}*.json")):
        name = path.stem[len(prefix) :]
        if name:
            out.append(name)
    return out


def file_mtime(path: str) -> float:
    try:
        return float(os.path.getmtime(path))
    except Exception:
        return 0.0
