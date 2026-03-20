from __future__ import annotations

import argparse
import os
import ipaddress
import json
import re
import socket
import subprocess
import sys
import threading
import traceback
import webbrowser
from collections import deque
from dataclasses import dataclass
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlsplit
from urllib.request import Request, urlopen

if __package__ in (None, ""):
    _REPO_ROOT = Path(__file__).resolve().parent.parent
    if str(_REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(_REPO_ROOT))
    from py3_script_runner.client import DMClient, DMClientError
    from py3_script_runner.runner import ScriptRunner
    from py3_script_runner.schema import ScriptSchemaError, validate_script
    from py3_script_runner.tts_preload import TtsPreloadService
else:
    from .client import DMClient, DMClientError
    from .runner import ScriptRunner
    from .schema import ScriptSchemaError, validate_script
    from .tts_preload import TtsPreloadService


MODULE_DIR = Path(__file__).resolve().parent
REPO_ROOT = MODULE_DIR.parent
WEB_ROOT = MODULE_DIR / "script_builder_web"
SCRIPTS_DIR = MODULE_DIR / "scripts"
DM_DIR = REPO_ROOT / "py3_dialog_manager"
DM_PYTHON = DM_DIR / "venv" / "Scripts" / "python.exe"
DM_WEBAPP = DM_DIR / "webapp_server.py"
CREATE_NEW_CONSOLE = getattr(subprocess, "CREATE_NEW_CONSOLE", 0x00000010)
EXAMPLE_FILES = {
    "example_workshop.json",
    "example_workshop_ppt.json",
    "example_workshop_summary.json",
}
ACTIVE_RUN_STATUSES = {"preflight", "running", "waiting"}
TTS_PRELOAD_SERVICE = TtsPreloadService()


@dataclass(frozen=True)
class DmLaunchSpec:
    robot_id: str
    dm_url: str
    bind_host: str
    port: int
    instance_id: str


def _local_dm_bind_host(raw_host: str) -> Optional[str]:
    host = str(raw_host or "").strip()
    if not host:
        return None
    if host.casefold() == "localhost":
        return "127.0.0.1"
    try:
        parsed = ipaddress.ip_address(host)
    except ValueError:
        return None
    if parsed.is_loopback or parsed.is_unspecified:
        return host
    return None


def _dm_launcher_prereq_error() -> str:
    if not DM_DIR.exists():
        return f"DM map niet gevonden: {DM_DIR}"
    if not DM_PYTHON.exists():
        return f"DM venv python.exe niet gevonden: {DM_PYTHON}"
    if not DM_WEBAPP.exists():
        return f"DM webapp_server.py niet gevonden: {DM_WEBAPP}"
    return ""


def _dm_launch_result(
    *,
    robot_id: str,
    dm_url: str,
    instance_id: str,
    started: bool,
    message: str,
    port: Optional[int] = None,
) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "robot_id": str(robot_id or ""),
        "dm_url": str(dm_url or ""),
        "instance_id": str(instance_id or ""),
        "started": bool(started),
        "message": str(message or ""),
    }
    if port is not None:
        result["port"] = int(port)
    if not started:
        result["error"] = str(message or "")
    return result


def _parse_dm_launch_spec(robot_id: str, robot_cfg: Dict[str, Any]) -> tuple[Optional[DmLaunchSpec], Optional[str]]:
    dm_url = str(robot_cfg.get("dm_url") or "").strip()
    instance_id = str(robot_cfg.get("instance_id") or "").strip()
    try:
        parsed = urlsplit(dm_url)
        host = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        return None, f"robots.{robot_id}.dm_url is ongeldig: {exc}"
    if parsed.scheme not in {"http", "https"} or not host:
        return None, f"robots.{robot_id}.dm_url moet een volledige http(s) URL zijn."
    if port is None:
        return None, f"robots.{robot_id}.dm_url moet een expliciete poort bevatten."
    bind_host = _local_dm_bind_host(host)
    if bind_host is None:
        return None, f"robots.{robot_id}.dm_url moet naar een lokale host wijzen om een DM te starten."
    return DmLaunchSpec(
        robot_id=str(robot_id or ""),
        dm_url=dm_url,
        bind_host=bind_host,
        port=int(port),
        instance_id=instance_id,
    ), None


