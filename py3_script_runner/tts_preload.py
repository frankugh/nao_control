from __future__ import annotations

import copy
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .client import DMClient


JsonDict = Dict[str, Any]
ClientFactory = Callable[[str, float], DMClient]

PRELOAD_VERSION = 1


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_preload_text(text: Any) -> str:
    value = str(text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    return value


def text_hash(text: str) -> str:
    return hashlib.sha256(normalize_preload_text(text).encode("utf-8")).hexdigest()


def clip_id_for(profile_fingerprint: str, text_digest: str) -> str:
    return f"{profile_fingerprint}:{text_digest}"


def _clip_dir_name(profile_fingerprint: str) -> str:
    raw = str(profile_fingerprint or "").strip()
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _safe_rel_path(value: Any) -> str:
    text = str(value or "").replace("\\", "/").strip().lstrip("/")
    return text


def _friendly_remote_error(exc: Exception) -> str:
    message = str(exc or "").strip()
    if not message:
        return "onbekende fout"
    if "):" in message:
        return message.split("):", 1)[1].strip()
    http_match = re.search(r"failed \(status=\d+\):\s*(.+)$", message)
    if http_match:
        return str(http_match.group(1) or "").strip() or message
    return message


def _profile_generated_at(meta: JsonDict) -> str:
    value = str(meta.get("generated_at") or "").strip()
    return value or ""


def _manifest_default(script_id: str) -> JsonDict:
    return {
        "version": PRELOAD_VERSION,
        "script_id": str(script_id or "").strip(),
        "profiles_meta": {},
        "steps": [],
    }


def _step_key(step: JsonDict) -> Tuple[str, str]:
    return (str(step.get("robot_id") or "").strip(), str(step.get("text_hash") or "").strip())


def _normalize_manifest_profile_entry(raw: Any) -> Optional[JsonDict]:
    if not isinstance(raw, dict):
        return None
    clip_id = str(raw.get("clip_id") or "").strip()
    clip_rel_path = _safe_rel_path(raw.get("clip_rel_path"))
    if not clip_id or not clip_rel_path:
        return None
    out: JsonDict = {
        "clip_id": clip_id,
        "clip_rel_path": clip_rel_path,
    }
    created_at = str(raw.get("created_at") or "").strip()
    if created_at:
        out["created_at"] = created_at
    return out


def _normalize_manifest(raw: Any, script_id: str, store: "TtsPreloadStore") -> JsonDict:
    manifest = _manifest_default(script_id)
    if not isinstance(raw, dict):
        return manifest
    profiles_meta_raw = raw.get("profiles_meta")
    if isinstance(profiles_meta_raw, dict):
        clean_meta: Dict[str, JsonDict] = {}
        for fingerprint, meta_raw in profiles_meta_raw.items():
            fp = str(fingerprint or "").strip()
            if not fp or not isinstance(meta_raw, dict):
                continue
            clean_meta[fp] = {
                "engine": str(meta_raw.get("engine") or "").strip(),
                "summary": str(meta_raw.get("summary") or "").strip(),
                "fingerprint": fp,
                "details": copy.deepcopy(meta_raw.get("details") or {}),
                "generated_at": str(meta_raw.get("generated_at") or "").strip(),
            }
            reason = str(meta_raw.get("reason") or "").strip()
            if reason:
                clean_meta[fp]["reason"] = reason
        manifest["profiles_meta"] = clean_meta
    steps_raw = raw.get("steps")
    if isinstance(steps_raw, list):
        clean_steps: List[JsonDict] = []
        for item in steps_raw:
            if not isinstance(item, dict):
                continue
            step_id = str(item.get("step_id") or "").strip()
            robot_id = str(item.get("robot_id") or "").strip()
            text = normalize_preload_text(item.get("text"))
            digest = str(item.get("text_hash") or "").strip() or text_hash(text)
            if not step_id or not robot_id or not text or not digest:
                continue
            profiles_raw = item.get("profiles")
            profiles: Dict[str, JsonDict] = {}
            if isinstance(profiles_raw, dict):
                for fingerprint, profile_entry_raw in profiles_raw.items():
                    fp = str(fingerprint or "").strip()
                    normalized = _normalize_manifest_profile_entry(profile_entry_raw)
                    if not fp or normalized is None:
                        continue
                    if not store.clip_exists(normalized["clip_rel_path"]):
                        continue
                    profiles[fp] = normalized
            clean_steps.append(
                {
                    "step_id": step_id,
                    "robot_id": robot_id,
                    "text": text,
                    "text_hash": digest,
                    "profiles": profiles,
                }
            )
        manifest["steps"] = clean_steps
    return manifest


def say_steps_from_script(script: JsonDict) -> List[JsonDict]:
    steps_out: List[JsonDict] = []
    for step_raw in list(script.get("steps") or []):
        if not isinstance(step_raw, dict):
            continue
        action = step_raw.get("action") if isinstance(step_raw.get("action"), dict) else {}
        if str(action.get("type") or "").strip().lower() != "say":
            continue
        step_id = str(step_raw.get("id") or "").strip()
        robot_id = str(step_raw.get("robot_id") or "").strip()
        text = normalize_preload_text(action.get("text"))
        if not step_id or not robot_id or not text:
            continue
        steps_out.append(
            {
                "step_id": step_id,
                "robot_id": robot_id,
                "text": text,
                "text_hash": text_hash(text),
            }
        )
    return steps_out


def script_id_from_script(script: JsonDict) -> str:
    meta = script.get("meta") if isinstance(script.get("meta"), dict) else {}
    return str(meta.get("script_id") or "").strip()


class TtsPreloadStore:
    def __init__(self, root: Optional[Path] = None) -> None:
        self.root = Path(root) if root is not None else (Path(__file__).resolve().parent.parent / "runtime_data" / "preloaded_tts")
        self.manifests_dir = self.root / "manifests"
        self.clips_dir = self.root / "clips"
        self.index_path = self.root / "cache_index.json"
        self.root.mkdir(parents=True, exist_ok=True)
        self.manifests_dir.mkdir(parents=True, exist_ok=True)
        self.clips_dir.mkdir(parents=True, exist_ok=True)

    def manifest_path(self, script_id: str) -> Path:
        return self.manifests_dir / str(script_id or "").strip() / "manifest.json"

    def load_manifest(self, script_id: str) -> JsonDict:
        script_key = str(script_id or "").strip()
        if not script_key:
            raise ValueError("script_id ontbreekt voor preload manifest.")
        path = self.manifest_path(script_key)
        if not path.is_file():
            return _manifest_default(script_key)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return _manifest_default(script_key)
        return _normalize_manifest(raw, script_key, self)

    def save_manifest(self, manifest: JsonDict) -> None:
        script_id = str(manifest.get("script_id") or "").strip()
        if not script_id:
            raise ValueError("manifest.script_id ontbreekt.")
        path = self.manifest_path(script_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    def clip_rel_path(self, profile_fingerprint: str, text_digest: str) -> str:
        return str(Path("clips") / _clip_dir_name(profile_fingerprint) / f"{text_digest}.wav").replace("\\", "/")

    def clip_abs_path(self, rel_path: str) -> Path:
        rel = _safe_rel_path(rel_path)
        return (self.root / rel).resolve()

    def clip_exists(self, rel_path: str) -> bool:
        if not rel_path:
            return False
        try:
            path = self.clip_abs_path(rel_path)
        except Exception:
            return False
        return path.is_file()

    def load_index(self) -> JsonDict:
        if not self.index_path.is_file():
            return {"version": PRELOAD_VERSION, "clips": {}}
        try:
            raw = json.loads(self.index_path.read_text(encoding="utf-8"))
        except Exception:
            return {"version": PRELOAD_VERSION, "clips": {}}
        clips_raw = raw.get("clips")
        clips: Dict[str, JsonDict] = {}
        if isinstance(clips_raw, dict):
            for clip_id, item in clips_raw.items():
                cid = str(clip_id or "").strip()
                if not cid or not isinstance(item, dict):
                    continue
                rel = _safe_rel_path(item.get("clip_rel_path"))
                if not rel:
                    continue
                clip_item: JsonDict = {
                    "clip_rel_path": rel,
                    "profile_fingerprint": str(item.get("profile_fingerprint") or "").strip(),
                    "text_hash": str(item.get("text_hash") or "").strip(),
                    "created_at": str(item.get("created_at") or "").strip(),
                    "last_seen_referenced_at": str(item.get("last_seen_referenced_at") or "").strip(),
                    "orphaned_at": str(item.get("orphaned_at") or "").strip(),
                }
                size_bytes = item.get("size_bytes")
                if isinstance(size_bytes, int) and size_bytes >= 0:
                    clip_item["size_bytes"] = size_bytes
                clips[cid] = clip_item
        return {"version": PRELOAD_VERSION, "clips": clips}

    def save_index(self, index: JsonDict) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")

    def touch_clip(self, clip_id: str, rel_path: str, *, profile_fingerprint: str, text_digest: str) -> None:
        index = self.load_index()
        clips = index.setdefault("clips", {})
        now = _now_iso()
        abs_path = self.clip_abs_path(rel_path)
        size_bytes = abs_path.stat().st_size if abs_path.is_file() else 0
        entry = clips.get(clip_id, {})
        created_at = str(entry.get("created_at") or "").strip() or now
        clips[clip_id] = {
            "clip_rel_path": _safe_rel_path(rel_path),
            "profile_fingerprint": str(profile_fingerprint or "").strip(),
            "text_hash": str(text_digest or "").strip(),
            "created_at": created_at,
            "last_seen_referenced_at": now,
            "orphaned_at": "",
            "size_bytes": int(size_bytes),
        }
        self.save_index(index)

    def ensure_clip(self, profile_fingerprint: str, text_digest: str, wav_bytes: bytes) -> JsonDict:
        clip_id = clip_id_for(profile_fingerprint, text_digest)
        rel_path = self.clip_rel_path(profile_fingerprint, text_digest)
        abs_path = self.clip_abs_path(rel_path)
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        if not abs_path.is_file():
            abs_path.write_bytes(wav_bytes)
        self.touch_clip(clip_id, rel_path, profile_fingerprint=profile_fingerprint, text_digest=text_digest)
        return {"clip_id": clip_id, "clip_rel_path": rel_path}

    def iter_manifest_paths(self) -> List[Path]:
        if not self.manifests_dir.exists():
            return []
        return sorted(self.manifests_dir.glob("*/manifest.json"))

    def refresh_index_from_manifests(self) -> JsonDict:
        index = self.load_index()
        clips = index.setdefault("clips", {})
        referenced: Dict[str, str] = {}
        for path in self.iter_manifest_paths():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            manifest = _normalize_manifest(raw, path.parent.name, self)
            for step in manifest.get("steps") or []:
                if not isinstance(step, dict):
                    continue
                profiles = step.get("profiles") if isinstance(step.get("profiles"), dict) else {}
                for profile_entry in profiles.values():
                    if not isinstance(profile_entry, dict):
                        continue
                    clip_id = str(profile_entry.get("clip_id") or "").strip()
                    rel_path = _safe_rel_path(profile_entry.get("clip_rel_path"))
                    if clip_id and rel_path and self.clip_exists(rel_path):
                        referenced[clip_id] = rel_path
        now = _now_iso()
        for clip_id, rel_path in referenced.items():
            entry = clips.get(clip_id, {})
            abs_path = self.clip_abs_path(rel_path)
            clips[clip_id] = {
                "clip_rel_path": rel_path,
                "profile_fingerprint": str(entry.get("profile_fingerprint") or "").strip(),
                "text_hash": str(entry.get("text_hash") or "").strip(),
                "created_at": str(entry.get("created_at") or "").strip() or now,
                "last_seen_referenced_at": now,
                "orphaned_at": "",
                "size_bytes": int(abs_path.stat().st_size if abs_path.is_file() else 0),
            }
        for clip_id, entry in list(clips.items()):
            if clip_id in referenced:
                continue
            orphaned_at = str(entry.get("orphaned_at") or "").strip()
            if not orphaned_at:
                entry["orphaned_at"] = now
            clips[clip_id] = entry
        self.save_index(index)
        return index

    def prune(self, policy: str) -> JsonDict:
        index = self.refresh_index_from_manifests()
        clips = index.setdefault("clips", {})
        now = datetime.now(timezone.utc)
        deleted: List[str] = []
        deleted_count = 0
        policy_key = str(policy or "").strip().lower()
        threshold_days: Optional[int]
        if policy_key == "all":
            threshold_days = 0
        elif policy_key in {"14d", "14", "14_days"}:
            threshold_days = 14
        elif policy_key in {"30d", "30", "30_days"}:
            threshold_days = 30
        else:
            raise ValueError("Ongeldig prune policy.")
        for clip_id, entry in list(clips.items()):
            orphaned_at = str(entry.get("orphaned_at") or "").strip()
            if not orphaned_at:
                continue
            should_delete = threshold_days == 0
            if threshold_days and threshold_days > 0:
                try:
                    orphan_dt = datetime.fromisoformat(orphaned_at)
                    delta_days = (now - orphan_dt).total_seconds() / 86400.0
                    should_delete = delta_days >= float(threshold_days)
                except Exception:
                    should_delete = False
            if not should_delete:
                continue
            rel_path = _safe_rel_path(entry.get("clip_rel_path"))
            abs_path = self.clip_abs_path(rel_path) if rel_path else None
            if abs_path is not None and abs_path.is_file():
                try:
                    abs_path.unlink()
                    parent = abs_path.parent
                    while parent != self.root and parent.exists():
                        try:
                            parent.rmdir()
                        except OSError:
                            break
                        parent = parent.parent
                except OSError:
                    continue
            deleted.append(rel_path)
            deleted_count += 1
            clips.pop(clip_id, None)
        self.save_index(index)
        return {
            "ok": True,
            "policy": policy_key,
            "deleted_count": deleted_count,
            "deleted": deleted,
            "remaining_count": len(clips),
        }


class TtsPreloadService:
    def __init__(
        self,
        *,
        store: Optional[TtsPreloadStore] = None,
        client_factory: Optional[ClientFactory] = None,
        timeout_s: float = 12.0,
    ) -> None:
        self.store = store or TtsPreloadStore()
        self.client_factory = client_factory or (lambda url, timeout: DMClient(url, timeout_s=timeout))
        self.timeout_s = float(timeout_s)

    def _require_script_id(self, script: JsonDict) -> str:
        script_id = script_id_from_script(script)
        if not script_id:
            raise ValueError("script meta.script_id ontbreekt.")
        return script_id

    def _script_client(self, dm_url: str) -> DMClient:
        return self.client_factory(str(dm_url or "").strip(), self.timeout_s)

    def _profile_for_robot(self, robot_cfg: JsonDict) -> JsonDict:
        dm_url = str(robot_cfg.get("dm_url") or "").strip()
        client = self._script_client(dm_url)
        override = robot_cfg.get("runtime_config") if isinstance(robot_cfg.get("runtime_config"), dict) else None
        return client.script_tts_profile(runtime_config_override=override, timeout_s=self.timeout_s)

    def _render_for_robot(self, robot_cfg: JsonDict, text: str) -> bytes:
        dm_url = str(robot_cfg.get("dm_url") or "").strip()
        client = self._script_client(dm_url)
        override = robot_cfg.get("runtime_config") if isinstance(robot_cfg.get("runtime_config"), dict) else None
        return client.script_tts_render(text=text, runtime_config_override=override, timeout_s=self.timeout_s)

    def _rebuild_manifest(self, script: JsonDict, existing_manifest: JsonDict) -> JsonDict:
        script_id = self._require_script_id(script)
        steps = say_steps_from_script(script)
        existing_steps = existing_manifest.get("steps") if isinstance(existing_manifest.get("steps"), list) else []
        existing_by_key: Dict[Tuple[str, str], Dict[str, JsonDict]] = {}
        for item in existing_steps:
            if not isinstance(item, dict):
                continue
            key = _step_key(item)
            if not key[0] or not key[1]:
                continue
            profiles = item.get("profiles") if isinstance(item.get("profiles"), dict) else {}
            merged = existing_by_key.setdefault(key, {})
            for fingerprint, profile_entry_raw in profiles.items():
                fp = str(fingerprint or "").strip()
                normalized = _normalize_manifest_profile_entry(profile_entry_raw)
                if not fp or normalized is None:
                    continue
                if not self.store.clip_exists(normalized["clip_rel_path"]):
                    continue
                current = merged.get(fp)
                created_at = str(normalized.get("created_at") or "").strip()
                current_created = str((current or {}).get("created_at") or "").strip()
                if current is None or created_at >= current_created:
                    merged[fp] = normalized
        profiles_meta_raw = existing_manifest.get("profiles_meta") if isinstance(existing_manifest.get("profiles_meta"), dict) else {}
        profiles_meta: Dict[str, JsonDict] = {}
        for fingerprint, meta_raw in profiles_meta_raw.items():
            fp = str(fingerprint or "").strip()
            if not fp or not isinstance(meta_raw, dict):
                continue
            profiles_meta[fp] = {
                "engine": str(meta_raw.get("engine") or "").strip(),
                "summary": str(meta_raw.get("summary") or "").strip(),
                "fingerprint": fp,
                "details": copy.deepcopy(meta_raw.get("details") or {}),
                "generated_at": str(meta_raw.get("generated_at") or "").strip(),
            }
            reason = str(meta_raw.get("reason") or "").strip()
            if reason:
                profiles_meta[fp]["reason"] = reason
        rebuilt_steps: List[JsonDict] = []
        for step in steps:
            key = _step_key(step)
            profiles = copy.deepcopy(existing_by_key.get(key) or {})
            rebuilt_steps.append(
                {
                    "step_id": step["step_id"],
                    "robot_id": step["robot_id"],
                    "text": step["text"],
                    "text_hash": step["text_hash"],
                    "profiles": profiles,
                }
            )
        referenced_fps = {
            str(fp)
            for step in rebuilt_steps
            for fp in (step.get("profiles") or {}).keys()
            if isinstance(step.get("profiles"), dict)
        }
        filtered_meta = {fp: meta for fp, meta in profiles_meta.items() if fp in referenced_fps}
        return {
            "version": PRELOAD_VERSION,
            "script_id": script_id,
            "profiles_meta": filtered_meta,
            "steps": rebuilt_steps,
        }

    def _analyze(self, script: JsonDict) -> JsonDict:
        script_id = self._require_script_id(script)
        existing_manifest = self.store.load_manifest(script_id)
        manifest = self._rebuild_manifest(script, existing_manifest)
        robots_cfg = script.get("robots") if isinstance(script.get("robots"), dict) else {}
        say_steps = manifest.get("steps") if isinstance(manifest.get("steps"), list) else []
        robots_out: List[JsonDict] = []
        for robot_id, robot_cfg_raw in robots_cfg.items():
            robot_cfg = robot_cfg_raw if isinstance(robot_cfg_raw, dict) else {}
            robot_steps = [step for step in say_steps if str(step.get("robot_id") or "").strip() == str(robot_id)]
            if not robot_steps:
                continue
            robot_info: JsonDict = {
                "robot_id": str(robot_id),
                "say_count": len(robot_steps),
                "supported": False,
                "status": "profile_unavailable",
                "current_ready": False,
                "existing_ready": False,
                "current_missing_count": len(robot_steps),
                "current_profile": None,
                "existing_profile": None,
                "message": "",
            }
            try:
                profile_payload = self._profile_for_robot(robot_cfg)
            except Exception as exc:
                robot_info["status"] = "profile_unavailable"
                robot_info["message"] = str(exc)
                robots_out.append(robot_info)
                continue
            supported = bool(profile_payload.get("supported"))
            robot_info["supported"] = supported
            if supported:
                current_profile = {
                    "engine": str(profile_payload.get("engine") or "").strip(),
                    "summary": str(profile_payload.get("summary") or "").strip(),
                    "fingerprint": str(profile_payload.get("fingerprint") or "").strip(),
                    "details": copy.deepcopy(profile_payload.get("details") or {}),
                }
                robot_info["current_profile"] = current_profile
                current_fp = current_profile["fingerprint"]
                current_ready = True
                current_missing = 0
                for step in robot_steps:
                    profiles = step.get("profiles") if isinstance(step.get("profiles"), dict) else {}
                    item = profiles.get(current_fp) if isinstance(profiles, dict) else None
                    rel_path = _safe_rel_path((item or {}).get("clip_rel_path"))
                    if not item or not rel_path or not self.store.clip_exists(rel_path):
                        current_ready = False
                        current_missing += 1
                robot_info["current_ready"] = current_ready
                robot_info["current_missing_count"] = current_missing
                candidates = set()
                for step in robot_steps:
                    profiles = step.get("profiles") if isinstance(step.get("profiles"), dict) else {}
                    candidates.update(str(fp) for fp in profiles.keys())
                candidates.discard(current_fp)
                best_alt: Optional[JsonDict] = None
                for fingerprint in sorted(candidates):
                    all_present = True
                    for step in robot_steps:
                        item = (step.get("profiles") or {}).get(fingerprint)
                        rel_path = _safe_rel_path((item or {}).get("clip_rel_path"))
                        if not item or not rel_path or not self.store.clip_exists(rel_path):
                            all_present = False
                            break
                    if not all_present:
                        continue
                    meta = manifest.get("profiles_meta", {}).get(fingerprint, {})
                    candidate = {
                        "fingerprint": fingerprint,
                        "engine": str(meta.get("engine") or "").strip(),
                        "summary": str(meta.get("summary") or "").strip(),
                        "details": copy.deepcopy(meta.get("details") or {}),
                        "generated_at": _profile_generated_at(meta),
                    }
                    if best_alt is None or candidate["generated_at"] >= str(best_alt.get("generated_at") or ""):
                        best_alt = candidate
                if current_ready:
                    robot_info["status"] = "current_ready"
                elif best_alt is not None:
                    robot_info["status"] = "mismatch_existing"
                    robot_info["existing_ready"] = True
                    robot_info["existing_profile"] = best_alt
                else:
                    robot_info["status"] = "missing"
            else:
                robot_info["status"] = "unsupported"
                robot_info["message"] = str(profile_payload.get("reason") or "not_renderable")
                robot_info["current_profile"] = {
                    "engine": str(profile_payload.get("engine") or "").strip(),
                    "summary": str(profile_payload.get("summary") or "").strip(),
                    "details": copy.deepcopy(profile_payload.get("details") or {}),
                }
            robots_out.append(robot_info)
        return {
            "ok": True,
            "script_id": script_id,
            "manifest": manifest,
            "robots": robots_out,
            "say_count": len(say_steps),
            "has_say_steps": len(say_steps) > 0,
        }

    def status(self, script: JsonDict) -> JsonDict:
        analysis = self._analyze(script)
        robots_out: List[JsonDict] = []
        for item in analysis["robots"]:
            robots_out.append(
                {
                    "robot_id": item["robot_id"],
                    "say_count": item["say_count"],
                    "supported": item["supported"],
                    "status": item["status"],
                    "current_ready": item["current_ready"],
                    "existing_ready": item["existing_ready"],
                    "current_missing_count": item["current_missing_count"],
                    "current_profile": copy.deepcopy(item.get("current_profile")),
                    "existing_profile": copy.deepcopy(item.get("existing_profile")),
                    "message": str(item.get("message") or ""),
                }
            )
        return {
            "ok": True,
            "script_id": analysis["script_id"],
            "has_say_steps": analysis["has_say_steps"],
            "say_count": analysis["say_count"],
            "robots": robots_out,
        }

    def generate(self, script: JsonDict) -> JsonDict:
        analysis = self._analyze(script)
        manifest = copy.deepcopy(analysis["manifest"])
        robots_cfg = script.get("robots") if isinstance(script.get("robots"), dict) else {}
        steps_by_robot: Dict[str, List[JsonDict]] = {}
        for step in manifest.get("steps") or []:
            if not isinstance(step, dict):
                continue
            steps_by_robot.setdefault(str(step.get("robot_id") or "").strip(), []).append(step)
        generated_count = 0
        reused_count = 0
        robots_out: List[JsonDict] = []
        profiles_meta = manifest.setdefault("profiles_meta", {})
        for robot_info in analysis["robots"]:
            robot_id = str(robot_info.get("robot_id") or "").strip()
            robot_cfg = robots_cfg.get(robot_id) if isinstance(robots_cfg.get(robot_id), dict) else {}
            robot_steps = steps_by_robot.get(robot_id, [])
            if not robot_steps:
                continue
            if not robot_info.get("supported"):
                robots_out.append(
                    {
                        "robot_id": robot_id,
                        "status": str(robot_info.get("status") or "unsupported"),
                        "generated_count": 0,
                        "reused_count": 0,
                        "message": str(robot_info.get("message") or ""),
                    }
                )
                continue
            current_profile = robot_info.get("current_profile") if isinstance(robot_info.get("current_profile"), dict) else {}
            fingerprint = str(current_profile.get("fingerprint") or "").strip()
            profiles_meta[fingerprint] = {
                "fingerprint": fingerprint,
                "engine": str(current_profile.get("engine") or "").strip(),
                "summary": str(current_profile.get("summary") or "").strip(),
                "details": copy.deepcopy(current_profile.get("details") or {}),
                "generated_at": _now_iso(),
            }
            robot_generated = 0
            robot_reused = 0
            for step in robot_steps:
                digest = str(step.get("text_hash") or "").strip()
                clip_rel_path = self.store.clip_rel_path(fingerprint, digest)
                clip_entry = {
                    "clip_id": clip_id_for(fingerprint, digest),
                    "clip_rel_path": clip_rel_path,
                    "created_at": _now_iso(),
                }
                if self.store.clip_exists(clip_rel_path):
                    self.store.touch_clip(clip_entry["clip_id"], clip_rel_path, profile_fingerprint=fingerprint, text_digest=digest)
                    step.setdefault("profiles", {})[fingerprint] = clip_entry
                    robot_reused += 1
                    reused_count += 1
                    continue
                try:
                    wav_bytes = self._render_for_robot(robot_cfg, str(step.get("text") or ""))
                except Exception as exc:
                    raise RuntimeError(
                        "Robot {robot_id}: preload render mislukt voor '{step_id}': {detail}".format(
                            robot_id=robot_id,
                            step_id=str(step.get("step_id") or "?"),
                            detail=_friendly_remote_error(exc),
                        )
                    ) from exc
                ensured = self.store.ensure_clip(fingerprint, digest, wav_bytes)
                clip_entry.update(ensured)
                step.setdefault("profiles", {})[fingerprint] = clip_entry
                robot_generated += 1
                generated_count += 1
            robots_out.append(
                {
                    "robot_id": robot_id,
                    "status": "generated" if robot_generated else "reused",
                    "generated_count": robot_generated,
                    "reused_count": robot_reused,
                    "message": "",
                }
            )
        self.store.save_manifest(manifest)
        self.store.refresh_index_from_manifests()
        out = self.status(script)
        out["generated_count"] = generated_count
        out["reused_count"] = reused_count
        out["robots_generation"] = robots_out
        return out

    def resolve_run_plan(self, script: JsonDict, policy_by_robot: Dict[str, Any]) -> JsonDict:
        analysis = self._analyze(script)
        manifest = analysis["manifest"]
        steps = manifest.get("steps") if isinstance(manifest.get("steps"), list) else []
        step_audio: Dict[str, JsonDict] = {}
        robot_modes: Dict[str, str] = {}
        robots_lookup = {str(item.get("robot_id") or ""): item for item in analysis["robots"]}
        for robot_id, robot_info in robots_lookup.items():
            policy_raw = policy_by_robot.get(robot_id) if isinstance(policy_by_robot, dict) else None
            policy = policy_raw if isinstance(policy_raw, dict) else {}
            mode = str(policy.get("mode") or "").strip().lower() or "live"
            if mode == "live":
                robot_modes[robot_id] = "live"
                continue
            if mode == "current":
                if not bool(robot_info.get("current_ready")):
                    raise ValueError(f"Geen volledige preload voor huidig profiel op robot {robot_id}.")
                current_profile = robot_info.get("current_profile") if isinstance(robot_info.get("current_profile"), dict) else {}
                fingerprint = str(current_profile.get("fingerprint") or "").strip()
            elif mode == "existing":
                if not bool(robot_info.get("existing_ready")):
                    raise ValueError(f"Geen volledig bestaand preload-profiel voor robot {robot_id}.")
                selected = str(policy.get("profile_fingerprint") or "").strip()
                existing_profile = robot_info.get("existing_profile") if isinstance(robot_info.get("existing_profile"), dict) else {}
                fingerprint = selected or str(existing_profile.get("fingerprint") or "").strip()
                if fingerprint != str(existing_profile.get("fingerprint") or "").strip():
                    raise ValueError(f"Onbekend bestaand preload-profiel voor robot {robot_id}.")
            else:
                raise ValueError(f"Ongeldige tts preload mode voor robot {robot_id}: {mode}")
            robot_modes[robot_id] = "preloaded_current" if mode == "current" else "preloaded_existing"
            for step in steps:
                if str(step.get("robot_id") or "").strip() != robot_id:
                    continue
                profiles = step.get("profiles") if isinstance(step.get("profiles"), dict) else {}
                clip_entry = profiles.get(fingerprint) if isinstance(profiles, dict) else None
                rel_path = _safe_rel_path((clip_entry or {}).get("clip_rel_path"))
                if not clip_entry or not rel_path or not self.store.clip_exists(rel_path):
                    raise ValueError(f"Preload clip ontbreekt voor step {step.get('step_id')} op robot {robot_id}.")
                step_audio[str(step.get("step_id") or "")] = {
                    "clip_id": str(clip_entry.get("clip_id") or "").strip(),
                    "clip_rel_path": rel_path,
                    "profile_fingerprint": fingerprint,
                }
        return {
            "ok": True,
            "script_id": analysis["script_id"],
            "robot_modes": robot_modes,
            "step_audio": step_audio,
        }
