from __future__ import annotations

import copy
import json
import os
import queue
import threading
import time
import uuid
from typing import Any, Callable, Dict, List, Optional, Tuple


JsonDict = Dict[str, Any]


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
        connectivity_snapshot: Callable[[], List[JsonDict]],
        log_event: Callable[[str, str, str, Optional[JsonDict]], None],
        capture_timeout_default_s: float,
        capture_timeout_max_s: float,
        stt_queue_max_items: int,
        worker_join_timeout_s: float = 2.0,
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
        self._connectivity_snapshot = connectivity_snapshot
        self._log_event = log_event
        self._capture_timeout_default_s = float(capture_timeout_default_s)
        self._capture_timeout_max_s = float(capture_timeout_max_s)
        self._stt_queue_max_items = int(stt_queue_max_items)
        self._worker_join_timeout_s = float(worker_join_timeout_s)
        self._generate_user_template = str(generate_user_template or "Transcript:\n{transcript}")
        self._lock = threading.RLock()
        self._config = self._normalize_config(_deepcopy_json(default_config))
        self._session: Optional[JsonDict] = None
        self._capture_thread: Optional[threading.Thread] = None
        self._stt_thread: Optional[threading.Thread] = None
        self._stop_event: Optional[threading.Event] = None
        self._audio_queue: Optional["queue.Queue[Any]"] = None
        self._publish_interrupt_requested = False
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

    def _new_error_session(self, detail: str) -> JsonDict:
        return {
            "session_id": str(uuid.uuid4()),
            "status": "error",
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
            "capture_stats": {
                "loops": 0,
                "timeouts": 0,
                "audio_chunks": 0,
                "dropped_audio_chunks": 0,
                "stt_calls": 0,
                "transcript_count": 0,
                "stt_queue_max": self._stt_queue_max_items,
                "stt_queue_size": 0,
                "started_at": None,
                "last_audio_at": None,
                "last_transcript_at": None,
            },
        }

    def _new_session(self, *, config_snapshot: JsonDict, started_at: float) -> JsonDict:
        return {
            "session_id": str(uuid.uuid4()),
            "status": "capturing",
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
            "capture_stats": {
                "loops": 0,
                "timeouts": 0,
                "audio_chunks": 0,
                "dropped_audio_chunks": 0,
                "stt_calls": 0,
                "transcript_count": 0,
                "stt_queue_max": self._stt_queue_max_items,
                "stt_queue_size": 0,
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
        tmp_path = path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        os.replace(tmp_path, path)

    def _write_text(self, path: str, content: str) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp_path = path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as fh:
            fh.write(str(content or ""))
        os.replace(tmp_path, path)

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
        stats = session.get("capture_stats")
        if not isinstance(stats, dict):
            stats = {}
        stats["stt_queue_max"] = self._stt_queue_max_items
        stats["stt_queue_size"] = 0
        stats["transcript_count"] = len(normalized_lines)
        session["capture_stats"] = stats
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
            self._persist_active_session()

    def _stop_capture_runtime(self) -> bool:
        with self._lock:
            stop_event = self._stop_event
            capture_thread = self._capture_thread
            stt_thread = self._stt_thread
        if stop_event is not None:
            try:
                stop_event.set()
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
                self._audio_queue = None
                session = self._session
                if session is not None:
                    stats = session.get("capture_stats")
                    if isinstance(stats, dict):
                        stats["stt_queue_size"] = 0
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
                app_cfg, runtime_cfg = self._build_app_config(config_snapshot.get("config") if isinstance(config_snapshot.get("config"), dict) else {})
                mic = self._make_mic(app_cfg)
            except Exception as exc:
                session = self._new_error_session(f"start: {exc}")
                self._session = session
                self._persist_active_session()
                self._log_event("error", "summary_start_failed", "Summary start failed.", {"detail": str(exc)})
                return self._payload(error="mic_unavailable", detail=str(exc), session=session), 400

            timeout_s = self._resolve_capture_timeout(app_cfg)
            stop_event = threading.Event()
            audio_queue: "queue.Queue[Any]" = queue.Queue(maxsize=self._stt_queue_max_items)
            started_at = time.time()
            session = self._new_session(config_snapshot=config_snapshot, started_at=started_at)
            self._session = session
            self._stop_event = stop_event
            self._audio_queue = audio_queue

            try:
                stt_backend = self._build_stt_backend(app_cfg)
            except Exception as exc:
                session["status"] = "error"
                session["last_error"] = "stt_init: " + str(exc)
                session["last_error_stage"] = "start"
                session["updated_at"] = _utc_now()
                self._persist_active_session()
                self._log_event("error", "summary_start_failed", "Summary STT initialization failed.", {"detail": str(exc)})
                return self._payload(error="stt_unavailable", detail=str(exc)), 400

            def _update_queue_size() -> None:
                try:
                    qsize = int(audio_queue.qsize())
                except Exception:
                    qsize = 0
                with self._lock:
                    current_session = self._session
                    if current_session is None:
                        return
                    stats = current_session.get("capture_stats")
                    if isinstance(stats, dict):
                        stats["stt_queue_size"] = max(0, qsize)
                        current_session["updated_at"] = _utc_now()
                        self._persist_active_session()

            def _record_timeout() -> None:
                with self._lock:
                    current_session = self._session
                    if current_session is None:
                        return
                    stats = current_session.get("capture_stats")
                    if isinstance(stats, dict):
                        stats["timeouts"] = int(stats.get("timeouts") or 0) + 1
                        current_session["updated_at"] = _utc_now()
                        self._persist_active_session()

            def _enqueue_audio(audio: Any) -> None:
                with self._lock:
                    current_session = self._session
                    if current_session is not None:
                        stats = current_session.get("capture_stats")
                        if isinstance(stats, dict):
                            stats["audio_chunks"] = int(stats.get("audio_chunks") or 0) + 1
                            stats["last_audio_at"] = time.time()
                            current_session["updated_at"] = _utc_now()
                            self._persist_active_session()

                while True:
                    try:
                        audio_queue.put_nowait(audio)
                        _update_queue_size()
                        return
                    except queue.Full:
                        dropped = False
                        try:
                            audio_queue.get_nowait()
                            dropped = True
                        except queue.Empty:
                            pass
                        if dropped:
                            with self._lock:
                                current_session = self._session
                                if current_session is not None:
                                    stats = current_session.get("capture_stats")
                                    if isinstance(stats, dict):
                                        stats["dropped_audio_chunks"] = int(stats.get("dropped_audio_chunks") or 0) + 1
                                        current_session["updated_at"] = _utc_now()
                                        self._persist_active_session()
                            continue
                        return

            def _capture_worker() -> None:
                try:
                    stream_utterances = getattr(mic, "stream_utterances", None)
                    if callable(stream_utterances):
                        with self._lock:
                            current_session = self._session
                            if current_session is not None:
                                stats = current_session.get("capture_stats")
                                if isinstance(stats, dict):
                                    stats["loops"] = int(stats.get("loops") or 0) + 1
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
                            stats = current_session.get("capture_stats")
                            if isinstance(stats, dict):
                                stats["loops"] = int(stats.get("loops") or 0) + 1
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
                        if stop_event.is_set() and audio_queue.empty():
                            break
                        try:
                            queued_audio = audio_queue.get(timeout=0.1)
                        except queue.Empty:
                            continue
                        _update_queue_size()
                        try:
                            with self._lock:
                                current_session = self._session
                                if current_session is not None:
                                    stats = current_session.get("capture_stats")
                                    if isinstance(stats, dict):
                                        stats["stt_calls"] = int(stats.get("stt_calls") or 0) + 1
                                        current_session["updated_at"] = _utc_now()
                                        self._persist_active_session()
                            stt_result = self._transcribe_audio(queued_audio, stt_backend, runtime_cfg)
                            text = str(getattr(stt_result, "text", "") or "").strip()
                        except Exception as exc:
                            with self._lock:
                                current_session = self._session
                                if current_session is not None:
                                    current_session["last_error"] = "stt: " + str(exc)
                                    current_session["last_error_stage"] = "capture"
                                    current_session["updated_at"] = _utc_now()
                                    self._persist_active_session()
                            continue
                        if text:
                            with self._lock:
                                current_session = self._session
                                if current_session is None:
                                    continue
                                lines = current_session.get("transcript_lines")
                                if not isinstance(lines, list):
                                    lines = []
                                    current_session["transcript_lines"] = lines
                                lines.append(
                                    {
                                        "id": str(uuid.uuid4()),
                                        "text": text,
                                        "source": "stt",
                                        "created_at": _utc_now(),
                                    }
                                )
                                current_session["transcript_text"] = _transcript_text_from_lines(lines)
                                stats = current_session.get("capture_stats")
                                if isinstance(stats, dict):
                                    stats["transcript_count"] = len(lines)
                                    stats["last_transcript_at"] = time.time()
                                current_session["updated_at"] = _utc_now()
                                self._persist_active_session()
                finally:
                    with self._lock:
                        if self._stt_thread is threading.current_thread():
                            self._stt_thread = None
                    _update_queue_size()

            capture_thread = threading.Thread(target=_capture_worker, daemon=True)
            stt_thread = threading.Thread(target=_stt_worker, daemon=True)
            self._capture_thread = capture_thread
            self._stt_thread = stt_thread
            self._persist_active_session()
            self._log_event(
                "info",
                "summary_started",
                "Summary session started.",
                {"session_id": session["session_id"], "status": session["status"]},
            )

        stt_thread.start()
        capture_thread.start()
        return self.get_payload(), 200

    def stop(self) -> Tuple[JsonDict, int]:
        with self._lock:
            session = self._session
            phase = str((session or {}).get("status") or "idle").strip().lower()
        if session is None or phase != "capturing":
            return self._payload(error="invalid_summary_state", phase=phase), 400
        if not self._stop_capture_runtime():
            return self._payload(error="summary_capture_stop_timeout", phase=phase), 409
        with self._lock:
            session = self._session
            if session is None:
                return self._payload(error="summary_session_missing"), 409
            session["status"] = "transcript_review"
            session["updated_at"] = _utc_now()
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
            session["transcript_lines"] = lines
            session["transcript_text"] = _transcript_text_from_lines(lines)
            session["updated_at"] = _utc_now()
            stats = session.get("capture_stats")
            if isinstance(stats, dict):
                stats["transcript_count"] = len(lines)
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
            session["updated_at"] = _utc_now()
            self._persist_active_session()
            self._log_event(
                "info",
                "summary_draft_updated",
                "Summary draft updated.",
                {"session_id": session["session_id"]},
            )
            return self._payload(), 200

    def generate(self) -> Tuple[JsonDict, int]:
        with self._lock:
            session = self._session
            phase = str((session or {}).get("status") or "idle").strip().lower()
            if session is None or phase != "transcript_review":
                return self._payload(error="invalid_summary_state", phase=phase), 400
            transcript_text = str(session.get("transcript_text") or "").strip()
            if not transcript_text:
                session["last_error"] = "summary: empty transcript"
                session["last_error_stage"] = "generate"
                session["updated_at"] = _utc_now()
                self._persist_active_session()
                return self._payload(error="summary_transcript_empty", phase=phase), 400
            session["status"] = "summarizing"
            session["updated_at"] = _utc_now()
            config_snapshot = _deepcopy_json(session.get("config_snapshot") or {})
            self._persist_active_session()

        prompt_text = str(config_snapshot.get("system_prompt_text") or "").strip()
        prompt_meta = config_snapshot.get("system_prompt_meta") if isinstance(config_snapshot.get("system_prompt_meta"), dict) else {}
        summary_cfg = config_snapshot.get("config") if isinstance(config_snapshot.get("config"), dict) else {}
        app_cfg, runtime_cfg = self._build_app_config(summary_cfg)
        pipeline = self._build_pipeline(app_cfg)
        prompt_template = str(config_snapshot.get("input_prompt_template") or self._generate_user_template)
        summary_instruction = str(config_snapshot.get("summary_instruction") or "")
        user_prompt = prompt_template.replace("{transcript}", transcript_text).replace("{instruction}", summary_instruction)
        messages: List[Dict[str, str]] = []
        if prompt_text:
            messages.append({"role": "system", "content": prompt_text})
        messages.append({"role": "user", "content": user_prompt})
        try:
            draft_text = str(self._generate_reply(pipeline, runtime_cfg, messages) or "").strip()
            if not draft_text:
                raise RuntimeError("empty reply")
        except Exception as exc:
            with self._lock:
                session = self._session
                if session is None:
                    return self._payload(error="summary_session_missing"), 409
                session["status"] = "transcript_review"
                session["last_error"] = "llm: " + str(exc)
                session["last_error_stage"] = "generate"
                session["updated_at"] = _utc_now()
                self._persist_active_session()
                self._log_event(
                    "error",
                    "summary_generate_failed",
                    "Summary generation failed.",
                    {"session_id": session["session_id"], "detail": str(exc)},
                )
                return self._payload(error="summary_generation_failed", detail=str(exc)), 502

        with self._lock:
            session = self._session
            if session is None:
                return self._payload(error="summary_session_missing"), 409
            session["status"] = "summary_review"
            session["draft_text"] = draft_text
            session["draft_meta"] = {
                "generated_at": _utc_now(),
                "system_prompt_source": prompt_meta.get("source"),
                "system_prompt_name": prompt_meta.get("name"),
            }
            session["last_error"] = None
            session["last_error_stage"] = None
            session["updated_at"] = _utc_now()
            self._persist_active_session()
            self._log_event("info", "summary_generated", "Summary draft generated.", {"session_id": session["session_id"]})
            return self._payload(), 200

    def publish(self) -> Tuple[JsonDict, int]:
        with self._lock:
            session = self._session
            phase = str((session or {}).get("status") or "idle").strip().lower()
            draft_text = str((session or {}).get("draft_text") or "").strip()
            if session is None or phase != "summary_review" or not draft_text:
                return self._payload(error="summary_draft_not_ready", phase=phase), 400
            self._publish_interrupt_requested = False
            session["status"] = "publishing"
            session["updated_at"] = _utc_now()
            config_snapshot = _deepcopy_json(session.get("config_snapshot") or {})
            self._persist_active_session()

        summary_cfg = config_snapshot.get("config") if isinstance(config_snapshot.get("config"), dict) else {}
        app_cfg, runtime_cfg = self._build_app_config(summary_cfg)
        pipeline = self._build_pipeline(app_cfg)
        try:
            self._publish_text(pipeline, runtime_cfg, draft_text)
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
            session["updated_at"] = _utc_now()
            self._persist_active_session()
            self._log_event("info", "summary_published", "Summary published.", {"session_id": session["session_id"]})
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
            session_id = str(session.get("session_id") or "")
        summary_cfg = config_snapshot.get("config") if isinstance(config_snapshot.get("config"), dict) else {}
        _app_cfg, runtime_cfg = self._build_app_config(summary_cfg)
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
        with self._lock:
            session = self._session
            if session is None:
                session = self._new_error_session("abort: missing session")
                self._session = session
            self._publish_interrupt_requested = False
            session["status"] = "aborted"
            session["last_error"] = None
            session["last_error_stage"] = None
            session["updated_at"] = _utc_now()
            self._persist_active_session()
            self._log_event("info", "summary_aborted", "Summary session aborted.", {"session_id": session["session_id"]})
            return self._payload(), 200

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
            self._session = None
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