def _build_dm_launch_command(spec: DmLaunchSpec) -> List[str]:
    python_cmd = [str(DM_PYTHON), str(DM_WEBAPP), "--host", spec.bind_host, "--port", str(spec.port)]
    if spec.instance_id:
        python_cmd.extend(["--instance-id", spec.instance_id])
    return ["cmd.exe", "/k", subprocess.list2cmdline(python_cmd)]


def _build_dm_launch_env() -> Dict[str, str]:
    env = dict(os.environ)
    python_dir = DM_PYTHON.parent
    scripts_dir = str(python_dir)
    venv_dir = str(python_dir.parent if python_dir.name.lower() == "scripts" else python_dir)
    path_parts = [scripts_dir]
    current_path = str(env.get("PATH") or "")
    if current_path:
        path_parts.append(current_path)
    env["PATH"] = os.pathsep.join(path_parts)
    env["VIRTUAL_ENV"] = venv_dir
    return env


def _local_target_is_occupied(spec: DmLaunchSpec) -> bool:
    probe_host = spec.bind_host
    if probe_host == "0.0.0.0":
        probe_host = "127.0.0.1"
    elif probe_host == "::":
        probe_host = "::1"
    try:
        with socket.create_connection((probe_host, spec.port), timeout=0.25):
            return True
    except OSError:
        return False


def _start_dm_process(spec: DmLaunchSpec) -> Dict[str, Any]:
    cmd = _build_dm_launch_command(spec)
    try:
        subprocess.Popen(
            cmd,
            cwd=str(DM_DIR),
            env=_build_dm_launch_env(),
            creationflags=CREATE_NEW_CONSOLE,
        )
    except OSError as exc:
        return _dm_launch_result(
            robot_id=spec.robot_id,
            dm_url=spec.dm_url,
            instance_id=spec.instance_id,
            started=False,
            message=f"DM starten mislukt: {exc}",
            port=spec.port,
        )
    return _dm_launch_result(
        robot_id=spec.robot_id,
        dm_url=spec.dm_url,
        instance_id=spec.instance_id,
        started=True,
        message="DM gestart in een nieuw cmd-venster.",
        port=spec.port,
    )


def start_dialog_managers(payload: Dict[str, Any]) -> tuple[int, Dict[str, Any]]:
    script_raw = payload.get("script") if isinstance(payload, dict) else None
    if not isinstance(script_raw, dict):
        return HTTPStatus.BAD_REQUEST, RunSessionManager._error_payload("Expected JSON body with object field 'script'.")
    try:
        script = validate_script(script_raw)
    except ScriptSchemaError as exc:
        return HTTPStatus.BAD_REQUEST, RunSessionManager._error_payload(f"Schema error: {exc}")

    prereq_error = _dm_launcher_prereq_error()
    if prereq_error:
        return HTTPStatus.INTERNAL_SERVER_ERROR, RunSessionManager._error_payload(prereq_error)

    robot_items = list((script.get("robots") or {}).items())
    results: List[Optional[Dict[str, Any]]] = [None] * len(robot_items)
    specs_by_index: Dict[int, DmLaunchSpec] = {}
    target_map: Dict[int, List[int]] = {}

    for index, (robot_id, robot_cfg_raw) in enumerate(robot_items):
        robot_cfg = robot_cfg_raw if isinstance(robot_cfg_raw, dict) else {}
        spec, error = _parse_dm_launch_spec(str(robot_id or ""), robot_cfg)
        if error:
            results[index] = _dm_launch_result(
                robot_id=str(robot_id or ""),
                dm_url=str(robot_cfg.get("dm_url") or ""),
                instance_id=str(robot_cfg.get("instance_id") or "").strip(),
                started=False,
                message=error,
            )
            continue
        specs_by_index[index] = spec
        target_map.setdefault(spec.port, []).append(index)

    for positions in target_map.values():
        if len(positions) < 2:
            continue
        first = specs_by_index[positions[0]]
        target_label = f"{first.bind_host}:{first.port}"
        for pos in positions:
            spec = specs_by_index[pos]
            results[pos] = _dm_launch_result(
                robot_id=spec.robot_id,
                dm_url=spec.dm_url,
                instance_id=spec.instance_id,
                started=False,
                message=f"Dubbele lokale DM target in script: {target_label}.",
                port=spec.port,
            )

    for index, spec in specs_by_index.items():
        if results[index] is not None:
            continue
        if _local_target_is_occupied(spec):
            results[index] = _dm_launch_result(
                robot_id=spec.robot_id,
                dm_url=spec.dm_url,
                instance_id=spec.instance_id,
                started=False,
                message=f"Er draait al iets op {spec.dm_url}.",
                port=spec.port,
            )
            continue
        results[index] = _start_dm_process(spec)

    final_results = [item for item in results if item is not None]
    started_count = sum(1 for item in final_results if item.get("started"))
    error_count = len(final_results) - started_count
    if started_count and not error_count:
        message = f"{started_count} DM{'s' if started_count != 1 else ''} gestart."
    elif started_count and error_count:
        message = f"{started_count} DM gestart, {error_count} fout."
    else:
        message = "Geen DM's gestart."
    return HTTPStatus.OK, {
        "ok": True,
        "results": final_results,
        "started_count": started_count,
        "error_count": error_count,
        "message": message,
    }


