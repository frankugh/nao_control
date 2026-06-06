from __future__ import annotations

import base64
import io
from concurrent.futures import ThreadPoolExecutor, as_completed

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Optional
import threading
import sys
import time
import uuid
import wave

from .client import DMClient
from .ppt_controller import ComPptController, PPTControllerError, PptControllerProtocol


ClientFactory = Callable[[str, float], DMClient]
InputFunc = Callable[[str], str]
SleepFunc = Callable[[float], None]
RunnerEventSink = Callable[[Dict[str, Any]], None]


class _RunAbortRequested(RuntimeError):
    pass


@dataclass
class RunResult:
    completed_steps: int
    total_steps: int
    aborted: bool
    log_path: Path


@dataclass
class _AutoRestLease:
    robot_id: str
    lease_id: str
    owner: str
    reason: str
    ttl_s: float
    dm_url: str
    expires_at_monotonic: float


class ScriptRunner:
    def __init__(
        self,
        script: Dict[str, Any],
        *,
        client_factory: Optional[ClientFactory] = None,
        input_func: Optional[InputFunc] = None,
        sleep_func: Optional[SleepFunc] = None,
        log_dir: Optional[Path] = None,
        ppt_controller: Optional[PptControllerProtocol] = None,
        continue_event: Optional[threading.Event] = None,
        abort_event: Optional[threading.Event] = None,
        on_error_prompt_policy: str = "prompt",
        summary_publish_policy: str = "prompt",
        ppt_mismatch_policy: str = "pause",
        event_sink: Optional[RunnerEventSink] = None,
        tts_preload_root: Optional[Path] = None,
        tts_preload_step_audio: Optional[Dict[str, Dict[str, Any]]] = None,
        tts_preload_robot_modes: Optional[Dict[str, str]] = None,
        log_to_stdout: bool = True,
    ) -> None:
        self.script = script
        self.client_factory = client_factory or (lambda url, timeout_s: DMClient(url, timeout_s=timeout_s))
        self.input_func = input_func or input
        self.sleep_func = sleep_func or time.sleep
        self.continue_event = continue_event
        self.abort_event = abort_event
        self.event_sink = event_sink
        self.tts_preload_root = Path(tts_preload_root) if tts_preload_root is not None else None
        self.tts_preload_step_audio = dict(tts_preload_step_audio or {})
        self.tts_preload_robot_modes = {
            str(robot_id): str(mode or "").strip().lower()
            for robot_id, mode in dict(tts_preload_robot_modes or {}).items()
            if str(robot_id or "").strip()
        }
        self.log_to_stdout = bool(log_to_stdout)
        self.defaults = dict(script.get("defaults") or {})
        self.on_error_prompt_policy = self._normalize_policy(
            on_error_prompt_policy,
            allowed={"prompt", "next", "abort"},
            default="prompt",
        )
        self.summary_publish_policy = self._normalize_policy(
            summary_publish_policy,
            allowed={"prompt", "publish", "cancel"},
            default="prompt",
        )
        self.ppt_mismatch_policy = self._normalize_policy(
            ppt_mismatch_policy,
            allowed={"pause", "defer_snapback", "abort"},
            default="pause",
        )
        self.control_poll_interval_s = self._parse_positive_float(
            self.defaults.get("control_poll_interval_s", 0.25),
            default=0.25,
        )
        self.ppt_cfg = dict(script.get("ppt") or {})
        self.ppt_enabled = bool(self.ppt_cfg.get("enabled", False))
        self._ppt_controller = ppt_controller
        self._ppt_prepared = False
        self._capture_on = True
        self._last_script_ppt_position: Optional[Dict[str, int]] = None
        self._pending_snapback = False
        self._waiting_for_next = False
        self._waiting_reason = "none"
        self.summary_live_poll_interval_s = self._parse_positive_float(
            self.defaults.get("summary_live_poll_interval_s", 0.75),
            default=0.75,
        )
        self._summary_open_nonce = 0

        self.log_dir = Path(log_dir) if log_dir is not None else (Path(__file__).resolve().parent / "logs")
        self.log_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.log_path = self.log_dir / f"run_{stamp}.log"
        self._log_handle = self.log_path.open("a", encoding="utf-8")
        self._closed = False

        self.clients: Dict[str, DMClient] = {}
        self.robot_cfgs: Dict[str, Dict[str, Any]] = {}
        self.auto_rest_suspend_owner = "script_runner"
        self.auto_rest_suspend_reason = "script_run"
        self.auto_rest_suspend_ttl_s = self._parse_positive_float(
            self.defaults.get("auto_rest_suspend_ttl_s", 300.0),
            default=300.0,
        )
        self.auto_rest_suspend_renew_interval_s = 5.0
        self._auto_rest_lease_lock = threading.RLock()
        self._auto_rest_lease_stop = threading.Event()
        self._auto_rest_lease_thread: Optional[threading.Thread] = None
        self._auto_rest_lease_failure: Optional[str] = None
        self._auto_rest_leases: Dict[str, _AutoRestLease] = {}
        self.readiness_poll_interval_s = self._parse_positive_float(
            self.defaults.get("readiness_poll_interval_s", 3.0),
            default=3.0,
        )
        self.readiness_timeout_s = self._parse_nonnegative_float(
            self.defaults.get("readiness_timeout_s", 0.0),
            default=0.0,
        )
        self.readiness_request_timeout_s = self._parse_positive_float(
            self.defaults.get("readiness_request_timeout_s", 3.0),
            default=3.0,
        )
        self._build_clients()

    @staticmethod
    def _parse_positive_float(value: Any, *, default: float) -> float:
        try:
            parsed = float(value)
        except Exception:
            return float(default)
        if parsed <= 0:
            return float(default)
        return float(parsed)

    @staticmethod
    def _parse_nonnegative_float(value: Any, *, default: float) -> float:
        try:
            parsed = float(value)
        except Exception:
            return float(default)
        if parsed < 0:
            return float(default)
        return float(parsed)

    @staticmethod
    def _normalize_policy(value: Any, *, allowed: set[str], default: str) -> str:
        raw = str(value or "").strip().lower()
        if raw in allowed:
            return raw
        return str(default)

    @staticmethod
    def _wav_duration_s(wav_bytes: bytes) -> float:
        try:
            with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
                sample_rate = float(wf.getframerate())
                if sample_rate <= 0:
                    return 0.0
                return float(wf.getnframes()) / sample_rate
        except Exception:
            return 0.0

    def _preloaded_say_timeout_s(self, clip_bytes: bytes, fallback_timeout_s: float) -> float:
        duration_s = self._wav_duration_s(clip_bytes)
        if duration_s <= 0.0:
            return float(fallback_timeout_s)
        return max(float(fallback_timeout_s), min(duration_s + 5.0, 30.0))

    @staticmethod
    def _normalize_position(raw: Dict[str, Any]) -> Dict[str, int]:
        slide = max(1, int(raw.get("slide", 1)))
        build = max(0, int(raw.get("build", 0)))
        return {"slide": slide, "build": build}

    @staticmethod
    def _format_position(pos: Dict[str, int]) -> str:
        return f"slide={int(pos.get('slide', 0))} build={int(pos.get('build', 0))}"

    def _build_clients(self) -> None:
        timeout_s = float(self.defaults.get("request_timeout_s", 12))
        for robot_id, cfg in (self.script.get("robots") or {}).items():
            robot_cfg = dict(cfg or {})
            self.robot_cfgs[robot_id] = robot_cfg
            dm_url = str(robot_cfg.get("dm_url") or "").strip()
            self.clients[robot_id] = self.client_factory(dm_url, timeout_s)

    def _auto_rest_timeout_hint_s(self) -> float:
        return max(self.readiness_request_timeout_s, 8.0)

    @staticmethod
    def _client_auto_rest_suspend_acquire(
        client: DMClient,
        *,
        lease_id: str,
        owner: str,
        reason: str,
        ttl_s: float,
        timeout_s: float,
    ) -> Dict[str, Any]:
        fn = getattr(client, "auto_rest_suspend_acquire", None)
        if not callable(fn):
            return {
                "ok": True,
                "lease_id": lease_id,
                "owner": owner,
                "reason": reason,
                "ttl_s": ttl_s,
            }
        return fn(lease_id=lease_id, owner=owner, reason=reason, ttl_s=ttl_s, timeout_s=timeout_s)

    @staticmethod
    def _client_auto_rest_suspend_renew(
        client: DMClient,
        *,
        lease_id: str,
        owner: str,
        ttl_s: float,
        timeout_s: float,
    ) -> Dict[str, Any]:
        fn = getattr(client, "auto_rest_suspend_renew", None)
        if not callable(fn):
            return {"ok": True, "lease_id": lease_id, "owner": owner, "ttl_s": ttl_s}
        return fn(lease_id=lease_id, owner=owner, ttl_s=ttl_s, timeout_s=timeout_s)

    @staticmethod
    def _client_auto_rest_suspend_release(
        client: DMClient,
        *,
        lease_id: str,
        timeout_s: float,
    ) -> Dict[str, Any]:
        fn = getattr(client, "auto_rest_suspend_release", None)
        if not callable(fn):
            return {"ok": True, "lease_id": lease_id}
        return fn(lease_id=lease_id, timeout_s=timeout_s)

    def _auto_rest_acquire_failure_message(self, robot_id: str, exc: Exception) -> str:
        robot_cfg = self.robot_cfgs.get(robot_id) or {}
        dm_url = str(robot_cfg.get("dm_url") or "").strip() or "-"
        detail = ""
        lowered = str(exc or "").strip().lower()
        if "auto-rest suspend already active" in lowered:
            detail = (
                " Er is al een andere of achtergebleven auto-rest suspend lease actief. "
                "Dit blijft blocker, omdat deze runner die lease niet bezit en dus ook niet kan vernieuwen of veilig vrijgeven."
            )
        return (
            f"Robot {robot_id}: auto-rest suspend kon niet worden geactiveerd op {dm_url} ({exc}). "
            f"{detail}{self._fallback_manual_disable_hint()}"
        )

    def _auto_rest_renew_failure_message(self, robot_id: str, exc: Exception) -> str:
        robot_cfg = self.robot_cfgs.get(robot_id) or {}
        dm_url = str(robot_cfg.get("dm_url") or "").strip() or "-"
        return (
            f"Robot {robot_id}: auto-rest suspend lease ging verloren op {dm_url} ({exc}). "
            f"Run gestopt. {self._fallback_manual_disable_hint()}"
        )

    def _stop_auto_rest_lease_heartbeat(self) -> None:
        self._auto_rest_lease_stop.set()
        thread = self._auto_rest_lease_thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        self._auto_rest_lease_thread = None

    def _auto_rest_lease_retry_until_deadline(self, lease: _AutoRestLease) -> None:
        client = self.clients[lease.robot_id]
        timeout_s = self._auto_rest_timeout_hint_s()
        last_exc: Optional[Exception] = None
        while not self._auto_rest_lease_stop.is_set():
            if time.monotonic() >= lease.expires_at_monotonic:
                break
            try:
                self._client_auto_rest_suspend_renew(
                    client,
                    lease_id=lease.lease_id,
                    owner=lease.owner,
                    ttl_s=lease.ttl_s,
                    timeout_s=timeout_s,
                )
                with self._auto_rest_lease_lock:
                    current = self._auto_rest_leases.get(lease.robot_id)
                    if current and current.lease_id == lease.lease_id:
                        current.expires_at_monotonic = time.monotonic() + lease.ttl_s
                return
            except Exception as exc:
                last_exc = exc
                remaining = lease.expires_at_monotonic - time.monotonic()
                if remaining <= 0:
                    break
                self._auto_rest_lease_stop.wait(min(1.0, remaining))
        raise RuntimeError(self._auto_rest_renew_failure_message(lease.robot_id, last_exc or RuntimeError("lease expired")))

    def _auto_rest_lease_heartbeat_loop(self) -> None:
        while not self._auto_rest_lease_stop.wait(self.auto_rest_suspend_renew_interval_s):
            with self._auto_rest_lease_lock:
                leases = list(self._auto_rest_leases.values())
            if not leases:
                return
            for lease in leases:
                if self._auto_rest_lease_stop.is_set():
                    return
                try:
                    self._auto_rest_lease_retry_until_deadline(lease)
                except Exception as exc:
                    self._set_auto_rest_lease_failure(str(exc))
                    return

    def _start_auto_rest_lease_heartbeat(self) -> None:
        if self._auto_rest_lease_thread is not None and self._auto_rest_lease_thread.is_alive():
            return
        self._auto_rest_lease_stop.clear()
        thread = threading.Thread(target=self._auto_rest_lease_heartbeat_loop, daemon=True)
        self._auto_rest_lease_thread = thread
        thread.start()

    def _ensure_auto_rest_leases(self) -> None:
        with self._auto_rest_lease_lock:
            if self._auto_rest_leases:
                return
            self._auto_rest_lease_failure = None

        timeout_s = self._auto_rest_timeout_hint_s()
        current_robot_id = "unknown"
        try:
            for robot_id in sorted(self.clients.keys()):
                current_robot_id = robot_id
                client = self.clients[robot_id]
                lease_id = uuid.uuid4().hex
                self._log(f"Robot {robot_id}: acquiring auto-rest suspend lease")
                self._client_auto_rest_suspend_acquire(
                    client,
                    lease_id=lease_id,
                    owner=self.auto_rest_suspend_owner,
                    reason=self.auto_rest_suspend_reason,
                    ttl_s=self.auto_rest_suspend_ttl_s,
                    timeout_s=timeout_s,
                )
                lease = _AutoRestLease(
                    robot_id=robot_id,
                    lease_id=lease_id,
                    owner=self.auto_rest_suspend_owner,
                    reason=self.auto_rest_suspend_reason,
                    ttl_s=self.auto_rest_suspend_ttl_s,
                    dm_url=str((self.robot_cfgs.get(robot_id) or {}).get("dm_url") or "").strip(),
                    expires_at_monotonic=time.monotonic() + self.auto_rest_suspend_ttl_s,
                )
                with self._auto_rest_lease_lock:
                    self._auto_rest_leases[robot_id] = lease
            if self.clients:
                self._start_auto_rest_lease_heartbeat()
        except Exception as exc:
            self._release_auto_rest_leases(log_warnings=True)
            raise RuntimeError(self._auto_rest_acquire_failure_message(current_robot_id, exc)) from exc

    def _release_auto_rest_leases(self, *, log_warnings: bool) -> None:
        self._stop_auto_rest_lease_heartbeat()
        with self._auto_rest_lease_lock:
            leases = list(self._auto_rest_leases.values())
            self._auto_rest_leases.clear()
        timeout_s = self._auto_rest_timeout_hint_s()
        for lease in leases:
            try:
                self._client_auto_rest_suspend_release(
                    self.clients[lease.robot_id],
                    lease_id=lease.lease_id,
                    timeout_s=timeout_s,
                )
            except Exception as exc:
                if log_warnings:
                    self._log(
                        f"Robot {lease.robot_id}: auto-rest suspend release warning ({exc}). "
                        "Lease verloopt vanzelf als de DM hem niet meer kan vernieuwen."
                    )

    def close(self) -> None:
        if self._closed:
            return
        self._release_auto_rest_leases(log_warnings=True)
        if not self._log_handle.closed:
            self._log_handle.close()
        self._closed = True

    def _tts_preload_abs_path(self, rel_path: str) -> Path:
        if self.tts_preload_root is None:
            raise RuntimeError("tts preload root ontbreekt.")
        safe_rel = str(rel_path or "").replace("\\", "/").lstrip("/")
        abs_path = (self.tts_preload_root / safe_rel).resolve()
        if self.tts_preload_root.resolve() not in abs_path.parents and abs_path != self.tts_preload_root.resolve():
            raise RuntimeError("tts preload clip_rel_path is ongeldig.")
        return abs_path

    def _log(self, message: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] {message}"
        if self.log_to_stdout:
            print(line)
        if not self._log_handle.closed:
            self._log_handle.write(line + "\n")
            self._log_handle.flush()
        self._emit_event("log", message=line)

    def _emit_event(self, event_type: str, **payload: Any) -> None:
        sink = self.event_sink
        if sink is None:
            return
        event: Dict[str, Any] = {"type": event_type}
        event.update(payload)
        try:
            sink(event)
        except Exception:
            return

    def _abort_requested(self) -> bool:
        return bool(self.abort_event is not None and self.abort_event.is_set())

    @staticmethod
    def _fallback_manual_disable_hint() -> str:
        return "Handmatige fallback: zet auto-rest in de DM op uit (nao_auto_rest_after_s = 0)."

    def _get_auto_rest_lease_failure(self) -> Optional[str]:
        with self._auto_rest_lease_lock:
            value = self._auto_rest_lease_failure
        return str(value) if value else None

    def _set_auto_rest_lease_failure(self, message: str) -> None:
        normalized = str(message or "").strip()
        if not normalized:
            return
        should_log = False
        with self._auto_rest_lease_lock:
            if self._auto_rest_lease_failure:
                return
            self._auto_rest_lease_failure = normalized
            should_log = True
        if should_log:
            self._log(normalized)

    def _raise_if_abort_requested(self, *, context: str = "") -> None:
        lease_failure = self._get_auto_rest_lease_failure()
        if lease_failure:
            if context:
                raise RuntimeError(f"{context}: {lease_failure}")
            raise RuntimeError(lease_failure)
        if not self._abort_requested():
            return
        message = "Abort requested"
        if context:
            message = f"{context}: abort requested"
        raise _RunAbortRequested(message)

    def _sleep_interruptible(self, duration_s: float) -> None:
        remaining = max(0.0, float(duration_s))
        if self.abort_event is None:
            if remaining > 0:
                self.sleep_func(remaining)
            return
        while remaining > 0:
            self._raise_if_abort_requested()
            chunk = min(self.control_poll_interval_s, remaining)
            self.sleep_func(chunk)
            remaining -= chunk
        self._raise_if_abort_requested()

    def _set_waiting_for_next(self, *, reason: str, index: int, total: int, step_id: str) -> None:
        self._waiting_for_next = True
        self._waiting_reason = str(reason or "none")
        self._emit_event(
            "waiting",
            waiting_for_next=True,
            waiting_reason=self._waiting_reason,
            index=index,
            total=total,
            step_id=step_id,
        )

    def _clear_waiting_for_next(self, *, index: int, total: int, step_id: str) -> None:
        if not self._waiting_for_next:
            return
        self._waiting_for_next = False
        self._waiting_reason = "none"
        self._emit_event(
            "waiting_cleared",
            waiting_for_next=False,
            waiting_reason="none",
            index=index,
            total=total,
            step_id=step_id,
        )

    def _wait_for_continue_signal(self, *, reason: str, index: int, total: int, step_id: str) -> None:
        if self.continue_event is None:
            return
        self._set_waiting_for_next(reason=reason, index=index, total=total, step_id=step_id)
        try:
            while True:
                self._raise_if_abort_requested(context=f"[{index + 1}/{total}] {step_id}")
                if self.continue_event.is_set():
                    self.continue_event.clear()
                    return
                self._sleep_interruptible(self.control_poll_interval_s)
        finally:
            self._clear_waiting_for_next(index=index, total=total, step_id=step_id)

    def _runtime_health_payload(self, runtime_cfg: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "nao_ip": runtime_cfg.get("nao_ip"),
            "nao_ip_enabled": bool(runtime_cfg.get("nao_ip_enabled", False)),
            "nao_base_url": runtime_cfg.get("nao_base_url"),
            "behavior_manager_url": runtime_cfg.get("behavior_manager_url"),
            "base_enabled": bool(runtime_cfg.get("base_enabled", True)),
            "behavior_enabled": bool(runtime_cfg.get("behavior_enabled", True)),
        }

    def _describe_health_context(self, runtime_cfg: Dict[str, Any], *, endpoint: str) -> str:
        nao_ip_enabled = bool(runtime_cfg.get("nao_ip_enabled", False))
        if endpoint == "base":
            url = str(runtime_cfg.get("nao_base_url") or "").strip() or "-"
            return (
                f"nao_base_url={url}; base_enabled=true; "
                f"nao_ip_enabled={'true' if nao_ip_enabled else 'false'}"
            )
        if endpoint == "behavior":
            url = str(runtime_cfg.get("behavior_manager_url") or "").strip() or "-"
            return (
                f"behavior_manager_url={url}; behavior_enabled=true; "
                f"nao_ip_enabled={'true' if nao_ip_enabled else 'false'}"
            )
        nao_ip = str(runtime_cfg.get("nao_ip") or "").strip() or "-"
        return f"nao_ip={nao_ip}; nao_ip_enabled={'true' if nao_ip_enabled else 'false'}"

    def _readiness_dependencies(self, runtime_cfg: Dict[str, Any]) -> list[tuple[str, str]]:
        deps: list[tuple[str, str]] = []
        if bool(runtime_cfg.get("base_enabled", True)):
            deps.append(("base connector", self._describe_health_context(runtime_cfg, endpoint="base")))
        if bool(runtime_cfg.get("behavior_enabled", True)):
            deps.append(("behavior manager", self._describe_health_context(runtime_cfg, endpoint="behavior")))
        if bool(runtime_cfg.get("nao_ip_enabled", False)):
            deps.append(("nao tcp", self._describe_health_context(runtime_cfg, endpoint="nao")))
        return deps

    def _runtime_health_failure_message(self, runtime_cfg: Dict[str, Any], exc: Exception) -> str:
        raw = str(exc or "").strip() or exc.__class__.__name__
        deps = self._readiness_dependencies(runtime_cfg)
        nao_ip_enabled = bool(runtime_cfg.get("nao_ip_enabled", False))
        base_enabled = bool(runtime_cfg.get("base_enabled", True))
        behavior_enabled = bool(runtime_cfg.get("behavior_enabled", True))
        notes: list[str] = []
        if not nao_ip_enabled and base_enabled:
            notes.append("NAO TCP is disabled, but base connector is still enabled")
        if not nao_ip_enabled and behavior_enabled:
            notes.append("NAO TCP is disabled, but behavior manager is still enabled")
        lowered = raw.lower()
        if "timed out" in lowered:
            if len(deps) == 1:
                name, context = deps[0]
                message = f"runtime_health timed out while checking {name} ({context})"
            elif deps:
                joined = "; ".join(f"{name}: {context}" for name, context in deps)
                message = f"runtime_health timed out while checking readiness dependencies ({joined})"
            else:
                message = "runtime_health timed out while checking DM readiness"
        else:
            if deps:
                joined = "; ".join(f"{name}: {context}" for name, context in deps)
                message = f"runtime_health failed while checking readiness ({joined})"
            else:
                message = "runtime_health failed"
        if notes:
            message += ". " + ". ".join(notes) + "."
        return f"{message} Original error: {raw}"

    def _health_issues(self, runtime_cfg: Dict[str, Any], health: Dict[str, Any]) -> list[str]:
        issues: list[str] = []
        base_enabled = bool(runtime_cfg.get("base_enabled", True))
        behavior_enabled = bool(runtime_cfg.get("behavior_enabled", True))
        nao_ip_enabled = bool(runtime_cfg.get("nao_ip_enabled", False))

        base = health.get("base") if isinstance(health, dict) else {}
        behavior = health.get("behavior") if isinstance(health, dict) else {}
        nao = health.get("nao") if isinstance(health, dict) else {}

        base_ping = bool((base or {}).get("ping"))
        base_nao_ping = bool((base or {}).get("nao_ping"))
        behavior_ping = bool((behavior or {}).get("ping"))
        behavior_nao_ping = bool((behavior or {}).get("nao_ping"))
        nao_ping = bool((nao or {}).get("ping"))

        if nao_ip_enabled and not nao_ping:
            issues.append(f"nao tcp down ({self._describe_health_context(runtime_cfg, endpoint='nao')})")

        if base_enabled:
            if not base_ping:
                issues.append(f"base connector down ({self._describe_health_context(runtime_cfg, endpoint='base')})")
            elif nao_ip_enabled and not base_nao_ping:
                issues.append(f"base->NAO down ({self._describe_health_context(runtime_cfg, endpoint='base')})")

        if behavior_enabled:
            if not behavior_ping:
                issues.append(
                    f"behavior manager down ({self._describe_health_context(runtime_cfg, endpoint='behavior')})"
                )
            elif nao_ip_enabled and not behavior_nao_ping:
                issues.append(
                    f"behavior->NAO down ({self._describe_health_context(runtime_cfg, endpoint='behavior')})"
                )

        return issues

    def _check_robot_ready(self, robot_id: str) -> tuple[bool, str, Dict[str, Any]]:
        client = self.clients[robot_id]
        timeout_s = self.readiness_request_timeout_s

        try:
            caps = client.capabilities(timeout_s=timeout_s)
        except Exception as exc:
            return False, f"DM down ({exc})", {}
        supports = caps.get("supports", {}) if isinstance(caps, dict) else {}

        try:
            effective = client.runtime_effective(timeout_s=timeout_s)
            runtime_cfg = effective.get("runtime_config", {}) if isinstance(effective, dict) else {}
            if not isinstance(runtime_cfg, dict):
                runtime_cfg = {}
        except Exception as exc:
            return False, f"runtime_effective unavailable ({exc})", {"supports": supports}

        try:
            health = client.runtime_health(self._runtime_health_payload(runtime_cfg), timeout_s=timeout_s)
        except Exception as exc:
            return False, self._runtime_health_failure_message(runtime_cfg, exc), {"supports": supports, "runtime_config": runtime_cfg}

        issues = self._health_issues(runtime_cfg, health)
        if issues:
            return False, "; ".join(issues), {"supports": supports, "runtime_config": runtime_cfg}
        return True, "dm+connections OK", {"supports": supports, "runtime_config": runtime_cfg}

    def _wait_for_readiness(self) -> Dict[str, Dict[str, Any]]:
        total = len(self.clients)
        started = time.monotonic()
        last_lines: Dict[str, str] = {}

        self._log(
            "Readiness: waiting for {n} robot(s) (DM + verbindingen), polling every {poll:.1f}s".format(
                n=total,
                poll=self.readiness_poll_interval_s,
            )
        )

        while True:
            ready_info: Dict[str, Dict[str, Any]] = {}
            ready_count = 0

            for robot_id in sorted(self.clients.keys()):
                ready, status, info = self._check_robot_ready(robot_id)
                line = "Robot {rid}: {state} - {status}".format(
                    rid=robot_id,
                    state="READY" if ready else "WAIT",
                    status=status,
                )
                if last_lines.get(robot_id) != line:
                    self._log(line)
                    last_lines[robot_id] = line
                if ready:
                    ready_count += 1
                    ready_info[robot_id] = info

            if ready_count == total:
                self._log("Readiness complete: all robots ready.")
                return ready_info

            elapsed = time.monotonic() - started
            timeout_s = self.readiness_timeout_s
            if timeout_s > 0 and elapsed >= timeout_s:
                waiting = [rid for rid in sorted(self.clients.keys()) if rid not in ready_info]
                raise RuntimeError(
                    "Readiness timeout after {timeout:.1f}s; still waiting for: {robots}".format(
                        timeout=timeout_s,
                        robots=", ".join(waiting) or "unknown",
                    )
                )

            if timeout_s > 0:
                remaining = max(0.0, timeout_s - elapsed)
                self._log(
                    "Readiness: {ready}/{total} ready. Retry in {poll:.1f}s (timeout in {remaining:.0f}s).".format(
                        ready=ready_count,
                        total=total,
                        poll=self.readiness_poll_interval_s,
                        remaining=remaining,
                    )
                )
            else:
                self._log(
                    "Readiness: {ready}/{total} ready. Retry in {poll:.1f}s.".format(
                        ready=ready_count,
                        total=total,
                        poll=self.readiness_poll_interval_s,
                    )
                )
            self._sleep_interruptible(self.readiness_poll_interval_s)

    def _get_ppt_controller(self) -> PptControllerProtocol:
        if not self.ppt_enabled:
            raise RuntimeError("PPT is not enabled for this script.")
        if self._ppt_controller is None:
            self._ppt_controller = ComPptController()
        return self._ppt_controller

    def _get_ppt_position(self) -> Dict[str, int]:
        controller = self._get_ppt_controller()
        try:
            raw = controller.get_position()
        except PPTControllerError as exc:
            raise RuntimeError(str(exc)) from exc
        return self._normalize_position(raw)

    def _prepare_ppt_if_needed(self) -> None:
        if not self.ppt_enabled or self._ppt_prepared:
            return
        if sys.platform != "win32":
            raise RuntimeError("PPT feature requires Windows + PowerPoint COM")

        ppt_file = str(self.ppt_cfg.get("file") or "").strip()
        if not ppt_file:
            raise RuntimeError("ppt.enabled=true requires ppt.file")

        fullscreen_required = bool(self.ppt_cfg.get("fullscreen_required", True))
        self._log(f"[PPT] opening slideshow: {ppt_file}")

        controller = self._get_ppt_controller()
        try:
            controller.open_and_start_slideshow(ppt_file, fullscreen_required=fullscreen_required)
        except PPTControllerError as exc:
            raise RuntimeError(str(exc)) from exc
        try:
            controller.goto(1, 0)
        except PPTControllerError as exc:
            raise RuntimeError(f"PowerPoint reset to start failed: {exc}") from exc

        if fullscreen_required and not controller.is_fullscreen_slideshow():
            raise RuntimeError("PowerPoint slideshow is not fullscreen.")

        pos = self._get_ppt_position()
        self._last_script_ppt_position = dict(pos)
        self._log(f"[PPT] {self._format_position(pos)}")

        self._capture_on = bool(self.ppt_cfg.get("start_capture_on_run", True))
        self._log("[CAPTURE] ON" if self._capture_on else "[CAPTURE] OFF")
        self._ppt_prepared = True

    def _operator_next_slide(self) -> None:
        controller = self._get_ppt_controller()
        try:
            controller.next_slide()
        except PPTControllerError as exc:
            raise RuntimeError(str(exc)) from exc
        pos = self._get_ppt_position()
        self._log(f"[PPT] operator next slide -> {self._format_position(pos)}")

    def _operator_previous_slide(self) -> None:
        controller = self._get_ppt_controller()
        try:
            controller.previous_slide()
        except PPTControllerError as exc:
            raise RuntimeError(str(exc)) from exc
        pos = self._get_ppt_position()
        self._log(f"[PPT] operator previous slide -> {self._format_position(pos)}")

    def _snapback_to_script_anchor(self) -> None:
        if not self.ppt_enabled:
            return
        if not self._last_script_ppt_position:
            self._log("[PPT] snapback skipped: no script anchor yet")
            return

        target = dict(self._last_script_ppt_position)
        controller = self._get_ppt_controller()
        self._log(f"[PPT] snapback to {self._format_position(target)}")
        try:
            controller.goto(target["slide"], target.get("build"))
        except PPTControllerError as exc:
            raise RuntimeError(str(exc)) from exc

        pos = self._get_ppt_position()
        self._last_script_ppt_position = dict(pos)
        self._log(f"[PPT] {self._format_position(pos)}")

    def _read_control_input(self, prompt: str = "") -> str:
        self._raise_if_abort_requested()
        try:
            return str(self.input_func(prompt) or "").strip().lower()
        except EOFError as exc:
            raise RuntimeError("manual step requires interactive stdin (capture/manual controls)") from exc

    def _ask_quit_action(self) -> str:
        while True:
            choice = self._read_control_input("Quit run? [a]bort / [c]ontinue: ")
            if choice in {"a", "abort"}:
                return "abort"
            if choice in {"c", "continue", "", "n", "no"}:
                return "continue"

    def _toggle_capture(self) -> None:
        if not self.ppt_enabled:
            self._log("[CAPTURE] toggle ignored: PPT not enabled")
            return

        if self._capture_on:
            self._capture_on = False
            self._log("[CAPTURE] OFF")
            return

        self._capture_on = True
        self._log("[CAPTURE] ON")
        self._snapback_to_script_anchor()

    def _pause_while_capture_off(self) -> None:
        if not self.ppt_enabled:
            return
        if self.continue_event is not None and not self._capture_on:
            self._log("[CAPTURE] OFF in web mode -> auto ON")
            self._capture_on = True
            self._snapback_to_script_anchor()
            return

        while not self._capture_on:
            self._raise_if_abort_requested()
            self._log("[CAPTURE] paused (ENTER=next slide, p=previous slide, c=capture ON, q=quit)")
            choice = self._read_control_input("")
            if choice == "":
                self._operator_next_slide()
                continue
            if choice in {"p", "prev", "previous"}:
                self._operator_previous_slide()
                continue
            if choice in {"c", "capture"}:
                self._capture_on = True
                self._log("[CAPTURE] ON")
                self._snapback_to_script_anchor()
                continue
            if choice in {"q", "quit"}:
                if self._ask_quit_action() == "abort":
                    raise _RunAbortRequested("Abort requested while capture was paused.")
                continue
            self._log("[CAPTURE] unknown key. Use ENTER, p, c, or q.")

    def _check_capture_sync(self) -> None:
        if not self.ppt_enabled or not self._capture_on or not self._last_script_ppt_position:
            return
        if self._pending_snapback:
            return

        current = self._get_ppt_position()
        expected = dict(self._last_script_ppt_position)
        if current == expected:
            return

        if self.ppt_mismatch_policy == "abort":
            raise _RunAbortRequested(
                "[SYNC] mismatch detected -> abort (expected {exp}, got {got})".format(
                    exp=self._format_position(expected),
                    got=self._format_position(current),
                )
            )
        if self.ppt_mismatch_policy == "defer_snapback":
            self._pending_snapback = True
            self._log(
                "[SYNC] mismatch detected -> defer snapback at next script step "
                "(expected {exp}, got {got})".format(
                    exp=self._format_position(expected),
                    got=self._format_position(current),
                )
            )
            return

        self._log(
            "[SYNC] mismatch detected -> hard pause (expected {exp}, got {got})".format(
                exp=self._format_position(expected),
                got=self._format_position(current),
            )
        )
        self._capture_on = False
        self._log("[CAPTURE] OFF")
        self._pause_while_capture_off()

    def _apply_pending_snapback_if_needed(self) -> None:
        if not self._pending_snapback:
            return
        self._log("[SYNC] applying deferred snapback before next step")
        self._snapback_to_script_anchor()
        self._pending_snapback = False

    def preflight(self) -> None:
        self._emit_event("status", status="preflight")
        self._log("Preflight: checking robot DM capabilities...")
        try:
            ready_info = self._wait_for_readiness()
            if self.clients:
                self._log("Preflight: acquiring auto-rest suspend leases...")
            self._ensure_auto_rest_leases()
            for robot_id in sorted(self.clients.keys()):
                info = ready_info.get(robot_id) or {}
                supports = info.get("supports", {}) if isinstance(info, dict) else {}
                runtime = info.get("runtime_config", {}) if isinstance(info, dict) else {}
                self._log(f"Robot {robot_id}: capabilities OK ({supports})")
                self._log(
                    "Robot {rid}: runtime nao_ip={ip} base={base} out={out}/{tts}".format(
                        rid=robot_id,
                        ip=runtime.get("nao_ip"),
                        base=runtime.get("nao_base_url"),
                        out=runtime.get("output_target"),
                        tts=runtime.get("tts_engine"),
                    )
                )
                preload_mode = str(self.tts_preload_robot_modes.get(robot_id) or "live")
                if preload_mode != "live":
                    self._log(f"Robot {robot_id}: tts preload mode {preload_mode}")

            self._prepare_ppt_if_needed()
            self._log("Preflight complete.")
        except Exception:
            self._release_auto_rest_leases(log_warnings=True)
            raise

    def _wait_for_step_start(self, step: Dict[str, Any], index: int, total: int) -> None:
        start = step.get("start") or {}
        mode = str(start.get("mode") or "").strip().lower()
        step_id = step.get("id", f"step_{index+1}")

        if mode == "manual":
            self._log(f"[{index + 1}/{total}] {step_id}: waiting for continue (manual start)")
            if self.continue_event is not None:
                self._wait_for_continue_signal(
                    reason="manual_start",
                    index=index,
                    total=total,
                    step_id=step_id,
                )
                return
            while True:
                self._raise_if_abort_requested(context=f"[{index + 1}/{total}] {step_id}")
                choice = self._read_control_input("")
                if choice == "":
                    return
                if choice in {"c", "capture"}:
                    self._toggle_capture()
                    self._pause_while_capture_off()
                    continue
                if choice in {"q", "quit"}:
                    if self._ask_quit_action() == "abort":
                        raise _RunAbortRequested(f"[{index + 1}/{total}] {step_id}: abort requested")
                    continue
                self._log(f"[{index + 1}/{total}] {step_id}: unknown key '{choice}' (use ENTER/c/q)")

        if mode == "with_prev":
            return

        if mode == "after_prev":
            delay_s = float(start.get("delay_s", 0))
            if delay_s > 0:
                self._log(f"[{index + 1}/{total}] {step_id}: waiting {delay_s:.2f}s before execution")
                self._sleep_interruptible(delay_s)
            return

        raise RuntimeError(f"unsupported start mode: {mode}")

    def _execute_step_action(self, step: Dict[str, Any], *, index: Optional[int] = None, total: Optional[int] = None) -> Dict[str, Any]:
        self._raise_if_abort_requested()
        action = step.get("action") or {}
        action_type = str(action.get("type") or "").strip().lower()
        timeout_s = float(step.get("request_timeout_s", self.defaults.get("request_timeout_s", 12)))
        step_id = str(step.get("id", "step"))
        step_index = 0 if index is None else int(index)
        step_total = 1 if total is None else int(total)

        if action_type == "pause":
            seconds = float(action.get("seconds", 0))
            self._log(f"Pause: sleeping {seconds:.2f}s")
            if seconds > 0:
                self._sleep_interruptible(seconds)
            return {"ok": True, "status": "accepted", "action": "pause"}

        if action_type == "ppt":
            if not self.ppt_enabled:
                raise RuntimeError("ppt action requires top-level ppt.enabled=true")

            controller = self._get_ppt_controller()
            ppt_mode = str(action.get("mode") or "").strip().lower()
            try:
                if ppt_mode == "next_slide":
                    controller.next_slide()
                elif ppt_mode == "previous_slide":
                    controller.previous_slide()
                elif ppt_mode == "goto":
                    slide = int(action.get("slide"))
                    click = action.get("click", action.get("build"))
                    controller.goto(slide, int(click) if click is not None else None)
                else:
                    raise RuntimeError(f"unsupported ppt mode: {ppt_mode}")
            except PPTControllerError as exc:
                raise RuntimeError(str(exc)) from exc

            pos = self._get_ppt_position()
            self._last_script_ppt_position = dict(pos)
            self._log(f"[PPT] {self._format_position(pos)}")
            return {
                "ok": True,
                "status": "accepted",
                "action": "ppt",
                "mode": ppt_mode,
                "position": dict(pos),
            }

        robot_id = str(step.get("robot_id") or "").strip()
        if robot_id not in self.clients:
            raise RuntimeError(f"unknown robot_id: {robot_id}")
        client = self.clients[robot_id]

        if action_type == "say":
            text = str(action.get("text") or "").strip()
            preload_meta = self.tts_preload_step_audio.get(step_id)
            if isinstance(preload_meta, dict):
                rel_path = str(preload_meta.get("clip_rel_path") or "").strip()
                if rel_path:
                    try:
                        clip_bytes = self._tts_preload_abs_path(rel_path).read_bytes()
                    except Exception as exc:
                        self._log(f"[TTS preload] {step_id}: clip ontbreekt/onleesbaar -> live synthese ({exc})")
                    else:
                        try:
                            return client.script_say(
                                text=text,
                                timeout_s=self._preloaded_say_timeout_s(clip_bytes, timeout_s),
                                preloaded_audio_b64=base64.b64encode(clip_bytes).decode("ascii"),
                                preloaded_audio_format="wav",
                            )
                        except Exception as exc:
                            self._log(f"[TTS preload] {step_id}: preload playback niet bevestigd; geen live fallback ({exc})")
                            return {
                                "ok": True,
                                "status": "preloaded_playback_unconfirmed",
                                "action": "say",
                                "preloaded_audio": True,
                                "warning": str(exc),
                            }
            return client.script_say(text=text, timeout_s=timeout_s)

        if action_type != "do":
            raise RuntimeError(f"unsupported action type: {action_type}")

        do_mode = str(action.get("mode") or "").strip().lower()
        payload: Dict[str, Any] = {"mode": do_mode}
        if do_mode == "command":
            payload["label"] = action.get("label")
            if isinstance(action.get("resolved"), dict):
                payload["resolved"] = action.get("resolved")
        elif do_mode == "dance":
            payload["dance_key"] = action.get("dance_key")
        elif do_mode == "nao_set_eye_color":
            color = str(action.get("color") or "").strip()
            if not color:
                raise RuntimeError("nao_set_eye_color requires action.color")
            duration = action.get("duration")
            if duration is None:
                return client.nao_set_eye_color(color=color, timeout_s=timeout_s)
            return client.nao_set_eye_color(color=color, duration=float(duration), timeout_s=timeout_s)
        elif do_mode in {"behavior_start", "behavior_stop"}:
            payload["behavior"] = action.get("behavior")
        elif do_mode == "summary_start":
            return self._start_summary_step(
                action=action,
                robot_id=robot_id,
                client=client,
                timeout_s=timeout_s,
                step_index=step_index,
                step_total=step_total,
                step_id=step_id,
            )
        else:
            raise RuntimeError(f"unsupported do.mode: {do_mode}")
        return client.script_do(payload=payload, timeout_s=timeout_s)

    @staticmethod
    def _summary_is_terminal(status: str) -> bool:
        return str(status or "").strip().lower() in {"completed", "aborted", "error"}

    @staticmethod
    def _summary_session(payload: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        session = payload.get("session") if isinstance(payload, dict) else None
        return session if isinstance(session, dict) else {}

    def _summary_status(self, payload: Optional[Dict[str, Any]], *, default: str = "") -> str:
        session = self._summary_session(payload)
        status = str(session.get("status") or "").strip().lower()
        if status:
            return status
        return str(default or "").strip().lower()

    def _summary_last_error(self, payload: Optional[Dict[str, Any]], *, fallback: str = "") -> str:
        session = self._summary_session(payload)
        for raw in (
            session.get("last_error"),
            payload.get("detail") if isinstance(payload, dict) else None,
            payload.get("error") if isinstance(payload, dict) else None,
            fallback,
        ):
            text = str(raw or "").strip()
            if text:
                return text
        return ""

    def _emit_summary_state(
        self,
        *,
        robot_id: str,
        client: DMClient,
        summary_url: str,
        payload: Optional[Dict[str, Any]],
        waiting: bool,
        connection_ok: bool,
        last_error: str = "",
        open_nonce: int = 0,
    ) -> None:
        session = self._summary_session(payload)
        status = self._summary_status(payload, default="unknown")
        active = not self._summary_is_terminal(status)
        event: Dict[str, Any] = {
            "robot_id": str(robot_id or "").strip(),
            "summary_dm_url": str(client.base_url or "").strip(),
            "summary_url": str(summary_url or "").strip(),
            "summary_active": bool(active),
            "summary_waiting": bool(waiting),
            "summary_session_id": str(session.get("session_id") or "").strip(),
            "summary_status": status,
            "summary_connection_ok": bool(connection_ok),
            "summary_last_error": self._summary_last_error(payload, fallback=last_error),
        }
        if open_nonce > 0:
            event["summary_open_nonce"] = int(open_nonce)
        self._emit_event("summary_state", **event)

    def _wait_for_summary_completion(
        self,
        *,
        robot_id: str,
        client: DMClient,
        timeout_s: float,
        step_index: int,
        step_total: int,
        step_id: str,
        summary_url: str,
        initial_payload: Dict[str, Any],
        open_nonce: int,
    ) -> Dict[str, Any]:
        self._log(f"[{step_index + 1}/{step_total}] {step_id}: waiting for summary completion")
        last_payload: Dict[str, Any] = dict(initial_payload or {})
        last_warn = ""
        self._set_waiting_for_next(reason="summary_wait", index=step_index, total=step_total, step_id=step_id)
        try:
            while True:
                self._raise_if_abort_requested(context=f"[{step_index + 1}/{step_total}] {step_id}")
                try:
                    payload = client.summary_get(timeout_s=timeout_s)
                except _RunAbortRequested:
                    raise
                except Exception as exc:
                    message = str(exc or "").strip() or "summary poll failed"
                    if message != last_warn:
                        self._log(f"[{step_index + 1}/{step_total}] {step_id}: WARN summary poll failed ({message})")
                        last_warn = message
                    self._emit_summary_state(
                        robot_id=robot_id,
                        client=client,
                        summary_url=summary_url,
                        payload=last_payload,
                        waiting=True,
                        connection_ok=False,
                        last_error=message,
                        open_nonce=open_nonce,
                    )
                    self._sleep_interruptible(self.summary_live_poll_interval_s)
                    continue

                last_payload = dict(payload or {})
                status = self._summary_status(payload)
                self._emit_summary_state(
                    robot_id=robot_id,
                    client=client,
                    summary_url=summary_url,
                    payload=payload,
                    waiting=True,
                    connection_ok=True,
                    open_nonce=open_nonce,
                )
                if status == "completed":
                    self._emit_summary_state(
                        robot_id=robot_id,
                        client=client,
                        summary_url=summary_url,
                        payload=payload,
                        waiting=False,
                        connection_ok=True,
                        open_nonce=open_nonce,
                    )
                    return payload
                if status == "aborted":
                    self._emit_summary_state(
                        robot_id=robot_id,
                        client=client,
                        summary_url=summary_url,
                        payload=payload,
                        waiting=False,
                        connection_ok=True,
                        open_nonce=open_nonce,
                    )
                    raise RuntimeError("Summary aborted.")
                if status == "error":
                    detail = self._summary_last_error(payload, fallback="Summary error.")
                    self._emit_summary_state(
                        robot_id=robot_id,
                        client=client,
                        summary_url=summary_url,
                        payload=payload,
                        waiting=False,
                        connection_ok=True,
                        open_nonce=open_nonce,
                    )
                    raise RuntimeError(f"Summary error: {detail}")
                last_warn = ""
                self._sleep_interruptible(self.summary_live_poll_interval_s)
        finally:
            self._clear_waiting_for_next(index=step_index, total=step_total, step_id=step_id)

    def _start_summary_step(
        self,
        *,
        action: Dict[str, Any],
        robot_id: str,
        client: DMClient,
        timeout_s: float,
        step_index: int,
        step_total: int,
        step_id: str,
    ) -> Dict[str, Any]:
        wait_for_complete = bool(action.get("wait_for_complete", True))
        open_on_new_tab = bool(action.get("open_on_new_tab", False))
        summary_url = client.summary_page_url()
        open_nonce = 0
        if open_on_new_tab:
            self._summary_open_nonce += 1
            open_nonce = self._summary_open_nonce
        result = client.summary_start(timeout_s=timeout_s)
        self._emit_summary_state(
            robot_id=robot_id,
            client=client,
            summary_url=summary_url,
            payload=result,
            waiting=wait_for_complete,
            connection_ok=True,
            open_nonce=open_nonce,
        )
        if not wait_for_complete:
            return result
        return self._wait_for_summary_completion(
            robot_id=robot_id,
            client=client,
            timeout_s=timeout_s,
            step_index=step_index,
            step_total=step_total,
            step_id=step_id,
            summary_url=summary_url,
            initial_payload=result,
            open_nonce=open_nonce,
        )

    def _ask_error_action(self) -> str:
        while True:
            self._raise_if_abort_requested()
            try:
                choice = str(self.input_func("Step failed. Choose: [r]etry / [n]ext / [a]bort: ") or "").strip().lower()
            except EOFError as exc:
                raise RuntimeError("Cannot prompt for retry/next/abort without interactive stdin") from exc
            if choice in {"r", "retry"}:
                return "retry"
            if choice in {"n", "next"}:
                return "next"
            if choice in {"a", "abort"}:
                return "abort"

    def _resolve_prompt_error_action(self) -> str:
        if self.on_error_prompt_policy == "next":
            return "next"
        if self.on_error_prompt_policy == "abort":
            return "abort"
        return self._ask_error_action()

    def _on_error_action(self, step: Dict[str, Any]) -> str:
        policy = str(step.get("on_error") or self.defaults.get("on_error", "prompt")).strip().lower()
        if policy == "abort":
            return "abort"
        if policy == "continue":
            return "next"
        return self._resolve_prompt_error_action()

    @staticmethod
    def _is_timeout_error(exc: Exception) -> bool:
        if isinstance(exc, TimeoutError):
            return True
        msg = str(exc or "").strip().lower()
        if not msg:
            return False
        if "auto-rest suspend" in msg:
            return False
        timeout_markers = (
            "timed out",
            "timeout",
            "read timeout",
            "connection timed out",
        )
        return any(marker in msg for marker in timeout_markers)

    @staticmethod
    def _step_start_mode(step: Dict[str, Any]) -> str:
        start = step.get("start") or {}
        return str(start.get("mode") or "").strip().lower()

    @staticmethod
    def _step_action_type(step: Dict[str, Any]) -> str:
        action = step.get("action") or {}
        return str(action.get("type") or "").strip().lower()

    def _execute_step_with_policy(self, step: Dict[str, Any], index: int, total: int) -> str:
        step_id = step.get("id", f"step_{index+1}")
        while True:
            try:
                self._raise_if_abort_requested(context=f"[{index + 1}/{total}] {step_id}")
                self._pause_while_capture_off()
                self._check_capture_sync()
                self._emit_event("step_start", index=index, total=total, step_id=step_id)
                self._log(f"[{index + 1}/{total}] {step_id}: executing")
                response = self._execute_step_action(step, index=index, total=total)
                self._raise_if_abort_requested(context=f"[{index + 1}/{total}] {step_id}")
                self._log(f"[{index + 1}/{total}] {step_id}: OK -> {response}")
                return "completed"
            except _RunAbortRequested:
                raise
            except Exception as exc:
                self._emit_event("step_error", index=index, total=total, step_id=step_id, error=str(exc))
                self._log(f"[{index + 1}/{total}] {step_id}: ERROR -> {exc}")
                action: str
                if self._is_timeout_error(exc):
                    timeout_policy = str(step.get("on_error") or self.defaults.get("on_error", "prompt")).strip().lower()
                    if timeout_policy == "prompt":
                        if self.on_error_prompt_policy == "prompt":
                            action = "next"
                            self._log(f"[{index + 1}/{total}] {step_id}: timeout detected -> default action next")
                        else:
                            action = self._resolve_prompt_error_action()
                    else:
                        action = self._on_error_action(step)
                else:
                    action = self._on_error_action(step)
                if action == "retry":
                    self._log(f"[{index + 1}/{total}] {step_id}: retry")
                    continue
                if action == "next":
                    self._log(f"[{index + 1}/{total}] {step_id}: skip to next")
                    return "skipped"
                self._log(f"[{index + 1}/{total}] {step_id}: abort requested")
                raise _RunAbortRequested(f"[{index + 1}/{total}] {step_id}: abort requested")

    def _execute_parallel_group_once(
        self,
        group: list[tuple[int, Dict[str, Any]]],
        total: int,
    ) -> tuple[int, list[tuple[int, Dict[str, Any], Exception]]]:
        completed = 0
        failures: list[tuple[int, Dict[str, Any], Exception]] = []
        future_map: Dict[Any, tuple[int, Dict[str, Any], str]] = {}
        worker_count = sum(1 for _, step in group if self._step_action_type(step) != "ppt")
        executor = ThreadPoolExecutor(max_workers=worker_count) if worker_count > 0 else None

        try:
            for step_index, step in group:
                step_id = step.get("id", f"step_{step_index+1}")
                self._raise_if_abort_requested(context=f"[{step_index + 1}/{total}] {step_id}")
                self._emit_event("step_start", index=step_index, total=total, step_id=step_id)
                self._log(f"[{step_index + 1}/{total}] {step_id}: executing (with_prev)")

                # PowerPoint COM objects are bound to the thread that opened the slideshow.
                if self._step_action_type(step) == "ppt":
                    try:
                        response = self._execute_step_action(step, index=step_index, total=total)
                    except Exception as exc:
                        self._emit_event("step_error", index=step_index, total=total, step_id=step_id, error=str(exc))
                        failures.append((step_index, step, exc))
                    else:
                        self._raise_if_abort_requested(context=f"[{step_index + 1}/{total}] {step_id}")
                        self._log(f"[{step_index + 1}/{total}] {step_id}: OK -> {response}")
                        self._emit_event("step_done", index=step_index, total=total, step_id=step_id, status="completed")
                        completed += 1
                    continue

                if executor is None:
                    raise RuntimeError("parallel group executor missing for non-PPT step")
                future = executor.submit(self._execute_step_action, step, index=step_index, total=total)
                future_map[future] = (step_index, step, step_id)

            for future in as_completed(future_map):
                step_index, step, step_id = future_map[future]
                try:
                    response = future.result()
                except Exception as exc:
                    self._emit_event("step_error", index=step_index, total=total, step_id=step_id, error=str(exc))
                    failures.append((step_index, step, exc))
                    continue
                self._raise_if_abort_requested(context=f"[{step_index + 1}/{total}] {step_id}")
                self._log(f"[{step_index + 1}/{total}] {step_id}: OK -> {response}")
                self._emit_event("step_done", index=step_index, total=total, step_id=step_id, status="completed")
                completed += 1
        finally:
            if executor is not None:
                executor.shutdown(wait=True)

        failures.sort(key=lambda item: item[0])
        return completed, failures

    def _resolve_failed_step(
        self,
        step: Dict[str, Any],
        index: int,
        total: int,
        exc: Exception,
    ) -> str:
        step_id = step.get("id", f"step_{index+1}")
        self._log(f"[{index + 1}/{total}] {step_id}: ERROR -> {exc}")
        self._emit_event("step_error", index=index, total=total, step_id=step_id, error=str(exc))
        while True:
            self._raise_if_abort_requested(context=f"[{index + 1}/{total}] {step_id}")
            action = self._on_error_action(step)
            if action == "retry":
                self._log(f"[{index + 1}/{total}] {step_id}: retry")
                return self._execute_step_with_policy(step, index, total)
            if action == "next":
                self._log(f"[{index + 1}/{total}] {step_id}: skip to next")
                return "skipped"
            self._log(f"[{index + 1}/{total}] {step_id}: abort requested")
            raise _RunAbortRequested(f"[{index + 1}/{total}] {step_id}: abort requested")

    def run(self) -> RunResult:
        steps = list(self.script.get("steps") or [])
        completed = 0
        total = len(steps)
        aborted = False
        self._ensure_auto_rest_leases()
        self._emit_event("status", status="running", completed_steps=0, total_steps=total)
        try:
            self._prepare_ppt_if_needed()
            index = 0
            while index < total:
                self._raise_if_abort_requested()
                step = steps[index]
                self._pause_while_capture_off()
                self._check_capture_sync()
                self._wait_for_step_start(step, index, total)
                self._pause_while_capture_off()
                self._check_capture_sync()
                self._apply_pending_snapback_if_needed()

                group: list[tuple[int, Dict[str, Any]]] = [(index, step)]
                next_index = index + 1
                while next_index < total and self._step_start_mode(steps[next_index]) == "with_prev":
                    group.append((next_index, steps[next_index]))
                    next_index += 1

                if len(group) == 1:
                    status = self._execute_step_with_policy(step, index, total)
                    if status == "completed":
                        completed += 1
                    self._emit_event(
                        "step_done",
                        index=index,
                        total=total,
                        step_id=step.get("id", f"step_{index+1}"),
                        status=status,
                        completed_steps=completed,
                    )
                    index = next_index
                    continue

                group_completed, failures = self._execute_parallel_group_once(group, total)
                completed += group_completed
                for failed_index, failed_step, failed_exc in failures:
                    status = self._resolve_failed_step(
                        failed_step,
                        failed_index,
                        total,
                        failed_exc,
                    )
                    if status == "completed":
                        completed += 1
                    self._emit_event(
                        "step_done",
                        index=failed_index,
                        total=total,
                        step_id=failed_step.get("id", f"step_{failed_index+1}"),
                        status=status,
                        completed_steps=completed,
                    )

                index = next_index
        except _RunAbortRequested as exc:
            aborted = True
            self._log(str(exc))
            self._emit_event("status", status="aborted", completed_steps=completed, total_steps=total, error=str(exc))
        finally:
            self._release_auto_rest_leases(log_warnings=True)
            if not self._log_handle.closed:
                self._log_handle.close()
            self._closed = True
        if not aborted:
            self._emit_event("status", status="completed", completed_steps=completed, total_steps=total)

        return RunResult(
            completed_steps=completed,
            total_steps=total,
            aborted=aborted,
            log_path=self.log_path,
        )
