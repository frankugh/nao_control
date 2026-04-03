from __future__ import annotations

import base64
import copy
import json
import os
import queue
import re
import threading
import time
import uuid
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional, Tuple


JsonDict = Dict[str, Any]
_SUMMARY_PHASE_TARGETS = ("capture", "transcript_review", "summary_review")


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _deepcopy_json(value: Any) -> Any:
    return copy.deepcopy(value)


def _transcript_text_from_lines(lines: List[JsonDict]) -> str:
    parts: List[str] = []
    for entry in lines:
        if not isinstance(entry, dict):
            continue
        text = str(entry.get("text") or "").strip()
        if text:
            parts.append(text)
    return "\n".join(parts).strip()


def _lines_from_transcript_text(text: str, *, source: str) -> List[JsonDict]:
    lines: List[JsonDict] = []
    for raw_line in str(text or "").splitlines():
        cleaned = raw_line.strip()
        if not cleaned:
            continue
        lines.append(
            {
                "id": str(uuid.uuid4()),
                "text": cleaned,
                "source": source,
                "created_at": _utc_now(),
            }
        )
    return lines


def _is_terminal_status(status: str) -> bool:
    return status in {"completed", "aborted", "error"}


def _fallback_status_for_inflight(status: str) -> str:
    if status in {"capturing", "summarizing"}:
        return "transcript_review"
    if status == "publishing":
        return "summary_review"
    return status


def _has_meaningful_text(value: Any) -> bool:
    return bool(str(value or "").strip())


def _normalize_execution_mode(raw_value: Any) -> str:
    mode = str(raw_value or "cloud").strip().lower()
    return "local" if mode == "local" else "cloud"


def _detail_contains(detail: str, needles: List[str]) -> bool:
    haystack = str(detail or "").strip().lower()
    return any(needle in haystack for needle in needles)