def _normalize_cmdrec_labels(raw_labels: Any) -> List[str]:
    seen = set()
    labels: List[str] = []
    if not isinstance(raw_labels, list):
        return labels
    for item in raw_labels:
        label = str(item or "").strip()
        if not label:
            continue
        if label.upper() == "NONE":
            continue
        key = label.casefold()
        if key in seen:
            continue
        seen.add(key)
        labels.append(label)
    labels.sort(key=lambda item: item.casefold())
    return labels


def _candidate_cmdrec_bundle_roots(bundles_dir: str) -> List[Path]:
    raw = Path(str(bundles_dir or "dist"))
    if raw.is_absolute():
        return [raw]
    return [
        REPO_ROOT / raw,
        REPO_ROOT / "py3_command_recognition_train" / raw,
    ]


def _resolve_cmdrec_bundles_dir(bundles_dir: str) -> Path:
    candidates = _candidate_cmdrec_bundle_roots(bundles_dir)
    for candidate in candidates:
        if candidate.is_dir():
            return candidate.resolve()
    return candidates[-1].resolve()


def _resolve_cmdrec_bundle_path(cmdrec_value: str, bundles_dir: str) -> Path:
    bundles_path = _resolve_cmdrec_bundles_dir(bundles_dir)
    available = [path for path in bundles_path.iterdir()] if bundles_path.exists() else []
    available_dirs = [path for path in available if path.is_dir()]
    value = str(cmdrec_value or "latest").strip().lower()

    if value == "latest":
        versioned = []
        for path in available_dirs:
            match = re.match(r"bundle_v(\d+)(?:_(\d{8}))?$", path.name)
            if not match:
                continue
            version = int(match.group(1))
            date_part = int(match.group(2)) if match.group(2) else 0
            versioned.append((version, date_part, path))
        if not versioned:
            raise ValueError(f"Geen cmdrec bundles gevonden in {bundles_path}.")
        versioned.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return versioned[0][2].resolve()

    match = re.match(r"^(?:bundle_)?v(\d+)(?:_(\d{8}))?$", value)
    if match:
        version = int(match.group(1))
        date_part = match.group(2)
        suffix = f"_{date_part}" if date_part else ""
        candidate = bundles_path / f"bundle_v{version}{suffix}"
        if candidate.is_dir():
            return candidate.resolve()

    explicit = Path(cmdrec_value)
    if explicit.is_dir():
        return explicit.resolve()
    if not explicit.is_absolute():
        for root in [REPO_ROOT, REPO_ROOT / "py3_command_recognition_train", bundles_path]:
            candidate = (root / explicit).resolve()
            if candidate.is_dir():
                return candidate

    raise ValueError(f"Cmdrec bundle niet gevonden voor '{cmdrec_value}' in {bundles_path}.")


def _fetch_cmdrec_labels(dm_url: str) -> tuple[List[str], Optional[str]]:
    base_url = str(dm_url or "").strip().rstrip("/")
    if not base_url:
        return [], "dm_url ontbreekt."
    url = base_url + "/api/cmdrec_labels"
    request = Request(url, method="GET", headers={"Accept": "application/json"})
    try:
        with urlopen(request, timeout=2.0) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        return [], f"{url} returned HTTP {exc.code}"
    except URLError as exc:
        reason = getattr(exc, "reason", exc)
        return [], f"{url} failed: {reason}"
    except Exception as exc:
        return [], f"{url} failed: {exc}"
    if not isinstance(payload, dict):
        return [], f"{url} returned ongeldige JSON."
    if not payload.get("ok"):
        return [], str(payload.get("error") or f"{url} returned ok=false")
    return _normalize_cmdrec_labels(payload.get("labels")), None