class SummaryService:
    def __init__(
        self,
        *,
        instance_id: str,
        state_root: Optional[str],
        default_config: JsonDict,
        normalize_config: Callable[[Any], JsonDict],
        build_app_config: Callable[[JsonDict], Tuple[JsonDict, JsonDict]],
        resolve_prompt: Callable[[JsonDict], Tuple[Optional[str], JsonDict, Optional[str]]],
        make_mic: Callable[[JsonDict], Any],
        build_stt_backend: Callable[[JsonDict], Any],
        build_pipeline: Callable[[JsonDict], Any],
        transcribe_audio: Callable[[Any, Any, JsonDict], Any],
        generate_reply: Callable[[Any, JsonDict, List[Dict[str, str]]], str],
        publish_text: Callable[[Any, JsonDict, str], None],
        stop_output: Callable[[JsonDict], JsonDict],
        stop_local_llm_model: Optional[Callable[[str, str], JsonDict]],
        connectivity_snapshot: Callable[[], List[JsonDict]],
        log_event: Callable[[str, str, str, Optional[JsonDict]], None],
        capture_timeout_default_s: float,
        capture_timeout_max_s: float,
        stt_queue_max_items: int,
        stt_disk_spool_max_items: int = 64,
        worker_join_timeout_s: float = 2.0,
        auto_retry_delay_s: float = 0.15,
        stt_recovery_probe_delay_s: float = 1.0,
        generate_user_template: str = "Transcript:\n{transcript}",
    ) -> None:
        self.instance_id = str(instance_id or "default").strip() or "default"
        self.state_root = str(state_root) if state_root else None
        self._normalize_config = normalize_config
        self._build_app_config = build_app_config
        self._resolve_prompt = resolve_prompt
        self._make_mic = make_mic
        self._build_stt_backend = build_stt_backend
        self._build_pipeline = build_pipeline
        self._transcribe_audio = transcribe_audio
        self._generate_reply = generate_reply
        self._publish_text = publish_text
        self._stop_output = stop_output
        self._stop_local_llm_model = stop_local_llm_model
        self._connectivity_snapshot = connectivity_snapshot
        self._log_event = log_event
        self._capture_timeout_default_s = float(capture_timeout_default_s)
        self._capture_timeout_max_s = float(capture_timeout_max_s)
        self._stt_queue_max_items = int(stt_queue_max_items)
        self._stt_disk_spool_max_items = max(1, int(stt_disk_spool_max_items))
        self._worker_join_timeout_s = float(worker_join_timeout_s)
        self._auto_retry_delay_s = max(0.0, float(auto_retry_delay_s))
        self._stt_recovery_probe_delay_s = max(0.25, float(stt_recovery_probe_delay_s))
        self._generate_user_template = str(generate_user_template or "Transcript:\n{transcript}")
        self._lock = threading.RLock()
        self._config = self._normalize_config(_deepcopy_json(default_config))
        self._session: Optional[JsonDict] = None
        self._capture_thread: Optional[threading.Thread] = None
        self._stt_thread: Optional[threading.Thread] = None
        self._stop_event: Optional[threading.Event] = None
        self._stt_resume_event: Optional[threading.Event] = None
        self._audio_queue: Optional["queue.Queue[Any]"] = None
        self._publish_interrupt_requested = False
        self._active_llm_job: Optional[JsonDict] = None
        self._llm_job_thread: Optional[threading.Thread] = None
        self._load_persisted_state()

    def shutdown(self) -> None:
        self._stop_capture_runtime()

    def get_payload(self) -> JsonDict:
        with self._lock:
            return self._payload()

    def get_config_payload(self) -> JsonDict:
        with self._lock:
            return {"ok": True, "instance_id": self.instance_id, "config": _deepcopy_json(self._config)}

    def update_config(self, raw_config: Any) -> Tuple[JsonDict, int]:
        with self._lock:
            try:
                self._config = self._normalize_config(raw_config)
            except Exception as exc:
                return self._payload(error="invalid_summary_config", detail=str(exc)), 400
            self._persist_config()
            self._log_event(
                "info",
                "summary_config_updated",
                "Summary config updated.",
                {"keys": sorted(list(self._config.keys()))},
            )
            return {
                "ok": True,
                "instance_id": self.instance_id,
                "config": _deepcopy_json(self._config),
                "applies_to_next_session": True,
            }, 200

    def _make_toast(self, key: str, *, level: str = "warning") -> JsonDict:
        return {
            "id": str(uuid.uuid4()),
            "key": str(key or "").strip(),
            "level": str(level or "warning").strip() or "warning",
            "created_at": _utc_now(),
        }

    def _set_toast_locked(self, session: Optional[JsonDict], key: str, *, level: str = "warning") -> None:
        if session is None:
            return
        session["toast"] = self._make_toast(key, level=level)

    def _clear_toast_locked(self, session: Optional[JsonDict]) -> None:
        if session is None:
            return
        session["toast"] = None

    def _clear_recovery_locked(self, session: Optional[JsonDict], *, stage: Optional[str] = None) -> None:
        if session is None:
            return
        recovery = session.get("recovery")
        if not isinstance(recovery, dict):
            session["recovery"] = None
            return
        if stage and str(recovery.get("stage") or "").strip().lower() != str(stage or "").strip().lower():
            return
        session["recovery"] = None

    def _set_recovery_locked(
        self,
        session: Optional[JsonDict],
        *,
        stage: str,
        kind: str,
        message: str,
        blocking: bool,
        auto_retry_used: bool = False,
        next_retry_after: Optional[float] = None,
    ) -> None:
        if session is None:
            return
        recovery: JsonDict = {
            "stage": str(stage or "").strip().lower(),
            "kind": str(kind or "").strip().lower() or "service",
            "message": str(message or "").strip(),
            "auto_retry_used": bool(auto_retry_used),
            "blocking": bool(blocking),
            "updated_at": _utc_now(),
        }
        if next_retry_after is not None:
            try:
                recovery["next_retry_after"] = float(next_retry_after)
            except Exception:
                pass
        session["recovery"] = recovery

    def _classify_failure(self, stage: str, detail: str) -> str:
        stage_key = str(stage or "").strip().lower()
        text = str(detail or "").strip()
        if not text:
            return "service"
        lowered = text.lower()
        resource_terms = [
            "output disabled",
            "device",
            "speaker",
            "micro",
            "mic",
            "not connected",
            "niet verbonden",
            "not available",
            "niet beschikbaar",
            "invalid model",
            "onbekende",
            "nao audio",
            "nao speaker",
            "nao mic",
        ]
        connectivity_terms = [
            "connection",
            "connectionpool",
            "timed out",
            "timeout",
            "max retries exceeded",
            "temporary failure",
            "temporarily unavailable",
            "dns",
            "internet",
            "network",
            "refused",
            "unreachable",
            "host",
            "ssl",
        ]
        result_terms = [
            "empty reply",
            "empty draft",
            "empty transcript",
            "empty result",
            "leeg",
        ]
        if _detail_contains(lowered, resource_terms):
            return "resource"
        if _detail_contains(lowered, result_terms):
            return "result"
        if _detail_contains(lowered, connectivity_terms):
            return "connectivity"
        if stage_key == "llm" and "reply" in lowered:
            return "result"
        return "service"

    def _is_retryable_failure(self, kind: str) -> bool:
        return str(kind or "").strip().lower() in {"connectivity", "service"}

    def _session_mode_locked(self, session: Optional[JsonDict]) -> str:
        if not isinstance(session, dict):
            return "cloud"
        return _normalize_execution_mode(session.get("execution_mode"))

    def _config_for_mode(self, config_snapshot: JsonDict, execution_mode: str) -> JsonDict:
        base_cfg = config_snapshot.get("config") if isinstance(config_snapshot.get("config"), dict) else {}
        cfg = self._normalize_config(_deepcopy_json(base_cfg))
        if _normalize_execution_mode(execution_mode) != "local":
            return cfg
        models = cfg.get("models") if isinstance(cfg.get("models"), dict) else {}
        for stage in ("stt", "llm", "tts"):
            fallback_value = models.get(f"{stage}_fallback")
            if fallback_value:
                models[f"{stage}_primary"] = fallback_value
        return cfg

    def _build_runtime_for_mode(self, config_snapshot: JsonDict, execution_mode: str) -> Tuple[JsonDict, JsonDict]:
        return self._build_app_config(self._config_for_mode(config_snapshot, execution_mode))

    def _spool_root(self) -> Optional[str]:
        if not self.state_root:
            return None
        return os.path.join(self.state_root, "stt_spool")

    def _spool_session_dir(self, session_id: Any) -> Optional[str]:
        root = self._spool_root()
        cleaned = str(session_id or "").strip()
        if not root or not cleaned:
            return None
        return os.path.join(root, cleaned)

    def _clear_spool_dir(self, session_id: Any) -> None:
        path = self._spool_session_dir(session_id)
        if not path or not os.path.isdir(path):
            return
        for name in os.listdir(path):
            try:
                os.remove(os.path.join(path, name))
            except OSError:
                pass
        try:
            os.rmdir(path)
        except OSError:
            pass

    def _disk_spool_files(self, session: Optional[JsonDict]) -> List[str]:
        if not isinstance(session, dict):
            return []
        path = self._spool_session_dir(session.get("session_id"))
        if not path or not os.path.isdir(path):
            return []
        files = [os.path.join(path, name) for name in os.listdir(path) if name.lower().endswith(".json")]
        files.sort()
        return files

    def _refresh_spool_stats_locked(self, session: Optional[JsonDict]) -> None:
        if not isinstance(session, dict):
            return
        stats = session.get("capture_stats")
        if not isinstance(stats, dict):
            stats = {}
            session["capture_stats"] = stats
        stats["disk_spool_limit"] = self._stt_disk_spool_max_items
        stats["disk_spool_count"] = len(self._disk_spool_files(session))

    def _serialize_audio(self, audio: Any) -> JsonDict:
        pcm = getattr(audio, "pcm", b"") or b""
        if not isinstance(pcm, (bytes, bytearray)):
            raise TypeError("audio pcm must be bytes-like")
        return {
            "pcm_b64": base64.b64encode(bytes(pcm)).decode("ascii"),
            "sample_rate": int(getattr(audio, "sample_rate", 16000) or 16000),
            "channels": int(getattr(audio, "channels", 1) or 1),
            "sample_width": int(getattr(audio, "sample_width", 2) or 2),
        }

    def _deserialize_audio(self, payload: JsonDict) -> Any:
        return SimpleNamespace(
            pcm=base64.b64decode(str(payload.get("pcm_b64") or "").encode("ascii")),
            sample_rate=int(payload.get("sample_rate") or 16000),
            channels=int(payload.get("channels") or 1),
            sample_width=int(payload.get("sample_width") or 2),
        )

    def _spool_audio_locked(self, session: Optional[JsonDict], audio: Any) -> bool:
        if not isinstance(session, dict):
            return False
        self._refresh_spool_stats_locked(session)
        stats = session.get("capture_stats") if isinstance(session.get("capture_stats"), dict) else {}
        spool_count = int(stats.get("disk_spool_count") or 0)
        if spool_count >= self._stt_disk_spool_max_items:
            stats["dropped_audio_chunks"] = int(stats.get("dropped_audio_chunks") or 0) + 1
            self._set_recovery_locked(
                session,
                stage="stt",
                kind="service",
                message="STT-backlog bereikt de limiet. Niet alle nieuwe audio kan gegarandeerd behouden blijven.",
                blocking=False,
                auto_retry_used=False,
            )
            self._set_toast_locked(session, "stt_backlog_overflow", level="warning")
            return False
        seq = int(session.get("spool_next_seq") or 0) + 1
        session["spool_next_seq"] = seq
        spool_dir = self._spool_session_dir(session.get("session_id"))
        if not spool_dir:
            stats["dropped_audio_chunks"] = int(stats.get("dropped_audio_chunks") or 0) + 1
            return False
        os.makedirs(spool_dir, exist_ok=True)
        path = os.path.join(spool_dir, f"{seq:08d}.json")
        self._write_json(path, self._serialize_audio(audio))
        self._refresh_spool_stats_locked(session)
        return True

    def _next_spooled_audio_locked(self, session: Optional[JsonDict]) -> Tuple[Optional[Any], Optional[str]]:
        files = self._disk_spool_files(session)
        if not files:
            self._refresh_spool_stats_locked(session)
            return None, None
        path = files[0]
        try:
            raw = self._load_json(path)
            if not isinstance(raw, dict):
                raise ValueError("invalid spool payload")
            return self._deserialize_audio(raw), path
        except Exception:
            try:
                os.remove(path)
            except OSError:
                pass
            self._refresh_spool_stats_locked(session)
            return None, None

    def _mark_spool_processed_locked(self, session: Optional[JsonDict], path: Optional[str]) -> None:
        if path:
            try:
                os.remove(path)
            except OSError:
                pass
        self._refresh_spool_stats_locked(session)

    def _clear_pending_audio_locked(self, session: Optional[JsonDict]) -> None:
        audio_queue = self._audio_queue
        if audio_queue is not None:
            while True:
                try:
                    audio_queue.get_nowait()
                except queue.Empty:
                    break
                except Exception:
                    break
        if isinstance(session, dict):
            self._clear_spool_dir(session.get("session_id"))
            stats = session.get("capture_stats")
            if isinstance(stats, dict):
                stats["stt_queue_size"] = 0
                stats["disk_spool_count"] = 0

    def _transcribe_with_mode(self, audio: Any, *, config_snapshot: JsonDict, execution_mode: str) -> str:
        app_cfg, runtime_cfg = self._build_runtime_for_mode(config_snapshot, execution_mode)
        stt_backend = self._build_stt_backend(app_cfg)
        stt_result = self._transcribe_audio(audio, stt_backend, runtime_cfg)
        return str(getattr(stt_result, "text", "") or "").strip()

    def _generate_with_mode(
        self,
        *,
        config_snapshot: JsonDict,
        execution_mode: str,
        transcript_text: str,
        prompt_text: str,
        prompt_meta: JsonDict,
        summary_instruction: str,
        prompt_template: str,
        current_draft: Optional[str] = None,
        repair_prompt: Optional[str] = None,
    ) -> Tuple[str, JsonDict]:
        summary_cfg = self._config_for_mode(config_snapshot, execution_mode)
        app_cfg, runtime_cfg = self._build_app_config(summary_cfg)
        pipeline = self._build_pipeline(app_cfg)
        messages: List[Dict[str, str]] = []
        if prompt_text:
            messages.append({"role": "system", "content": prompt_text})
        if repair_prompt is None:
            user_prompt = prompt_template.replace("{transcript}", transcript_text).replace("{instruction}", summary_instruction)
            messages.append({"role": "user", "content": user_prompt})
        else:
            base_prompt = prompt_template.replace("{transcript}", transcript_text).replace("{instruction}", summary_instruction)
            messages.append({"role": "user", "content": base_prompt})
            if current_draft:
                messages.append({"role": "assistant", "content": str(current_draft or "")})
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "Repareer de bestaande samenvatting op basis van onderstaande instructie. "
                        "Geef alleen de herstelde samenvatting terug, zonder toelichting.\n\n"
                        f"Reparatieprompt:\n{str(repair_prompt or '').strip()}"
                    ),
                }
            )
        reply = str(self._generate_reply(pipeline, runtime_cfg, messages) or "").strip()
        if not reply:
            raise RuntimeError("empty reply")
        meta = {
            "generated_at": _utc_now(),
            "system_prompt_source": prompt_meta.get("source"),
            "system_prompt_name": prompt_meta.get("name"),
            "execution_mode": _normalize_execution_mode(execution_mode),
        }
        return reply, meta

    def _llm_job_runtime_meta(self, *, config_snapshot: JsonDict, execution_mode: str) -> JsonDict:
        summary_cfg = self._config_for_mode(config_snapshot, execution_mode)
        app_cfg, runtime_cfg = self._build_app_config(summary_cfg)
        llm_cfg = app_cfg.get("llm") if isinstance(app_cfg.get("llm"), dict) else {}
        llm_params = llm_cfg.get("params") if isinstance(llm_cfg.get("params"), dict) else {}
        return {
            "provider": str(runtime_cfg.get("llm_type") or llm_cfg.get("type") or "").strip().lower(),
            "model": str(runtime_cfg.get("llm_model") or llm_params.get("model") or "").strip(),
            "host": str(runtime_cfg.get("llm_host") or llm_params.get("host") or "").strip(),
        }

    def _llm_job_is_current_locked(self, job_id: str, session_id: str) -> bool:
        session = self._session
        active_job = self._active_llm_job
        if not isinstance(session, dict) or not isinstance(active_job, dict):
            return False
        if str(session.get("session_id") or "") != str(session_id or ""):
            return False
        return str(active_job.get("job_id") or "") == str(job_id or "")

    def _launch_llm_job(self, job: JsonDict) -> None:
        def _worker() -> None:
            try:
                self._run_llm_job(job)
            finally:
                with self._lock:
                    if self._llm_job_thread is threading.current_thread():
                        self._llm_job_thread = None

        thread_obj = threading.Thread(target=_worker, daemon=True)
        with self._lock:
            self._llm_job_thread = thread_obj
        thread_obj.start()

    def _launch_best_effort_local_llm_stop(self, job: JsonDict) -> None:
        stop_callback = self._stop_local_llm_model
        if not callable(stop_callback):
            return

        def _worker() -> None:
            session_id = str(job.get("session_id") or "")
            model = str(job.get("llm_model") or "")
            host = str(job.get("llm_host") or "")
            try:
                meta = stop_callback(host, model)
                self._log_event(
                    "info" if bool(meta.get("stopped")) else "warning",
                    "summary_local_llm_stop_attempted",
                    "Best-effort local summary LLM stop attempted.",
                    {
                        "session_id": session_id,
                        "job_id": str(job.get("job_id") or ""),
                        "llm_provider": str(job.get("llm_provider") or ""),
                        "llm_model": model,
                        "llm_host": host,
                        "stop_meta": meta,
                    },
                )
            except Exception as exc:
                self._log_event(
                    "warning",
                    "summary_local_llm_stop_failed",
                    "Best-effort local summary LLM stop failed.",
                    {
                        "session_id": session_id,
                        "job_id": str(job.get("job_id") or ""),
                        "llm_provider": str(job.get("llm_provider") or ""),
                        "llm_model": model,
                        "llm_host": host,
                        "detail": str(exc),
                    },
                )

        threading.Thread(target=_worker, daemon=True).start()

    def _log_llm_job_discarded(self, job: JsonDict, *, reason: str, detail: Optional[str] = None) -> None:
        self._log_event(
            "info",
            "summary_llm_job_discarded",
            "Summary LLM job result discarded.",
            {
                "session_id": str(job.get("session_id") or ""),
                "job_id": str(job.get("job_id") or ""),
                "kind": str(job.get("kind") or ""),
                "reason": str(reason or ""),
                "detail": str(detail or ""),
            },
        )

    def _complete_llm_job_success(self, job: JsonDict, draft_text: str, draft_meta: JsonDict) -> None:
        with self._lock:
            session_id = str(job.get("session_id") or "")
            job_id = str(job.get("job_id") or "")
            if not self._llm_job_is_current_locked(job_id, session_id):
                self._log_llm_job_discarded(job, reason="obsolete_success")
                return
            session = self._session
            if not isinstance(session, dict):
                self._log_llm_job_discarded(job, reason="missing_session")
                self._active_llm_job = None
                return
            session["status"] = "summary_review"
            session["draft_text"] = draft_text
            session["draft_meta"] = draft_meta
            self._mark_phase_reached_locked(session, "summary_review")
            session["draft_transcript_revision"] = int(session.get("transcript_revision") or 0)
            session["draft_stale"] = False
            session["draft_stale_reason"] = None
            session["last_error"] = None
            session["last_error_stage"] = None
            self._clear_recovery_locked(session, stage="llm")
            session["updated_at"] = _utc_now()
            self._active_llm_job = None
            self._persist_active_session()
            self._log_event(
                "info",
                "summary_generated" if str(job.get("kind") or "") == "generate" else "summary_repaired",
                "Summary draft generated." if str(job.get("kind") or "") == "generate" else "Summary draft repaired.",
                {"session_id": session["session_id"], "job_id": job_id},
            )

    def _complete_llm_job_failure(self, job: JsonDict, detail: str, *, attempt: int) -> None:
        with self._lock:
            session_id = str(job.get("session_id") or "")
            job_id = str(job.get("job_id") or "")
            if not self._llm_job_is_current_locked(job_id, session_id):
                self._log_llm_job_discarded(job, reason="obsolete_failure", detail=detail)
                return
            session = self._session
            if not isinstance(session, dict):
                self._log_llm_job_discarded(job, reason="missing_session_failure", detail=detail)
                self._active_llm_job = None
                return
            kind = self._classify_failure("llm", detail)
            job_kind = str(job.get("kind") or "").strip().lower()
            if job_kind == "repair":
                session["status"] = "summary_review"
                session["draft_text"] = str(job.get("current_draft") or "")
                session["last_error_stage"] = "repair"
                payload_error = "summary_repair_failed"
                event_name = "summary_repair_failed"
                event_message = "Summary repair failed."
            else:
                previous_draft = str(job.get("previous_draft") or "").strip() or None
                phase = str(job.get("origin_phase") or "transcript_review").strip().lower()
                session["status"] = "summary_review" if phase == "summary_review" and previous_draft else "transcript_review"
                if session["status"] == "summary_review" and previous_draft:
                    session["draft_text"] = previous_draft
                session["last_error_stage"] = "generate"
                payload_error = "summary_generation_failed"
                event_name = "summary_generate_failed"
                event_message = "Summary generation failed."
            session["last_error"] = "llm: " + detail
            self._set_recovery_locked(
                session,
                stage="llm",
                kind=kind,
                message=detail,
                blocking=False,
                auto_retry_used=attempt > 1,
            )
            session["updated_at"] = _utc_now()
            self._active_llm_job = None
            self._persist_active_session()
            self._log_event(
                "error",
                event_name,
                event_message,
                {"session_id": session["session_id"], "job_id": job_id, "detail": detail, "error": payload_error},
            )

    def _run_llm_job(self, job: JsonDict) -> None:
        attempt = 0
        while True:
            with self._lock:
                if not self._llm_job_is_current_locked(str(job.get("job_id") or ""), str(job.get("session_id") or "")):
                    self._log_llm_job_discarded(job, reason="obsolete_before_attempt")
                    return
            try:
                draft_text, draft_meta = self._generate_with_mode(
                    config_snapshot=_deepcopy_json(job.get("config_snapshot") or {}),
                    execution_mode=str(job.get("execution_mode") or "cloud"),
                    transcript_text=str(job.get("transcript_text") or ""),
                    prompt_text=str(job.get("prompt_text") or ""),
                    prompt_meta=_deepcopy_json(job.get("prompt_meta") or {}),
                    summary_instruction=str(job.get("summary_instruction") or ""),
                    prompt_template=str(job.get("prompt_template") or self._generate_user_template),
                    current_draft=(None if job.get("kind") == "generate" else str(job.get("current_draft") or "")),
                    repair_prompt=(None if job.get("kind") == "generate" else str(job.get("repair_prompt") or "")),
                )
                if job.get("kind") == "repair":
                    draft_meta["repair_prompt_used"] = True
                self._complete_llm_job_success(job, draft_text, draft_meta)
                return
            except Exception as exc:
                detail = str(exc)
                kind = self._classify_failure("llm", detail)
                attempt += 1
                if self._is_retryable_failure(kind) and attempt <= 1:
                    time.sleep(self._auto_retry_delay_s)
                    continue
                self._complete_llm_job_failure(job, detail, attempt=attempt)
                return

    def _publish_with_mode(self, *, config_snapshot: JsonDict, execution_mode: str, draft_text: str) -> None:
        summary_cfg = self._config_for_mode(config_snapshot, execution_mode)
        app_cfg, runtime_cfg = self._build_app_config(summary_cfg)
        pipeline = self._build_pipeline(app_cfg)
        self._publish_text(pipeline, runtime_cfg, draft_text)

    def _payload(
        self,
        *,
        error: Optional[str] = None,
        detail: Optional[str] = None,
        phase: Optional[str] = None,
        session: Optional[JsonDict] = None,
    ) -> JsonDict:
        payload: JsonDict = {
            "ok": error is None,
            "instance_id": self.instance_id,
            "config": _deepcopy_json(self._config),
            "session": self._session_public(session if session is not None else self._session),
            "connectivity_issues": self._connectivity_snapshot(),
            "execution_mode": self._session_mode_locked(session if session is not None else self._session),
            "reachable_phases": self._reachable_phases_locked(session if session is not None else self._session),
        }
        if error:
            payload["error"] = error
        if detail:
            payload["detail"] = detail
        if phase:
            payload["phase"] = phase
        return payload

    def _session_public(self, session: Optional[JsonDict]) -> Optional[JsonDict]:
        if session is None:
            return None
        return _deepcopy_json(session)

    def _normalize_reached_phases_locked(self, session: Optional[JsonDict], raw: Any) -> List[str]:
        if not isinstance(session, dict):
            return []
        phases: List[str] = []
        if isinstance(raw, list):
            for item in raw:
                cleaned = str(item or "").strip().lower()
                if cleaned in _SUMMARY_PHASE_TARGETS and cleaned not in phases:
                    phases.append(cleaned)
        if "capture" not in phases:
            phases.insert(0, "capture")
        if (bool(session.get("transcript_lines")) or _has_meaningful_text(session.get("transcript_text"))) and "transcript_review" not in phases:
            phases.append("transcript_review")
        if _has_meaningful_text(session.get("draft_text")) and "summary_review" not in phases:
            phases.append("summary_review")
        ordered: List[str] = []
        for candidate in _SUMMARY_PHASE_TARGETS:
            if candidate in phases:
                ordered.append(candidate)
        return ordered

    def _mark_phase_reached_locked(self, session: Optional[JsonDict], phase: str) -> None:
        if not isinstance(session, dict):
            return
        cleaned = str(phase or "").strip().lower()
        if cleaned not in _SUMMARY_PHASE_TARGETS:
            return
        phases = self._normalize_reached_phases_locked(session, session.get("reached_phases"))
        if cleaned not in phases:
            phases.append(cleaned)
        session["reached_phases"] = self._normalize_reached_phases_locked(session, phases)

    def _reachable_phases_locked(self, session: Optional[JsonDict]) -> List[str]:
        if not isinstance(session, dict):
            return []
        status = str(session.get("status") or "").strip().lower()
        if status == "error":
            return []
        if status in {"summarizing", "publishing"}:
            return []
        return self._normalize_reached_phases_locked(session, session.get("reached_phases"))

    def _has_pending_audio_locked(self, session: Optional[JsonDict]) -> bool:
        if not isinstance(session, dict):
            return False
        queue_has_items = False
        audio_queue = self._audio_queue
        if audio_queue is not None:
            try:
                queue_has_items = int(audio_queue.qsize()) > 0
            except Exception:
                try:
                    queue_has_items = not bool(audio_queue.empty())
                except Exception:
                    queue_has_items = False
        self._refresh_spool_stats_locked(session)
        stats = session.get("capture_stats") if isinstance(session.get("capture_stats"), dict) else {}
        spool_count = int(stats.get("disk_spool_count") or 0)
        return queue_has_items or spool_count > 0

    def try_auto_resume_stt_connectivity_recovery(self) -> bool:
        resume_event: Optional[threading.Event] = None
        session_id = ""
        with self._lock:
            session = self._session
            if not isinstance(session, dict):
                return False
            phase = str(session.get("status") or "").strip().lower()
            if phase != "capturing":
                return False
            if self._session_mode_locked(session) != "cloud":
                return False
            recovery = session.get("recovery") if isinstance(session.get("recovery"), dict) else {}
            if str(recovery.get("stage") or "").strip().lower() != "stt":
                return False
            if str(recovery.get("kind") or "").strip().lower() != "connectivity":
                return False
            if not bool(recovery.get("blocking")):
                return False
            if not self._has_pending_audio_locked(session):
                return False
            resume_event = self._stt_resume_event
            if resume_event is None:
                return False
            session_id = str(session.get("session_id") or "")
            self._clear_recovery_locked(session, stage="stt")
            session["last_error"] = None
            session["last_error_stage"] = None
            self._set_toast_locked(session, "internet_restored", level="info")
            session["updated_at"] = _utc_now()
            self._persist_active_session()
            self._log_event(
                "info",
                "summary_capture_auto_resumed",
                "Summary STT recovery auto-resumed after connectivity probe recovered.",
                {"session_id": session_id},
            )
        resume_event.set()
        return True

    def _new_error_session(self, detail: str) -> JsonDict:
        return {
            "session_id": str(uuid.uuid4()),
            "status": "error",
            "execution_mode": "cloud",
            "created_at": _utc_now(),
            "updated_at": _utc_now(),
            "config_snapshot": None,
            "transcript_lines": [],
            "transcript_text": "",
            "draft_text": None,
            "draft_meta": None,
            "published_text": None,
            "published_at": None,
            "save_meta": None,
            "last_error": str(detail or "summary load error"),
            "last_error_stage": "load",
            "last_repair_prompt": None,
            "reached_phases": [],
            "transcript_revision": 0,
            "draft_transcript_revision": None,
            "draft_stale": False,
            "draft_stale_reason": None,
            "recovery": None,
            "toast": None,
            "spool_next_seq": 0,
            "capture_stats": {
                "loops": 0,
                "timeouts": 0,
                "audio_chunks": 0,
                "dropped_audio_chunks": 0,
                "stt_calls": 0,
                "transcript_count": 0,
                "stt_queue_max": self._stt_queue_max_items,
                "stt_queue_size": 0,
                "disk_spool_limit": self._stt_disk_spool_max_items,
                "disk_spool_count": 0,
                "started_at": None,
                "last_audio_at": None,
                "last_transcript_at": None,
            },
        }

    def _new_session(self, *, config_snapshot: JsonDict, started_at: float) -> JsonDict:
        return {
            "session_id": str(uuid.uuid4()),
            "status": "capturing",
            "execution_mode": "cloud",
            "created_at": _utc_now(),
            "updated_at": _utc_now(),
            "config_snapshot": config_snapshot,
            "transcript_lines": [],
            "transcript_text": "",
            "draft_text": None,
            "draft_meta": None,
            "published_text": None,
            "published_at": None,
            "save_meta": None,
            "last_error": None,
            "last_error_stage": None,
            "last_repair_prompt": None,
            "reached_phases": ["capture"],
            "transcript_revision": 0,
            "draft_transcript_revision": None,
            "draft_stale": False,
            "draft_stale_reason": None,
            "recovery": None,
            "toast": None,
            "spool_next_seq": 0,
            "capture_stats": {
                "loops": 0,
                "timeouts": 0,
                "audio_chunks": 0,
                "dropped_audio_chunks": 0,
                "stt_calls": 0,
                "transcript_count": 0,
                "stt_queue_max": self._stt_queue_max_items,
                "stt_queue_size": 0,
                "disk_spool_limit": self._stt_disk_spool_max_items,
                "disk_spool_count": 0,
                "started_at": started_at,
                "last_audio_at": None,
                "last_transcript_at": None,
            },
        }

    def _resolve_capture_timeout(self, app_cfg: JsonDict) -> float:
        input_cfg = (app_cfg.get("input") or {}) if isinstance(app_cfg, dict) else {}
        params = (input_cfg.get("params") or {}) if isinstance(input_cfg, dict) else {}
        try:
            value = float(params.get("start_timeout_s", self._capture_timeout_default_s))
        except Exception:
            value = self._capture_timeout_default_s
        if value <= 0:
            value = self._capture_timeout_default_s
        return max(0.25, min(value, self._capture_timeout_max_s))

    def _build_config_snapshot_locked(self) -> Tuple[JsonDict, Optional[JsonDict]]:
        summary_cfg = _deepcopy_json(self._config)
        prompt_text, prompt_meta, prompt_err = self._resolve_prompt(summary_cfg)
        if prompt_err:
            return {}, self._payload(error=str(prompt_err), detail=str(prompt_err))
        prompts_cfg = summary_cfg.get("prompts") if isinstance(summary_cfg.get("prompts"), dict) else {}
        return {
            "config": summary_cfg,
            "system_prompt_text": str(prompt_text or ""),
            "system_prompt_meta": _deepcopy_json(prompt_meta),
            "input_prompt_template": str(prompts_cfg.get("input_prompt_template") or self._generate_user_template),
            "summary_instruction": str(prompts_cfg.get("summary_instruction") or ""),
        }, None

    def _config_path(self) -> Optional[str]:
        if not self.state_root:
            return None
        return os.path.join(self.state_root, "summary_runtime.config.json")

    def _legacy_config_path(self) -> Optional[str]:
        if not self.state_root:
            return None
        return os.path.join(self.state_root, "config.json")

    def _active_session_path(self) -> Optional[str]:
        if not self.state_root:
            return None
        return os.path.join(self.state_root, "active_session.json")

    def _saved_dir_path(self) -> str:
        return os.path.join(str(self.state_root or ""), "saved")

    def _saved_json_path(self, session_id: str) -> str:
        return os.path.join(self._saved_dir_path(), f"{session_id}.json")

    def _saved_text_path(self, session_id: str) -> str:
        return os.path.join(self._saved_dir_path(), f"{session_id}.txt")

    def _load_json(self, path: Optional[str]) -> Optional[JsonDict]:
        if not path or not os.path.isfile(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
        except Exception:
            return None
        return raw if isinstance(raw, dict) else None

    def _write_json(self, path: str, payload: JsonDict) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp_path = path + "." + uuid.uuid4().hex + ".tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=2)
            os.replace(tmp_path, path)
        finally:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass

    def _write_text(self, path: str, content: str) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp_path = path + "." + uuid.uuid4().hex + ".tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as fh:
                fh.write(str(content or ""))
            os.replace(tmp_path, path)
        finally:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass

    def _persist_config(self) -> None:
        path = self._config_path()
        if not path:
            return
        self._write_json(path, _deepcopy_json(self._config))

    def _persist_active_session(self) -> None:
        path = self._active_session_path()
        if not path or self._session is None:
            return
        self._write_json(path, _deepcopy_json(self._session))

    def _normalize_loaded_session(self, raw: JsonDict) -> JsonDict:
        session = _deepcopy_json(raw)
        status = str(session.get("status") or "error").strip().lower()
        session["status"] = _fallback_status_for_inflight(status)
        if status in {"capturing", "summarizing", "publishing"}:
            session["last_error"] = "session recovered after restart"
            session["last_error_stage"] = "restart"
        session["updated_at"] = _utc_now()
        lines = session.get("transcript_lines")
        if not isinstance(lines, list):
            lines = []
        normalized_lines: List[JsonDict] = []
        for line in lines:
            if not isinstance(line, dict):
                continue
            text = str(line.get("text") or "").strip()
            if not text:
                continue
            normalized_lines.append(
                {
                    "id": str(line.get("id") or str(uuid.uuid4())),
                    "text": text,
                    "source": str(line.get("source") or "stt").strip() or "stt",
                    "created_at": str(line.get("created_at") or _utc_now()),
                }
            )
        session["transcript_lines"] = normalized_lines
        session["transcript_text"] = _transcript_text_from_lines(normalized_lines)
        config_snapshot = session.get("config_snapshot")
        if isinstance(config_snapshot, dict):
            if "config" not in config_snapshot:
                legacy_cfg: JsonDict = {}
                runtime_overrides = config_snapshot.get("runtime_overrides")
                if isinstance(runtime_overrides, dict):
                    legacy_cfg["runtime_overrides"] = _deepcopy_json(runtime_overrides)
                prompt_meta = config_snapshot.get("system_prompt_meta")
                if isinstance(prompt_meta, dict) and str(prompt_meta.get("source") or "") == "inline":
                    legacy_cfg["system_prompt"] = str(config_snapshot.get("system_prompt_text") or "")
                elif isinstance(prompt_meta, dict) and prompt_meta.get("name"):
                    legacy_cfg["system_prompt_file"] = prompt_meta.get("name")
                else:
                    legacy_cfg["system_prompt"] = str(config_snapshot.get("system_prompt_text") or "")
                try:
                    config_snapshot["config"] = self._normalize_config(legacy_cfg)
                except Exception:
                    config_snapshot["config"] = _deepcopy_json(self._config)
            if "input_prompt_template" not in config_snapshot:
                config_snapshot["input_prompt_template"] = self._generate_user_template
            if "summary_instruction" not in config_snapshot:
                config_snapshot["summary_instruction"] = ""
            session["config_snapshot"] = config_snapshot
        if "config_snapshot" not in session:
            session["config_snapshot"] = None
        if "draft_text" not in session:
            session["draft_text"] = None
        if "draft_meta" not in session:
            session["draft_meta"] = None
        if "published_text" not in session:
            session["published_text"] = None
        if "published_at" not in session:
            session["published_at"] = None
        if "save_meta" not in session:
            session["save_meta"] = None
        if "last_error" not in session:
            session["last_error"] = None
        if "last_error_stage" not in session:
            session["last_error_stage"] = None
        session["execution_mode"] = _normalize_execution_mode(session.get("execution_mode"))
        if "last_repair_prompt" not in session:
            session["last_repair_prompt"] = None
        session["transcript_revision"] = max(0, int(session.get("transcript_revision") or (1 if session.get("transcript_text") else 0)))
        draft_text = str(session.get("draft_text") or "").strip()
        draft_revision_raw = session.get("draft_transcript_revision")
        if draft_revision_raw is None and draft_text:
            session["draft_transcript_revision"] = int(session.get("transcript_revision") or 0)
        else:
            session["draft_transcript_revision"] = int(draft_revision_raw or 0) if draft_revision_raw is not None else None
        session["draft_stale"] = bool(session.get("draft_stale")) if draft_text else False
        session["draft_stale_reason"] = str(session.get("draft_stale_reason") or "").strip() or None
        session["reached_phases"] = self._normalize_reached_phases_locked(session, session.get("reached_phases"))
        if not isinstance(session.get("recovery"), dict):
            session["recovery"] = None
        if not isinstance(session.get("toast"), dict):
            session["toast"] = None
        session["spool_next_seq"] = int(session.get("spool_next_seq") or 0)
        stats = session.get("capture_stats")
        if not isinstance(stats, dict):
            stats = {}
        stats["stt_queue_max"] = self._stt_queue_max_items
        stats["stt_queue_size"] = 0
        stats["disk_spool_limit"] = self._stt_disk_spool_max_items
        stats["transcript_count"] = len(normalized_lines)
        session["capture_stats"] = stats
        self._refresh_spool_stats_locked(session)
        return session

    def _load_persisted_state(self) -> None:
        with self._lock:
            raw_cfg = self._load_json(self._config_path())
            if not isinstance(raw_cfg, dict):
                raw_cfg = self._load_json(self._legacy_config_path())
            try:
                self._config = self._normalize_config(raw_cfg or self._config)
            except Exception:
                self._config = self._normalize_config(self._config)
            session = self._load_json(self._active_session_path())
            if not isinstance(session, dict):
                self._session = None
                return
            try:
                self._session = self._normalize_loaded_session(session)
            except Exception as exc:
                self._session = self._new_error_session(f"load: {exc}")
            if isinstance(self._session, dict):
                recovered_status = str(self._session.get("last_error_stage") or "").strip().lower()
                if recovered_status == "restart":
                    self._clear_spool_dir(self._session.get("session_id"))
                    self._refresh_spool_stats_locked(self._session)
            self._persist_active_session()

    def _mark_draft_stale_locked(self, session: Optional[JsonDict], reason: str) -> None:
        if not isinstance(session, dict):
            return
        if not _has_meaningful_text(session.get("draft_text")):
            session["draft_stale"] = False
            session["draft_stale_reason"] = None
            return
        session["draft_stale"] = True
        session["draft_stale_reason"] = str(reason or "").strip() or "Transcript gewijzigd sinds de laatste samenvatting."

    def _mark_transcript_changed_locked(self, session: Optional[JsonDict], *, reason: str) -> None:
        if not isinstance(session, dict):
            return
        session["transcript_revision"] = int(session.get("transcript_revision") or 0) + 1
        self._mark_draft_stale_locked(session, reason)

    def _set_transcript_lines_locked(self, session: Optional[JsonDict], lines: List[JsonDict], *, reason: str) -> None:
        if not isinstance(session, dict):
            return
        previous_text = str(session.get("transcript_text") or "")
        session["transcript_lines"] = lines
        session["transcript_text"] = _transcript_text_from_lines(lines)
        stats = session.get("capture_stats")
        if isinstance(stats, dict):
            stats["transcript_count"] = len(lines)
        if session["transcript_text"] != previous_text:
            self._mark_transcript_changed_locked(session, reason=reason)

    def _append_transcript_line_locked(self, session: Optional[JsonDict], text: str, *, source: str, reason: str) -> None:
        if not isinstance(session, dict):
            return
        cleaned = str(text or "").strip()
        if not cleaned:
            return
        lines = session.get("transcript_lines")
        if not isinstance(lines, list):
            lines = []
            session["transcript_lines"] = lines
        lines.append(
            {
                "id": str(uuid.uuid4()),
                "text": cleaned,
                "source": str(source or "stt").strip() or "stt",
                "created_at": _utc_now(),
            }
        )
        session["transcript_text"] = _transcript_text_from_lines(lines)
        stats = session.get("capture_stats")
        if isinstance(stats, dict):
            stats["transcript_count"] = len(lines)
            stats["last_transcript_at"] = time.time()
        self._mark_transcript_changed_locked(session, reason=reason)

    def _clear_terminal_publish_fields_locked(self, session: Optional[JsonDict]) -> None:
        if not isinstance(session, dict):
            return
        session["published_text"] = None
        session["published_at"] = None

    def _launch_capture_runtime_locked(
        self,
        session: JsonDict,
        *,
        mic: Any,
        timeout_s: float,
        event_name: str,
        event_message: str,
    ) -> None:
        self._clear_pending_audio_locked(session)
        stop_event = threading.Event()
        resume_event = threading.Event()
        resume_event.set()
        audio_queue: "queue.Queue[Any]" = queue.Queue(maxsize=self._stt_queue_max_items)
        self._stop_event = stop_event
        self._stt_resume_event = resume_event
        self._audio_queue = audio_queue
        self._clear_spool_dir(session.get("session_id"))
        self._refresh_spool_stats_locked(session)
        session["status"] = "capturing"
        session["last_error"] = None
        session["last_error_stage"] = None
        session["recovery"] = None
        self._clear_toast_locked(session)
        stats = session.get("capture_stats")
        if isinstance(stats, dict):
            stats["stt_queue_size"] = 0
            stats["stt_queue_max"] = self._stt_queue_max_items
            stats["disk_spool_limit"] = self._stt_disk_spool_max_items
            stats["disk_spool_count"] = 0
            if not stats.get("started_at"):
                stats["started_at"] = time.time()

        def _update_queue_size() -> None:
            try:
                qsize = int(audio_queue.qsize())
            except Exception:
                qsize = 0
            with self._lock:
                current_session = self._session
                if current_session is None:
                    return
                current_stats = current_session.get("capture_stats")
                if isinstance(current_stats, dict):
                    current_stats["stt_queue_size"] = max(0, qsize)
                    self._refresh_spool_stats_locked(current_session)
                    current_session["updated_at"] = _utc_now()
                    self._persist_active_session()

        def _record_timeout() -> None:
            with self._lock:
                current_session = self._session
                if current_session is None:
                    return
                current_stats = current_session.get("capture_stats")
                if isinstance(current_stats, dict):
                    current_stats["timeouts"] = int(current_stats.get("timeouts") or 0) + 1
                    current_session["updated_at"] = _utc_now()
                    self._persist_active_session()

        def _enqueue_audio(audio: Any) -> None:
            with self._lock:
                current_session = self._session
                if current_session is not None:
                    current_stats = current_session.get("capture_stats")
                    if isinstance(current_stats, dict):
                        current_stats["audio_chunks"] = int(current_stats.get("audio_chunks") or 0) + 1
                        current_stats["last_audio_at"] = time.time()
                        current_session["updated_at"] = _utc_now()
                        self._persist_active_session()

            with self._lock:
                current_session = self._session
                if current_session is None:
                    return
                current_stats = current_session.get("capture_stats") if isinstance(current_session.get("capture_stats"), dict) else {}
                spool_count = int(current_stats.get("disk_spool_count") or 0)
                if spool_count > 0:
                    self._spool_audio_locked(current_session, audio)
                    current_session["updated_at"] = _utc_now()
                    self._persist_active_session()
                    return
            try:
                audio_queue.put_nowait(audio)
                _update_queue_size()
                return
            except queue.Full:
                pass
            with self._lock:
                current_session = self._session
                if current_session is None:
                    return
                self._spool_audio_locked(current_session, audio)
                current_session["updated_at"] = _utc_now()
                self._persist_active_session()

        def _capture_worker() -> None:
            try:
                stream_utterances = getattr(mic, "stream_utterances", None)
                if callable(stream_utterances):
                    with self._lock:
                        current_session = self._session
                        if current_session is not None:
                            current_stats = current_session.get("capture_stats")
                            if isinstance(current_stats, dict):
                                current_stats["loops"] = int(current_stats.get("loops") or 0) + 1
                                current_session["updated_at"] = _utc_now()
                                self._persist_active_session()
                    try:
                        stream_utterances(
                            stop_event,
                            timeout_s=timeout_s,
                            on_utterance=_enqueue_audio,
                            on_timeout=_record_timeout,
                        )
                    except Exception as exc:
                        if not stop_event.is_set():
                            with self._lock:
                                current_session = self._session
                                if current_session is not None:
                                    current_session["last_error"] = "mic: " + str(exc)
                                    current_session["last_error_stage"] = "capture"
                                    current_session["updated_at"] = _utc_now()
                                    self._persist_active_session()
                    return

                while not stop_event.is_set():
                    with self._lock:
                        current_session = self._session
                        if current_session is None:
                            break
                        current_stats = current_session.get("capture_stats")
                        if isinstance(current_stats, dict):
                            current_stats["loops"] = int(current_stats.get("loops") or 0) + 1
                            current_session["updated_at"] = _utc_now()
                            self._persist_active_session()
                    try:
                        audio = mic.capture_utterance(timeout_s=timeout_s)
                    except TimeoutError:
                        _record_timeout()
                        continue
                    except Exception as exc:
                        if stop_event.is_set():
                            break
                        with self._lock:
                            current_session = self._session
                            if current_session is not None:
                                current_session["last_error"] = "mic: " + str(exc)
                                current_session["last_error_stage"] = "capture"
                                current_session["updated_at"] = _utc_now()
                                self._persist_active_session()
                        continue
                    if stop_event.is_set():
                        break
                    _enqueue_audio(audio)
            finally:
                with self._lock:
                    if self._capture_thread is threading.current_thread():
                        self._capture_thread = None

        def _stt_worker() -> None:
            try:
                while True:
                    if stop_event.is_set():
                        with self._lock:
                            current_session = self._session
                            current_stats = current_session.get("capture_stats") if isinstance((current_session or {}).get("capture_stats"), dict) else {}
                            spool_count = int(current_stats.get("disk_spool_count") or 0)
                        if audio_queue.empty() and spool_count <= 0:
                            break
                    if not resume_event.is_set():
                        if stop_event.is_set():
                            break
                        time.sleep(0.05)
                        continue
                    queued_audio = None
                    queued_spool_path = None
                    queued_mode = "memory"
                    try:
                        queued_audio = audio_queue.get(timeout=0.1)
                    except queue.Empty:
                        with self._lock:
                            current_session = self._session
                            queued_audio, queued_spool_path = self._next_spooled_audio_locked(current_session)
                            if queued_audio is not None:
                                queued_mode = "disk"
                    if queued_audio is None:
                        continue
                    _update_queue_size()
                    attempt = 0
                    while True:
                        with self._lock:
                            current_session = self._session
                            if current_session is None:
                                break
                            current_stats = current_session.get("capture_stats")
                            if isinstance(current_stats, dict):
                                current_stats["stt_calls"] = int(current_stats.get("stt_calls") or 0) + 1
                                current_session["updated_at"] = _utc_now()
                                self._persist_active_session()
                            config_snapshot_local = _deepcopy_json(current_session.get("config_snapshot") or {})
                            execution_mode = self._session_mode_locked(current_session)
                        try:
                            text = self._transcribe_with_mode(
                                queued_audio,
                                config_snapshot=config_snapshot_local,
                                execution_mode=execution_mode,
                            )
                        except Exception as exc:
                            detail = str(exc)
                            kind = self._classify_failure("stt", detail)
                            attempt += 1
                            if self._is_retryable_failure(kind) and attempt <= 1:
                                time.sleep(self._auto_retry_delay_s)
                                continue
                            with self._lock:
                                current_session = self._session
                                if current_session is not None:
                                    current_session["last_error"] = "stt: " + detail
                                    current_session["last_error_stage"] = "capture"
                                    self._set_recovery_locked(
                                        current_session,
                                        stage="stt",
                                        kind=kind,
                                        message=detail,
                                        blocking=True,
                                        auto_retry_used=attempt > 1,
                                    )
                                    current_session["updated_at"] = _utc_now()
                                    self._persist_active_session()
                            resume_event.clear()
                            break
                        with self._lock:
                            current_session = self._session
                            if current_session is None:
                                break
                            self._clear_recovery_locked(current_session, stage="stt")
                            current_session["last_error"] = None
                            current_session["last_error_stage"] = None
                            if text:
                                self._append_transcript_line_locked(
                                    current_session,
                                    text,
                                    source="stt",
                                    reason="Transcript uitgebreid na hervatten of live opname.",
                                )
                            if queued_mode == "disk":
                                self._mark_spool_processed_locked(current_session, queued_spool_path)
                            current_session["updated_at"] = _utc_now()
                            self._persist_active_session()
                            break
            finally:
                with self._lock:
                    if self._stt_thread is threading.current_thread():
                        self._stt_thread = None
                _update_queue_size()

        capture_thread = threading.Thread(target=_capture_worker, daemon=True)
        stt_thread = threading.Thread(target=_stt_worker, daemon=True)
        self._capture_thread = capture_thread
        self._stt_thread = stt_thread
        session["updated_at"] = _utc_now()
        self._persist_active_session()
        self._log_event(
            "info",
            event_name,
            event_message,
            {"session_id": session["session_id"], "status": session["status"]},
        )
        stt_thread.start()
        capture_thread.start()

    def _stop_capture_runtime(self) -> bool:
        with self._lock:
            stop_event = self._stop_event
            resume_event = self._stt_resume_event
            capture_thread = self._capture_thread
            stt_thread = self._stt_thread
        if stop_event is not None:
            try:
                stop_event.set()
            except Exception:
                pass
        if resume_event is not None:
            try:
                resume_event.set()
            except Exception:
                pass
        for thread_obj in (capture_thread, stt_thread):
            if thread_obj is None or not hasattr(thread_obj, "is_alive"):
                continue
            try:
                if thread_obj.is_alive():
                    thread_obj.join(timeout=self._worker_join_timeout_s)
            except Exception:
                pass
        capture_alive = bool(capture_thread is not None and capture_thread.is_alive())
        stt_alive = bool(stt_thread is not None and stt_thread.is_alive())
        alive = bool(capture_alive or stt_alive)
        if not alive:
            with self._lock:
                self._capture_thread = None
                self._stt_thread = None
                self._stop_event = None
                self._stt_resume_event = None
                self._audio_queue = None
                session = self._session
                if session is not None:
                    stats = session.get("capture_stats")
                    if isinstance(stats, dict):
                        stats["stt_queue_size"] = 0
                        self._refresh_spool_stats_locked(session)
                        session["updated_at"] = _utc_now()
                        self._persist_active_session()
        return not alive

    def start(self) -> Tuple[JsonDict, int]:
        with self._lock:
            current = self._session
            if current and not _is_terminal_status(str(current.get("status") or "")):
                return {
                    "ok": False,
                    "error": "summary_session_already_active",
                    "instance_id": self.instance_id,
                    "session": self._session_public(current),
                    "config": _deepcopy_json(self._config),
                    "connectivity_issues": self._connectivity_snapshot(),
                }, 409

            config_snapshot, err_payload = self._build_config_snapshot_locked()
            if err_payload is not None:
                return err_payload, 400

            try:
                app_cfg, _runtime_cfg = self._build_runtime_for_mode(config_snapshot, "cloud")
                mic = self._make_mic(app_cfg)
            except Exception as exc:
                session = self._new_error_session(f"start: {exc}")
                self._session = session
                self._persist_active_session()
                self._log_event("error", "summary_start_failed", "Summary start failed.", {"detail": str(exc)})
                return self._payload(error="mic_unavailable", detail=str(exc), session=session), 400

            timeout_s = self._resolve_capture_timeout(app_cfg)
            started_at = time.time()
            session = self._new_session(config_snapshot=config_snapshot, started_at=started_at)
            self._session = session
            self._active_llm_job = None

            try:
                self._build_stt_backend(app_cfg)
            except Exception as exc:
                session["status"] = "error"
                session["last_error"] = "stt_init: " + str(exc)
                session["last_error_stage"] = "start"
                session["updated_at"] = _utc_now()
                self._persist_active_session()
                self._log_event("error", "summary_start_failed", "Summary STT initialization failed.", {"detail": str(exc)})
                return self._payload(error="stt_unavailable", detail=str(exc)), 400

            self._launch_capture_runtime_locked(
                session,
                mic=mic,
                timeout_s=timeout_s,
                event_name="summary_started",
                event_message="Summary session started.",
            )
        return self.get_payload(), 200

    def stop(self) -> Tuple[JsonDict, int]:
        with self._lock:
            session = self._session
            phase = str((session or {}).get("status") or "idle").strip().lower()
            recovery = session.get("recovery") if isinstance((session or {}).get("recovery"), dict) else {}
        if session is None or phase != "capturing":
            return self._payload(error="invalid_summary_state", phase=phase), 400
        if str(recovery.get("stage") or "").strip().lower() == "stt" and bool(recovery.get("blocking")):
            with self._lock:
                self._clear_pending_audio_locked(self._session)
                if self._session is not None:
                    self._session["updated_at"] = _utc_now()
                    self._persist_active_session()
        if not self._stop_capture_runtime():
            return self._payload(error="summary_capture_stop_timeout", phase=phase), 409
        with self._lock:
            session = self._session
            if session is None:
                return self._payload(error="summary_session_missing"), 409
            session["status"] = "transcript_review"
            self._mark_phase_reached_locked(session, "transcript_review")
            self._clear_recovery_locked(session, stage="stt")
            session["updated_at"] = _utc_now()
            self._clear_spool_dir(session.get("session_id"))
            self._refresh_spool_stats_locked(session)
            self._persist_active_session()
            self._log_event(
                "info",
                "summary_capture_stopped",
                "Summary capture stopped.",
                {"session_id": session["session_id"], "transcript_count": len(session.get("transcript_lines") or [])},
            )
            return self._payload(), 200

    def update_transcript(self, transcript_text: Any) -> Tuple[JsonDict, int]:
        with self._lock:
            session = self._session
            phase = str((session or {}).get("status") or "idle").strip().lower()
            if session is None or phase != "transcript_review":
                return self._payload(error="invalid_summary_state", phase=phase), 400
            lines = _lines_from_transcript_text(str(transcript_text or ""), source="manual")
            self._set_transcript_lines_locked(session, lines, reason="Transcript handmatig aangepast.")
            session["updated_at"] = _utc_now()
            self._persist_active_session()
            self._log_event(
                "info",
                "summary_transcript_updated",
                "Summary transcript updated.",
                {"session_id": session["session_id"], "transcript_count": len(lines)},
            )
            return self._payload(), 200

    def update_draft(self, draft_text: Any) -> Tuple[JsonDict, int]:
        with self._lock:
            session = self._session
            phase = str((session or {}).get("status") or "idle").strip().lower()
            if session is None or phase != "summary_review":
                return self._payload(error="invalid_summary_state", phase=phase), 400
            cleaned = str(draft_text or "").strip()
            if not cleaned:
                session["last_error"] = "summary: empty draft"
                session["last_error_stage"] = "draft"
                session["updated_at"] = _utc_now()
                self._persist_active_session()
                return self._payload(error="summary_draft_empty", phase=phase), 400
            session["draft_text"] = cleaned
            draft_meta = session.get("draft_meta")
            if not isinstance(draft_meta, dict):
                draft_meta = {}
                session["draft_meta"] = draft_meta
            draft_meta["edited_at"] = _utc_now()
            draft_meta["edited_manually"] = True
            session["last_error"] = None
            session["last_error_stage"] = None
            self._clear_recovery_locked(session, stage="llm")
            self._clear_recovery_locked(session, stage="tts")
            session["updated_at"] = _utc_now()
            self._persist_active_session()
            self._log_event(
                "info",
                "summary_draft_updated",
                "Summary draft updated.",
                {"session_id": session["session_id"]},
            )
            return self._payload(), 200

    def update_execution_mode(self, raw_mode: Any) -> Tuple[JsonDict, int]:
        with self._lock:
            session = self._session
            phase = str((session or {}).get("status") or "idle").strip().lower()
            if session is None or _is_terminal_status(phase):
                return self._payload(error="invalid_summary_state", phase=phase), 400
            if phase in {"summarizing", "publishing"}:
                return self._payload(error="invalid_summary_state", phase=phase), 409
            mode = _normalize_execution_mode(raw_mode)
            session["execution_mode"] = mode
            session["updated_at"] = _utc_now()
            self._set_toast_locked(session, "mode_switched_local" if mode == "local" else "mode_switched_cloud", level="info")
            self._persist_active_session()
            self._log_event(
                "info",
                "summary_execution_mode_updated",
                "Summary execution mode updated.",
                {"session_id": session.get("session_id"), "execution_mode": mode},
            )
            return self._payload(), 200

    def recover_capture(self) -> Tuple[JsonDict, int]:
        with self._lock:
            session = self._session
            phase = str((session or {}).get("status") or "idle").strip().lower()
            recovery = session.get("recovery") if isinstance((session or {}).get("recovery"), dict) else {}
            resume_event = self._stt_resume_event
            if session is None or phase != "capturing":
                return self._payload(error="invalid_summary_state", phase=phase), 400
            if str(recovery.get("stage") or "").strip().lower() != "stt":
                return self._payload(error="summary_capture_not_blocked", phase=phase), 409
            self._clear_recovery_locked(session, stage="stt")
            session["last_error"] = None
            session["last_error_stage"] = None
            session["updated_at"] = _utc_now()
            self._persist_active_session()
        if resume_event is not None:
            resume_event.set()
        return self._payload(), 200

    def navigate(self, target_phase: Any) -> Tuple[JsonDict, int]:
        target = str(target_phase or "").strip().lower()
        if target not in {"capture", "transcript_review", "summary_review"}:
            return self._payload(error="invalid_target_phase", detail=str(target_phase or "")), 400
        with self._lock:
            session = self._session
            phase = str((session or {}).get("status") or "idle").strip().lower()
            if session is None:
                return self._payload(error="summary_session_missing", phase=phase), 404
            if phase in {"summarizing", "publishing"}:
                return self._payload(error="invalid_summary_state", phase=phase), 409
            reachable = self._reachable_phases_locked(session)
            if target not in reachable:
                return self._payload(error="summary_phase_not_reachable", phase=phase), 409

            if phase in {"completed", "aborted"}:
                self._clear_terminal_publish_fields_locked(session)

            session["last_error"] = None
            session["last_error_stage"] = None
            session["recovery"] = None
            self._clear_toast_locked(session)

            if target == "transcript_review":
                session["status"] = "transcript_review"
                self._mark_phase_reached_locked(session, "transcript_review")
                session["updated_at"] = _utc_now()
                self._persist_active_session()
                self._log_event(
                    "info",
                    "summary_navigated",
                    "Summary navigation applied.",
                    {"session_id": session["session_id"], "target_phase": target},
                )
                return self._payload(), 200

            if target == "summary_review":
                if not _has_meaningful_text(session.get("draft_text")):
                    return self._payload(error="summary_draft_not_ready", phase=phase), 409
                session["status"] = "summary_review"
                self._mark_phase_reached_locked(session, "summary_review")
                session["updated_at"] = _utc_now()
                self._persist_active_session()
                self._log_event(
                    "info",
                    "summary_navigated",
                    "Summary navigation applied.",
                    {"session_id": session["session_id"], "target_phase": target},
                )
                return self._payload(), 200

            session["status"] = "capture_ready"
            session["updated_at"] = _utc_now()
            self._persist_active_session()
            self._log_event(
                "info",
                "summary_navigated",
                "Summary navigation applied.",
                {"session_id": session["session_id"], "target_phase": target},
            )
            return self._payload(), 200

    def resume_capture(self) -> Tuple[JsonDict, int]:
        with self._lock:
            session = self._session
            phase = str((session or {}).get("status") or "idle").strip().lower()
            if session is None or phase != "capture_ready":
                return self._payload(error="invalid_summary_state", phase=phase), 400
            config_snapshot = _deepcopy_json(session.get("config_snapshot") or {})
            if not isinstance(config_snapshot, dict) or not config_snapshot:
                return self._payload(error="summary_config_snapshot_missing", phase=phase), 409
            execution_mode = self._session_mode_locked(session)
            try:
                app_cfg, _runtime_cfg = self._build_runtime_for_mode(config_snapshot, execution_mode)
                mic = self._make_mic(app_cfg)
            except Exception as exc:
                session["updated_at"] = _utc_now()
                self._persist_active_session()
                return self._payload(error="mic_unavailable", detail=str(exc), phase=phase), 400
            timeout_s = self._resolve_capture_timeout(app_cfg)
            try:
                self._build_stt_backend(app_cfg)
            except Exception as exc:
                session["updated_at"] = _utc_now()
                self._persist_active_session()
                return self._payload(error="stt_unavailable", detail=str(exc), phase=phase), 400

            self._launch_capture_runtime_locked(
                session,
                mic=mic,
                timeout_s=timeout_s,
                event_name="summary_capture_resumed",
                event_message="Summary capture resumed.",
            )
            return self._payload(), 200

    def generate(self) -> Tuple[JsonDict, int]:
        job: Optional[JsonDict] = None
        with self._lock:
            session = self._session
            phase = str((session or {}).get("status") or "idle").strip().lower()
            if session is None or phase not in {"transcript_review", "summary_review"}:
                return self._payload(error="invalid_summary_state", phase=phase), 400
            transcript_text = str(session.get("transcript_text") or "").strip()
            if not transcript_text:
                session["last_error"] = "summary: empty transcript"
                session["last_error_stage"] = "generate"
                session["updated_at"] = _utc_now()
                self._persist_active_session()
                return self._payload(error="summary_transcript_empty", phase=phase), 400
            session["status"] = "summarizing"
            session["last_error"] = None
            session["last_error_stage"] = None
            self._clear_recovery_locked(session, stage="llm")
            self._clear_toast_locked(session)
            session["updated_at"] = _utc_now()
            config_snapshot = _deepcopy_json(session.get("config_snapshot") or {})
            execution_mode = self._session_mode_locked(session)
            previous_draft = str(session.get("draft_text") or "").strip() or None
            llm_meta = self._llm_job_runtime_meta(config_snapshot=config_snapshot, execution_mode=execution_mode)
            job = {
                "job_id": str(uuid.uuid4()),
                "session_id": str(session.get("session_id") or ""),
                "kind": "generate",
                "origin_phase": phase,
                "config_snapshot": config_snapshot,
                "execution_mode": execution_mode,
                "transcript_text": transcript_text,
                "prompt_text": str(config_snapshot.get("system_prompt_text") or "").strip(),
                "prompt_meta": _deepcopy_json(config_snapshot.get("system_prompt_meta") or {}),
                "summary_instruction": str(config_snapshot.get("summary_instruction") or ""),
                "prompt_template": str(config_snapshot.get("input_prompt_template") or self._generate_user_template),
                "previous_draft": previous_draft,
                "llm_provider": str(llm_meta.get("provider") or ""),
                "llm_model": str(llm_meta.get("model") or ""),
                "llm_host": str(llm_meta.get("host") or ""),
            }
            self._active_llm_job = _deepcopy_json(job)
            self._persist_active_session()
            self._log_event(
                "info",
                "summary_generate_started",
                "Summary generation started.",
                {"session_id": session["session_id"], "job_id": job["job_id"], "llm_provider": job["llm_provider"], "llm_model": job["llm_model"]},
            )
            payload = self._payload()
        if job is not None:
            self._launch_llm_job(job)
        return payload, 200

    def repair(self, repair_prompt: Any) -> Tuple[JsonDict, int]:
        repair_text = str(repair_prompt or "").strip()
        job: Optional[JsonDict] = None
        with self._lock:
            session = self._session
            phase = str((session or {}).get("status") or "idle").strip().lower()
            if session is None or phase != "summary_review":
                return self._payload(error="invalid_summary_state", phase=phase), 400
            transcript_text = str(session.get("transcript_text") or "").strip()
            current_draft = str(session.get("draft_text") or "").strip()
            if not transcript_text or not current_draft:
                return self._payload(error="summary_draft_not_ready", phase=phase), 400
            if not repair_text:
                return self._payload(error="summary_repair_prompt_empty", phase=phase), 400
            session["status"] = "summarizing"
            session["last_repair_prompt"] = repair_text
            session["last_error"] = None
            session["last_error_stage"] = None
            self._clear_recovery_locked(session, stage="llm")
            self._clear_toast_locked(session)
            session["updated_at"] = _utc_now()
            config_snapshot = _deepcopy_json(session.get("config_snapshot") or {})
            execution_mode = self._session_mode_locked(session)
            llm_meta = self._llm_job_runtime_meta(config_snapshot=config_snapshot, execution_mode=execution_mode)
            job = {
                "job_id": str(uuid.uuid4()),
                "session_id": str(session.get("session_id") or ""),
                "kind": "repair",
                "origin_phase": phase,
                "config_snapshot": config_snapshot,
                "execution_mode": execution_mode,
                "transcript_text": transcript_text,
                "prompt_text": str(config_snapshot.get("system_prompt_text") or "").strip(),
                "prompt_meta": _deepcopy_json(config_snapshot.get("system_prompt_meta") or {}),
                "summary_instruction": str(config_snapshot.get("summary_instruction") or ""),
                "prompt_template": str(config_snapshot.get("input_prompt_template") or self._generate_user_template),
                "current_draft": current_draft,
                "repair_prompt": repair_text,
                "llm_provider": str(llm_meta.get("provider") or ""),
                "llm_model": str(llm_meta.get("model") or ""),
                "llm_host": str(llm_meta.get("host") or ""),
            }
            self._active_llm_job = _deepcopy_json(job)
            self._persist_active_session()
            self._log_event(
                "info",
                "summary_repair_started",
                "Summary repair started.",
                {"session_id": session["session_id"], "job_id": job["job_id"], "llm_provider": job["llm_provider"], "llm_model": job["llm_model"]},
            )
            payload = self._payload()
        if job is not None:
            self._launch_llm_job(job)
        return payload, 200

    def use_fallback_summary(self) -> Tuple[JsonDict, int]:
        with self._lock:
            session = self._session
            phase = str((session or {}).get("status") or "idle").strip().lower()
            if session is None or phase != "summary_review":
                return self._payload(error="invalid_summary_state", phase=phase), 400
            config_snapshot = _deepcopy_json(session.get("config_snapshot") or {})
            summary_cfg = config_snapshot.get("config") if isinstance(config_snapshot.get("config"), dict) else {}
            fallbacks_cfg = summary_cfg.get("fallbacks") if isinstance(summary_cfg.get("fallbacks"), dict) else {}
            fallback_text = str(fallbacks_cfg.get("fallback_summary") or "").strip()
            if not fallback_text:
                return self._payload(error="summary_fallback_missing", phase=phase), 400
            execution_mode = self._session_mode_locked(session)
            session["status"] = "publishing"
            session["updated_at"] = _utc_now()
            self._persist_active_session()
            self._publish_interrupt_requested = False

        attempt = 0
        try:
            while True:
                try:
                    self._publish_with_mode(config_snapshot=config_snapshot, execution_mode=execution_mode, draft_text=fallback_text)
                    break
                except Exception as exc:
                    detail = str(exc)
                    kind = self._classify_failure("tts", detail)
                    attempt += 1
                    if self._is_retryable_failure(kind) and attempt <= 1:
                        time.sleep(self._auto_retry_delay_s)
                        continue
                    raise RuntimeError(detail) from exc
        except Exception as exc:
            with self._lock:
                session = self._session
                if session is None:
                    return self._payload(error="summary_session_missing"), 409
                interrupted = bool(self._publish_interrupt_requested)
                self._publish_interrupt_requested = False
                if interrupted:
                    session["status"] = "summary_review"
                    session["last_error"] = None
                    session["last_error_stage"] = None
                    session["updated_at"] = _utc_now()
                    self._persist_active_session()
                    payload = self._payload()
                    payload["publish_interrupted"] = True
                    return payload, 200
                session["status"] = "summary_review"
                session["last_error"] = "tts: " + str(exc)
                session["last_error_stage"] = "publish_fallback"
                self._set_recovery_locked(
                    session,
                    stage="tts",
                    kind=self._classify_failure("tts", str(exc)),
                    message=str(exc),
                    blocking=False,
                    auto_retry_used=attempt > 1,
                )
                session["updated_at"] = _utc_now()
                self._persist_active_session()
                self._log_event(
                    "error",
                    "summary_fallback_publish_failed",
                    "Fallback summary publish failed.",
                    {"session_id": session["session_id"], "detail": str(exc)},
                )
                return self._payload(error="summary_fallback_publish_failed", detail=str(exc)), 502

        with self._lock:
            session = self._session
            if session is None:
                return self._payload(error="summary_session_missing"), 409
            interrupted = bool(self._publish_interrupt_requested)
            self._publish_interrupt_requested = False
            if interrupted:
                session["status"] = "summary_review"
                session["last_error"] = None
                session["last_error_stage"] = None
                session["updated_at"] = _utc_now()
                self._persist_active_session()
                payload = self._payload()
                payload["publish_interrupted"] = True
                return payload, 200
            session["status"] = "completed"
            session["published_text"] = fallback_text
            session["published_at"] = _utc_now()
            session["last_error"] = None
            session["last_error_stage"] = None
            self._clear_recovery_locked(session, stage="llm")
            self._clear_recovery_locked(session, stage="tts")
            session["updated_at"] = _utc_now()
            self._persist_active_session()
            self._log_event(
                "info",
                "summary_fallback_published",
                "Fallback summary published.",
                {"session_id": session["session_id"]},
            )
            return self._payload(), 200

    def publish(self, *, allow_stale: bool = False) -> Tuple[JsonDict, int]:
        with self._lock:
            session = self._session
            phase = str((session or {}).get("status") or "idle").strip().lower()
            if session is None:
                return self._payload(error="summary_session_missing", phase=phase), 404
            if phase != "summary_review":
                return self._payload(error="invalid_summary_state", phase=phase), 409
            draft_text = str(session.get("draft_text") or "").strip()
            if not draft_text:
                return self._payload(error="summary_draft_not_ready", phase=phase), 400
            if bool(session.get("draft_stale")) and not allow_stale:
                return self._payload(
                    error="summary_draft_stale",
                    detail=str(session.get("draft_stale_reason") or "Transcript gewijzigd sinds de laatste samenvatting."),
                    phase=phase,
                ), 409
            self._publish_interrupt_requested = False
            session["status"] = "publishing"
            session["updated_at"] = _utc_now()
            config_snapshot = _deepcopy_json(session.get("config_snapshot") or {})
            execution_mode = self._session_mode_locked(session)
            self._persist_active_session()

        attempt = 0
        try:
            while True:
                try:
                    self._publish_with_mode(config_snapshot=config_snapshot, execution_mode=execution_mode, draft_text=draft_text)
                    break
                except Exception as exc:
                    detail = str(exc)
                    kind = self._classify_failure("tts", detail)
                    attempt += 1
                    if self._is_retryable_failure(kind) and attempt <= 1:
                        time.sleep(self._auto_retry_delay_s)
                        continue
                    raise RuntimeError(detail) from exc
        except Exception as exc:
            with self._lock:
                session = self._session
                if session is None:
                    return self._payload(error="summary_session_missing"), 409
                interrupted = bool(self._publish_interrupt_requested)
                self._publish_interrupt_requested = False
                if interrupted:
                    session["status"] = "summary_review"
                    session["last_error"] = None
                    session["last_error_stage"] = None
                    session["updated_at"] = _utc_now()
                    self._persist_active_session()
                    self._log_event(
                        "info",
                        "summary_publish_interrupted",
                        "Summary publish interrupted.",
                        {"session_id": session["session_id"]},
                    )
                    payload = self._payload()
                    payload["publish_interrupted"] = True
                    return payload, 200
                session["status"] = "summary_review"
                session["last_error"] = "tts: " + str(exc)
                session["last_error_stage"] = "publish"
                self._set_recovery_locked(
                    session,
                    stage="tts",
                    kind=self._classify_failure("tts", str(exc)),
                    message=str(exc),
                    blocking=False,
                    auto_retry_used=attempt > 1,
                )
                session["updated_at"] = _utc_now()
                self._persist_active_session()
                self._log_event(
                    "error",
                    "summary_publish_failed",
                    "Summary publish failed.",
                    {"session_id": session["session_id"], "detail": str(exc)},
                )
                return self._payload(error="summary_publish_failed", detail=str(exc)), 502

        with self._lock:
            session = self._session
            if session is None:
                return self._payload(error="summary_session_missing"), 409
            interrupted = bool(self._publish_interrupt_requested)
            self._publish_interrupt_requested = False
            if interrupted:
                session["status"] = "summary_review"
                session["last_error"] = None
                session["last_error_stage"] = None
                session["updated_at"] = _utc_now()
                self._persist_active_session()
                self._log_event(
                    "info",
                    "summary_publish_interrupted",
                    "Summary publish interrupted.",
                    {"session_id": session["session_id"]},
                )
                payload = self._payload()
                payload["publish_interrupted"] = True
                return payload, 200
            session["status"] = "completed"
            session["published_text"] = draft_text
            session["published_at"] = _utc_now()
            session["last_error"] = None
            session["last_error_stage"] = None
            self._clear_recovery_locked(session, stage="tts")
            session["updated_at"] = _utc_now()
            self._persist_active_session()
            self._log_event("info", "summary_published", "Summary published.", {"session_id": session["session_id"]})
            return self._payload(), 200

    def complete_without_publish(self) -> Tuple[JsonDict, int]:
        with self._lock:
            session = self._session
            phase = str((session or {}).get("status") or "idle").strip().lower()
            if session is None:
                return self._payload(error="summary_session_missing", phase=phase), 404
            if phase != "summary_review":
                return self._payload(error="invalid_summary_state", phase=phase), 409
            draft_text = str(session.get("draft_text") or "").strip()
            if not draft_text:
                return self._payload(error="summary_draft_not_ready", phase=phase), 400
            session["status"] = "completed"
            session["published_text"] = None
            session["published_at"] = None
            session["last_error"] = None
            session["last_error_stage"] = None
            self._clear_recovery_locked(session, stage="tts")
            session["updated_at"] = _utc_now()
            self._persist_active_session()
            return self._payload(), 200

    def stop_output(self) -> Tuple[JsonDict, int]:
        with self._lock:
            session = self._session
            if session is None:
                return self._payload(error="summary_session_missing"), 404
            phase = str((session or {}).get("status") or "idle").strip().lower()
            if phase not in {"publishing", "completed"}:
                return self._payload(error="invalid_summary_state", phase=phase), 409
            self._publish_interrupt_requested = True
            config_snapshot = _deepcopy_json(session.get("config_snapshot") or {})
            execution_mode = self._session_mode_locked(session)
            session_id = str(session.get("session_id") or "")
        _app_cfg, runtime_cfg = self._build_runtime_for_mode(config_snapshot, execution_mode)
        try:
            stop_meta = _deepcopy_json(self._stop_output(runtime_cfg))
        except Exception as exc:
            return self._payload(error="summary_output_stop_failed", detail=str(exc), phase=phase), 502
        with self._lock:
            session = self._session
            if session is not None:
                session["updated_at"] = _utc_now()
                self._persist_active_session()
            self._log_event(
                "info",
                "summary_output_stop_requested",
                "Summary output stop requested.",
                {"session_id": session_id, "phase": phase, "stop_meta": stop_meta},
            )
            payload = self._payload()
            payload["stop_output"] = stop_meta
            return payload, 200

    def abort(self) -> Tuple[JsonDict, int]:
        self._stop_capture_runtime()
        llm_job_to_stop: Optional[JsonDict] = None
        with self._lock:
            session = self._session
            if session is None:
                session = self._new_error_session("abort: missing session")
                self._session = session
            active_job = self._active_llm_job if isinstance(self._active_llm_job, dict) else None
            if isinstance(active_job, dict):
                llm_job_to_stop = _deepcopy_json(active_job)
                active_job["cancel_requested"] = True
                self._active_llm_job = None
            self._publish_interrupt_requested = False
            session["status"] = "aborted"
            session["last_error"] = None
            session["last_error_stage"] = None
            session["recovery"] = None
            session["updated_at"] = _utc_now()
            self._clear_spool_dir(session.get("session_id"))
            self._refresh_spool_stats_locked(session)
            self._persist_active_session()
            self._log_event("info", "summary_aborted", "Summary session aborted.", {"session_id": session["session_id"]})
            payload = self._payload()
        if isinstance(llm_job_to_stop, dict) and str(llm_job_to_stop.get("llm_provider") or "").strip().lower() == "ollama_local":
            self._launch_best_effort_local_llm_stop(llm_job_to_stop)
        return payload, 200

    def clear_recent(self) -> Tuple[JsonDict, int]:
        self._stop_capture_runtime()
        with self._lock:
            session = self._session
            if session is None:
                payload = self._payload()
                payload["session"] = None
                return payload, 200
            status = str(session.get("status") or "").strip().lower()
            if status not in {"completed", "aborted", "error"}:
                return self._payload(error="invalid_summary_state", detail=f"clear not allowed from status {status or 'unknown'}"), 409
            session_id = session.get("session_id")
            self._clear_spool_dir(session_id)
            self._session = None
            self._active_llm_job = None
            active_session_path = self._active_session_path()
            if active_session_path:
                try:
                    os.remove(active_session_path)
                except FileNotFoundError:
                    pass
            self._log_event("info", "summary_cleared", "Recent summary cleared.", {"session_id": session_id})
            payload = self._payload()
            payload["session"] = None
            return payload, 200

    def save(self) -> Tuple[JsonDict, int]:
        with self._lock:
            session = self._session
            if session is None:
                return self._payload(error="summary_session_missing"), 404
            if not self.state_root:
                return self._payload(error="summary_persistence_disabled"), 400
            session_id = str(session["session_id"])
            saved_path = self._saved_json_path(session_id)
            text_path = self._saved_text_path(session_id)
            archived = _deepcopy_json(session)
            archived["saved_at"] = _utc_now()
            self._write_json(saved_path, archived)
            text_content = str(session.get("draft_text") or session.get("published_text") or session.get("transcript_text") or "").strip()
            self._write_text(text_path, text_content)
            filename = "summary.txt"
            session["save_meta"] = {
                "saved_at": archived["saved_at"],
                "path": saved_path,
                "archive_path": saved_path,
                "text_path": text_path,
                "download_url": f"/api/summary/save/{session_id}.txt",
                "filename": filename,
            }
            session["updated_at"] = _utc_now()
            self._persist_active_session()
            self._log_event("info", "summary_saved", "Summary session archived.", {"session_id": session["session_id"]})
            payload = self._payload()
            payload["download"] = _deepcopy_json(session.get("save_meta"))
            return payload, 200

    def saved_text_path(self, session_id: Any) -> Optional[str]:
        cleaned = str(session_id or "").strip()
        if not cleaned or not self.state_root:
            return None
        path = self._saved_text_path(cleaned)
        return path if os.path.isfile(path) else None