def _load_local_cmdrec_labels(*, cmdrec_value: str = "latest", bundles_dir: str = "dist") -> tuple[List[str], Optional[str]]:
    try:
        bundle_path = _resolve_cmdrec_bundle_path(str(cmdrec_value or "latest"), str(bundles_dir or "dist"))
    except Exception as exc:
        return [], f"Lokale cmdrec bundle niet gevonden: {exc}"
    labels_path = Path(bundle_path) / "labels.json"
    if not labels_path.is_file():
        return [], f"Lokale labels ontbreken: {labels_path}"
    try:
        payload = json.loads(labels_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return [], f"Lokale labels lezen mislukt: {exc}"
    return _normalize_cmdrec_labels(payload), None


def fetch_cmdrec_labels(payload: Dict[str, Any]) -> tuple[int, Dict[str, Any]]:
    script_raw = payload.get("script") if isinstance(payload, dict) else None
    if not isinstance(script_raw, dict):
        return HTTPStatus.BAD_REQUEST, RunSessionManager._error_payload("Expected JSON body with object field 'script'.")
    try:
        script = validate_script(script_raw)
    except ScriptSchemaError as exc:
        return HTTPStatus.BAD_REQUEST, RunSessionManager._error_payload(f"Schema error: {exc}")

    robots = script.get("robots") or {}
    dm_urls: List[str] = []
    seen = set()
    if isinstance(robots, dict):
        for robot_cfg in robots.values():
            if not isinstance(robot_cfg, dict):
                continue
            dm_url = str(robot_cfg.get("dm_url") or "").strip()
            if not dm_url:
                continue
            key = dm_url.casefold()
            if key in seen:
                continue
            seen.add(key)
            dm_urls.append(dm_url)

    labels: List[str] = []
    errors: List[str] = []
    local_labels, local_error = _load_local_cmdrec_labels()
    labels.extend(local_labels)
    if local_error:
        errors.append(local_error)
    for dm_url in dm_urls:
        fetched, error = _fetch_cmdrec_labels(dm_url)
        labels.extend(fetched)
        if error:
            errors.append(error)

    return HTTPStatus.OK, {
        "ok": True,
        "labels": _normalize_cmdrec_labels(labels),
        "sources": dm_urls,
        "errors": errors,
    }


def _iter_script_dm_targets(script: Dict[str, Any]) -> List[Dict[str, str]]:
    targets: List[Dict[str, str]] = []
    robots = script.get("robots") or {}
    if not isinstance(robots, dict):
        return targets
    for robot_id, robot_cfg_raw in robots.items():
        robot_cfg = robot_cfg_raw if isinstance(robot_cfg_raw, dict) else {}
        dm_url = str(robot_cfg.get("dm_url") or "").strip()
        if not dm_url:
            continue
        targets.append(
            {
                "robot_id": str(robot_id or ""),
                "dm_url": dm_url,
                "instance_id": str(robot_cfg.get("instance_id") or "").strip(),
            }
        )
    return targets


def poll_auto_rest_watch(payload: Dict[str, Any]) -> tuple[int, Dict[str, Any]]:
    script_raw = payload.get("script") if isinstance(payload, dict) else None
    if not isinstance(script_raw, dict):
        return HTTPStatus.BAD_REQUEST, RunSessionManager._error_payload("Expected JSON body with object field 'script'.")
    try:
        script = validate_script(script_raw)
    except ScriptSchemaError as exc:
        return HTTPStatus.BAD_REQUEST, RunSessionManager._error_payload(f"Schema error: {exc}")

    robots_payload: List[Dict[str, Any]] = []
    for target in _iter_script_dm_targets(script):
        dm_url = target["dm_url"]
        try:
            client = DMClient(dm_url, timeout_s=4.0)
            state = client.nao_command_state(timeout_s=4.0)
            reachable_raw = state.get("reachable") if isinstance(state, dict) else None
            reachable = bool(reachable_raw) if isinstance(reachable_raw, bool) else bool(state.get("ok"))
            awake = state.get("awake") if isinstance(state, dict) else {}
            posture = state.get("posture") if isinstance(state, dict) else {}
            auto_rest = state.get("auto_rest") if isinstance(state, dict) else {}
            awake = awake if isinstance(awake, dict) else {}
            posture = posture if isinstance(posture, dict) else {}
            auto_rest = auto_rest if isinstance(auto_rest, dict) else {}
            errors = state.get("errors") if isinstance(state, dict) else []
            error = ""
            if isinstance(errors, list):
                for item in errors:
                    text = str(item or "").strip()
                    if text:
                        error = text
                        break
            if not error and isinstance(state, dict):
                error = str(state.get("error") or "").strip()
            remaining_raw = auto_rest.get("seconds_until_rest")
            try:
                remaining = int(remaining_raw) if remaining_raw is not None else None
            except Exception:
                remaining = None
            if remaining is not None:
                auto_rest = dict(auto_rest)
                auto_rest["seconds_until_rest"] = remaining
            robots_payload.append(
                {
                    "robot_id": target["robot_id"],
                    "dm_url": dm_url,
                    "instance_id": target["instance_id"],
                    "ok": reachable,
                    "reachable": reachable,
                    "awake": awake,
                    "posture": posture,
                    "auto_rest": auto_rest,
                    "error": error,
                }
            )
        except Exception as exc:
            robots_payload.append(
                {
                    "robot_id": target["robot_id"],
                    "dm_url": dm_url,
                    "instance_id": target["instance_id"],
                    "ok": False,
                    "reachable": False,
                    "awake": {},
                    "posture": {},
                    "error": str(exc),
                    "auto_rest": {},
                }
            )

    return HTTPStatus.OK, {"ok": True, "robots": robots_payload}


class RunSessionManager:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._run_thread: Optional[threading.Thread] = None
        self._abort_event: Optional[threading.Event] = None
        self._continue_event: Optional[threading.Event] = None
        self._run_counter = 0
        self._log_tail: deque[str] = deque(maxlen=300)
        self._state: Dict[str, Any] = {
            "status": "idle",
            "run_id": "",
            "waiting_for_next": False,
            "waiting_reason": "none",
            "current_step_index": None,
            "current_step_id": "",
            "completed_steps": 0,
            "total_steps": 0,
            "last_error": None,
            "log_path": "",
        }

    @staticmethod
    def _error_payload(message: str) -> Dict[str, Any]:
        return {"ok": False, "error": str(message)}

    def _snapshot_locked(self) -> Dict[str, Any]:
        snapshot = dict(self._state)
        snapshot["log_tail"] = list(self._log_tail)
        snapshot["ok"] = True
        return snapshot

    def state(self) -> Dict[str, Any]:
        with self._lock:
            return self._snapshot_locked()

    def _start_new_run_locked(self, script: Dict[str, Any], *, run_options: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        self._run_counter += 1
        run_id = f"run_{self._run_counter}"
        self._abort_event = threading.Event()
        self._continue_event = threading.Event()
        self._log_tail.clear()
        self._state = {
            "status": "preflight",
            "run_id": run_id,
            "waiting_for_next": False,
            "waiting_reason": "none",
            "current_step_index": None,
            "current_step_id": "",
            "completed_steps": 0,
            "total_steps": len(list(script.get("steps") or [])),
            "last_error": None,
            "log_path": "",
        }
        thread = threading.Thread(target=self._run_worker, args=(run_id, script, dict(run_options or {})), daemon=True)
        self._run_thread = thread
        thread.start()
        return self._snapshot_locked()

    def start(self, payload: Dict[str, Any]) -> tuple[int, Dict[str, Any]]:
        script_raw = payload.get("script") if isinstance(payload, dict) else None
        if not isinstance(script_raw, dict):
            return HTTPStatus.BAD_REQUEST, self._error_payload("Expected JSON body with object field 'script'.")
        try:
            script = validate_script(script_raw)
        except ScriptSchemaError as exc:
            return HTTPStatus.BAD_REQUEST, self._error_payload(f"Schema error: {exc}")
        run_options: Dict[str, Any] = {}
        tts_preload_raw = payload.get("tts_preload") if isinstance(payload, dict) else None
        if isinstance(tts_preload_raw, dict):
            policy_by_robot_raw = tts_preload_raw.get("policy_by_robot")
            policy_by_robot = policy_by_robot_raw if isinstance(policy_by_robot_raw, dict) else {}
            try:
                resolved = TTS_PRELOAD_SERVICE.resolve_run_plan(script, policy_by_robot)
            except Exception as exc:
                return HTTPStatus.BAD_REQUEST, self._error_payload(f"TTS preload error: {exc}")
            run_options["tts_preload_root"] = TTS_PRELOAD_SERVICE.store.root
            run_options["tts_preload_step_audio"] = resolved.get("step_audio") or {}
            run_options["tts_preload_robot_modes"] = resolved.get("robot_modes") or {}

        with self._lock:
            status = str(self._state.get("status") or "")
            if status in ACTIVE_RUN_STATUSES:
                return HTTPStatus.CONFLICT, self._error_payload("A run is already active.")
            snapshot = self._start_new_run_locked(script, run_options=run_options)
            return HTTPStatus.ACCEPTED, snapshot

    def request_next(self) -> tuple[int, Dict[str, Any]]:
        with self._lock:
            status = str(self._state.get("status") or "")
            waiting_for_next = bool(self._state.get("waiting_for_next"))
            if status not in ACTIVE_RUN_STATUSES or not waiting_for_next or self._continue_event is None:
                return HTTPStatus.CONFLICT, self._error_payload("Run is not waiting for next.")
            self._continue_event.set()
            return HTTPStatus.ACCEPTED, self._snapshot_locked()

    def request_abort(self) -> tuple[int, Dict[str, Any]]:
        with self._lock:
            status = str(self._state.get("status") or "")
            if status not in ACTIVE_RUN_STATUSES:
                return HTTPStatus.CONFLICT, self._error_payload("No active run.")
            if self._abort_event is not None:
                self._abort_event.set()
            self._state["last_error"] = "Abort requested from web UI."
            return HTTPStatus.ACCEPTED, self._snapshot_locked()

    def _handle_runner_event(self, run_id: str, event: Dict[str, Any]) -> None:
        event_type = str(event.get("type") or "").strip().lower()
        with self._lock:
            if run_id != str(self._state.get("run_id") or ""):
                return
            if event_type == "log":
                msg = str(event.get("message") or "").strip()
                if msg:
                    self._log_tail.append(msg)
                return
            if event_type == "status":
                status = str(event.get("status") or "").strip().lower()
                if status in {"preflight", "running", "waiting", "completed", "aborted", "failed"}:
                    self._state["status"] = status
                if "completed_steps" in event:
                    self._state["completed_steps"] = int(event.get("completed_steps") or 0)
                if "total_steps" in event:
                    self._state["total_steps"] = int(event.get("total_steps") or 0)
                err = str(event.get("error") or "").strip()
                if err:
                    self._state["last_error"] = err
                if status in {"completed", "aborted", "failed"}:
                    self._state["waiting_for_next"] = False
                    self._state["waiting_reason"] = "none"
                return
            if event_type == "waiting":
                self._state["status"] = "waiting"
                self._state["waiting_for_next"] = True
                self._state["waiting_reason"] = str(event.get("waiting_reason") or "none")
                self._state["current_step_index"] = int(event.get("index")) if event.get("index") is not None else None
                self._state["current_step_id"] = str(event.get("step_id") or "")
                return
            if event_type == "waiting_cleared":
                self._state["waiting_for_next"] = False
                self._state["waiting_reason"] = "none"
                if str(self._state.get("status") or "") == "waiting":
                    self._state["status"] = "running"
                return
            if event_type == "step_start":
                self._state["status"] = "running"
                self._state["current_step_index"] = int(event.get("index")) if event.get("index") is not None else None
                self._state["current_step_id"] = str(event.get("step_id") or "")
                if event.get("total") is not None:
                    self._state["total_steps"] = int(event.get("total") or 0)
                return
            if event_type == "step_done":
                self._state["current_step_index"] = int(event.get("index")) if event.get("index") is not None else None
                self._state["current_step_id"] = str(event.get("step_id") or "")
                if "completed_steps" in event:
                    self._state["completed_steps"] = int(event.get("completed_steps") or 0)
                elif str(event.get("status") or "") == "completed":
                    self._state["completed_steps"] = int(self._state.get("completed_steps") or 0) + 1
                if event.get("total") is not None:
                    self._state["total_steps"] = int(event.get("total") or 0)
                return
            if event_type == "step_error":
                err = str(event.get("error") or "").strip()
                if err:
                    step_id = str(event.get("step_id") or "").strip()
                    index = event.get("index")
                    total = event.get("total")
                    prefix = ""
                    if index is not None and total is not None:
                        try:
                            prefix = f"[{int(index) + 1}/{int(total)}] "
                        except Exception:
                            prefix = ""
                    if step_id:
                        prefix += f"{step_id}: "
                    full_error = f"{prefix}{err}" if prefix else err
                    self._state["last_error"] = full_error
                    log_line = f"[RUN][STEP_ERROR] {full_error}"
                    if not self._log_tail or self._log_tail[-1] != log_line:
                        self._log_tail.append(log_line)

    def _set_terminal_failure(self, run_id: str, error_message: str) -> None:
        with self._lock:
            if run_id != str(self._state.get("run_id") or ""):
                return
            self._state["status"] = "failed"
            self._state["waiting_for_next"] = False
            self._state["waiting_reason"] = "none"
            self._state["last_error"] = str(error_message)
            self._log_tail.append(f"[RUN] FAILED: {error_message}")

    def _run_worker(self, run_id: str, script: Dict[str, Any], run_options: Dict[str, Any]) -> None:
        abort_event: Optional[threading.Event]
        continue_event: Optional[threading.Event]
        with self._lock:
            abort_event = self._abort_event
            continue_event = self._continue_event

        runner = ScriptRunner(
            script,
            continue_event=continue_event,
            abort_event=abort_event,
            on_error_prompt_policy="next",
            summary_publish_policy="publish",
            ppt_mismatch_policy="defer_snapback",
            event_sink=lambda event: self._handle_runner_event(run_id, event),
            tts_preload_root=run_options.get("tts_preload_root"),
            tts_preload_step_audio=run_options.get("tts_preload_step_audio"),
            tts_preload_robot_modes=run_options.get("tts_preload_robot_modes"),
        )
        try:
            runner.preflight()
            result = runner.run()
        except Exception as exc:
            tb = traceback.format_exc(limit=1)
            tb_line = tb.strip().splitlines()[-1] if tb else ""
            message = str(exc or "Run failed")
            if tb_line and tb_line != message:
                message = f"{message} ({tb_line})"
            self._set_terminal_failure(run_id, message)
            return
        finally:
            try:
                runner.close()
            except Exception:
                pass

        with self._lock:
            if run_id != str(self._state.get("run_id") or ""):
                return
            self._state["completed_steps"] = int(result.completed_steps)
            self._state["total_steps"] = int(result.total_steps)
            self._state["waiting_for_next"] = False
            self._state["waiting_reason"] = "none"
            self._state["log_path"] = str(result.log_path)
            self._state["status"] = "aborted" if result.aborted else "completed"


RUN_SESSIONS = RunSessionManager()


class ScriptBuilderHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(WEB_ROOT), **kwargs)

    def _send_json(self, status: int, payload: Dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self) -> tuple[bool, Dict[str, Any], str]:
        raw_len = self.headers.get("Content-Length", "0")
        try:
            length = int(raw_len)
        except Exception:
            return False, {}, "Invalid Content-Length header."
        if length <= 0:
            return False, {}, "Request body is required."
        try:
            data = self.rfile.read(length)
        except Exception as exc:
            return False, {}, f"Could not read request body: {exc}"
        try:
            parsed = json.loads(data.decode("utf-8"))
        except Exception as exc:
            return False, {}, f"Invalid JSON body: {exc}"
        if not isinstance(parsed, dict):
            return False, {}, "JSON root must be an object."
        return True, parsed, ""

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlsplit(self.path)
        if parsed.path.startswith("/examples/"):
            name = parsed.path[len("/examples/") :]
            self._serve_example_file(name)
            return
        if parsed.path == "/api/run/state":
            self._send_json(HTTPStatus.OK, RUN_SESSIONS.state())
            return
        super().do_GET()

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlsplit(self.path)
        if parsed.path == "/api/dm/start":
            ok, payload, error = self._read_json_body()
            if not ok:
                self._send_json(HTTPStatus.BAD_REQUEST, RunSessionManager._error_payload(error))
                return
            status, response = start_dialog_managers(payload)
            self._send_json(status, response)
            return
        if parsed.path == "/api/cmdrec/labels":
            ok, payload, error = self._read_json_body()
            if not ok:
                self._send_json(HTTPStatus.BAD_REQUEST, RunSessionManager._error_payload(error))
                return
            status, response = fetch_cmdrec_labels(payload)
            self._send_json(status, response)
            return
        if parsed.path == "/api/auto_rest_watch/status":
            ok, payload, error = self._read_json_body()
            if not ok:
                self._send_json(HTTPStatus.BAD_REQUEST, RunSessionManager._error_payload(error))
                return
            status, response = poll_auto_rest_watch(payload)
            self._send_json(status, response)
            return
        if parsed.path == "/api/tts_preload/status":
            ok, payload, error = self._read_json_body()
            if not ok:
                self._send_json(HTTPStatus.BAD_REQUEST, RunSessionManager._error_payload(error))
                return
            script_raw = payload.get("script") if isinstance(payload, dict) else None
            if not isinstance(script_raw, dict):
                self._send_json(HTTPStatus.BAD_REQUEST, RunSessionManager._error_payload("Expected JSON body with object field 'script'."))
                return
            try:
                script = validate_script(script_raw)
                response = TTS_PRELOAD_SERVICE.status(script)
            except ScriptSchemaError as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, RunSessionManager._error_payload(f"Schema error: {exc}"))
                return
            except Exception as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, RunSessionManager._error_payload(str(exc)))
                return
            self._send_json(HTTPStatus.OK, response)
            return
        if parsed.path == "/api/tts_preload/generate":
            ok, payload, error = self._read_json_body()
            if not ok:
                self._send_json(HTTPStatus.BAD_REQUEST, RunSessionManager._error_payload(error))
                return
            script_raw = payload.get("script") if isinstance(payload, dict) else None
            if not isinstance(script_raw, dict):
                self._send_json(HTTPStatus.BAD_REQUEST, RunSessionManager._error_payload("Expected JSON body with object field 'script'."))
                return
            try:
                script = validate_script(script_raw)
                response = TTS_PRELOAD_SERVICE.generate(script)
            except ScriptSchemaError as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, RunSessionManager._error_payload(f"Schema error: {exc}"))
                return
            except Exception as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, RunSessionManager._error_payload(str(exc)))
                return
            self._send_json(HTTPStatus.OK, response)
            return
        if parsed.path == "/api/tts_preload/prune":
            ok, payload, error = self._read_json_body()
            if not ok:
                self._send_json(HTTPStatus.BAD_REQUEST, RunSessionManager._error_payload(error))
                return
            try:
                response = TTS_PRELOAD_SERVICE.store.prune(str(payload.get("policy") or ""))
            except Exception as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, RunSessionManager._error_payload(str(exc)))
                return
            self._send_json(HTTPStatus.OK, response)
            return
        if parsed.path == "/api/run/start":
            ok, payload, error = self._read_json_body()
            if not ok:
                self._send_json(HTTPStatus.BAD_REQUEST, RunSessionManager._error_payload(error))
                return
            status, response = RUN_SESSIONS.start(payload)
            self._send_json(status, response)
            return
        if parsed.path == "/api/run/next":
            status, response = RUN_SESSIONS.request_next()
            self._send_json(status, response)
            return
        if parsed.path == "/api/run/abort":
            status, response = RUN_SESSIONS.request_abort()
            self._send_json(status, response)
            return
        self.send_error(HTTPStatus.NOT_FOUND, "Unknown endpoint.")

    def _serve_example_file(self, raw_name: str) -> None:
        safe_name = Path(unquote(raw_name)).name
        if safe_name not in EXAMPLE_FILES:
            self.send_error(HTTPStatus.NOT_FOUND, "Example file not found.")
            return
        target = SCRIPTS_DIR / safe_name
        if not target.exists() or not target.is_file():
            self.send_error(HTTPStatus.NOT_FOUND, "Example file missing.")
            return

        try:
            payload = target.read_bytes()
        except OSError as exc:
            self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))
            return

        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the standalone Script Builder webapp.")
    parser.add_argument("--host", default="127.0.0.1", help="Host interface to bind (default: 127.0.0.1).")
    parser.add_argument("--port", default=8765, type=int, help="Port to bind (default: 8765).")
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Do not open a browser window automatically.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not WEB_ROOT.exists():
        print(f"Missing web root: {WEB_ROOT}", file=sys.stderr)
        return 2

    try:
        server = ThreadingHTTPServer((args.host, args.port), ScriptBuilderHandler)
    except OSError as exc:
        print(f"Failed to bind {args.host}:{args.port}: {exc}", file=sys.stderr)
        return 3

    url = f"http://{args.host}:{args.port}/"
    print("Script Builder is running.")
    print(f"URL: {url}")
    print("Press Ctrl+C to stop.")

    if not args.no_browser:
        try:
            webbrowser.open(url, new=1)
        except Exception:
            pass

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping Script Builder...")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
