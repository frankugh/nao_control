from __future__ import annotations

import argparse
import copy
import json
import queue
import shutil
import sys
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple
from urllib.parse import urlparse


DM_ROOT = Path(__file__).resolve().parent
REPO_ROOT = DM_ROOT.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import webapp_server
from dialog.interfaces import UtteranceAudio
from py3_script_runner.client import DMClientError
from py3_script_runner.runner import RunResult, ScriptRunner


PROFILE_OFFLINE = "offline"
PROFILE_LIVE_SERVICES = "live_services"
PROFILE_LIVE_ROBOT = "live_robot"
PROFILES = (PROFILE_OFFLINE, PROFILE_LIVE_SERVICES, PROFILE_LIVE_ROBOT)

SCENARIO_HAPPY_PATH = "happy_path_dialog"
SCENARIO_SUMMARY_EDIT = "summary_edit_flow"
SCENARIO_SERVICE_LOSS = "service_loss_recovery"
SCENARIO_FULL_REHEARSAL = "full_demo_rehearsal"
SCENARIOS = (
    SCENARIO_HAPPY_PATH,
    SCENARIO_SUMMARY_EDIT,
    SCENARIO_SERVICE_LOSS,
    SCENARIO_FULL_REHEARSAL,
)
SCENARIO_OPERATOR_NAME = {
    SCENARIO_HAPPY_PATH: "chat",
    SCENARIO_SUMMARY_EDIT: "summary",
    SCENARIO_SERVICE_LOSS: "fallbacks",
    SCENARIO_FULL_REHEARSAL: "rehearsal",
}

DEFAULT_RUNTIME_PRESET_BY_PROFILE = {
    PROFILE_OFFLINE: DM_ROOT / "configs" / "runtime" / "runtime_virtuele_robot.json",
    PROFILE_LIVE_SERVICES: DM_ROOT / "configs" / "runtime" / "runtime_virtuele_robot.json",
    PROFILE_LIVE_ROBOT: DM_ROOT / "configs" / "runtime" / "runtime_alex.json",
}

DEFAULT_SUMMARY_SCRIPT = REPO_ROOT / "py3_script_runner" / "scripts" / "demo_gate_summary_single_robot.json"
DEFAULT_WORKSHOP_SCRIPT = REPO_ROOT / "py3_script_runner" / "scripts" / "demo_gate_workshop_single_robot.json"
DEFAULT_SUMMARY_PRESETS_PATH = DM_ROOT / "configs" / "summary_presets.json"
DEFAULT_DM_CONFIG_PATH = DM_ROOT / "configs" / "default.json"
DEFAULT_AUDIO_FIXTURES_ROOT = DM_ROOT / "demo_gate_audio"

FIXTURE_DIALOG_SEQUENCE = (
    ("hoi", "Hoi"),
    ("sta_op", "sta op"),
    ("loop_met_me_mee", "loop met me mee"),
    ("gezellig_zo", "gezellig zo"),
    ("stop_maar", "stop maar"),
    ("motoren_uit", "motoren uit"),
)
FIXTURE_SUMMARY_SEQUENCE = (
    ("zin_een", "zin een"),
    ("zin_twee", "zin twee"),
    ("zin_drie", "zin drie"),
    ("zin_vier", "zin vier"),
)

LIVE_ROBOT_SETTLE_STAND_UP_S = 4.0
LIVE_ROBOT_SETTLE_WALK_WITH_ME_S = 5.0
LIVE_ROBOT_SETTLE_STOP_S = 4.0
LIVE_ROBOT_SETTLE_REST_S = 4.0
SUMMARY_GENERATE_TIMEOUT_ECHO_S = 3.0
SUMMARY_GENERATE_TIMEOUT_LOCAL_LLM_S = 180.0
SUMMARY_GENERATE_TIMEOUT_CLOUD_S = 45.0

SUMMARY_TRANSCRIPT_VARIANTS = {
    "zin_een": ("zin een", "zin in"),
    "zin_twee": ("zin twee",),
    "zin_drie": ("zin drie",),
    "zin_vier": ("zin vier",),
}


class DemoGateError(RuntimeError):
    """Base error for the demo-gate harness."""


class ScenarioFailure(DemoGateError):
    """Raised when a scenario assertion fails."""


def _load_json(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _make_local_paths_absolute(cfg: Dict[str, Any]) -> Dict[str, Any]:
    input_cfg = cfg.get("input") if isinstance(cfg.get("input"), dict) else {}
    stt_cfg = input_cfg.get("stt") if isinstance(input_cfg.get("stt"), dict) else {}
    params = stt_cfg.get("params") if isinstance(stt_cfg.get("params"), dict) else {}
    input_params = input_cfg.get("params") if isinstance(input_cfg.get("params"), dict) else {}
    run_cfg = cfg.get("run") if isinstance(cfg.get("run"), dict) else {}
    model_path = str(params.get("model_path") or "").strip()
    if model_path and not Path(model_path).is_absolute():
        params["model_path"] = str((DM_ROOT / model_path).resolve())
    if str(stt_cfg.get("type") or "").strip().lower() == "vosk":
        params["sample_rate"] = None
    input_params["status_to_console"] = False
    input_params["print_input"] = False
    run_cfg["status_to_console"] = False
    cfg["debug_cmdrec"] = False
    return cfg


def _norm_url(raw_url: str) -> str:
    return str(raw_url or "").strip().rstrip("/")


def _url_port(raw_url: str) -> str:
    try:
        parsed = urlparse(str(raw_url or ""))
    except Exception:
        parsed = None
    if parsed and parsed.port is not None:
        return str(parsed.port)
    cleaned = _norm_url(raw_url)
    if ":" in cleaned:
        return cleaned.rsplit(":", 1)[-1]
    return "default"


def _instance_id_for_url(raw_url: str) -> str:
    return f"demo_gate_{_url_port(raw_url)}"


def _cfg_instance_id(cfg: Dict[str, Any]) -> str:
    run_cfg = cfg.get("run") if isinstance(cfg.get("run"), dict) else {}
    return str(run_cfg.get("instance_id") or "").strip() or "default"


def _is_live_robot_profile(profile: str) -> bool:
    return str(profile or "").strip().lower() == PROFILE_LIVE_ROBOT


def _wait_until(
    predicate: Callable[[], bool],
    *,
    timeout_s: float,
    interval_s: float = 0.05,
    description: str,
) -> None:
    deadline = time.time() + float(timeout_s)
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(interval_s)
    raise ScenarioFailure(f"Timed out while waiting for {description}.")


def _assistant_reply_from_growth(before_len: int, history: Sequence[Dict[str, Any]]) -> str:
    if len(history) < before_len + 2:
        raise ScenarioFailure("Expected two new history items after queued utterance.")
    added = list(history[before_len:])
    user_item = added[-2]
    assistant_item = added[-1]
    if str(user_item.get("role") or "") != "user":
        raise ScenarioFailure(f"Expected penultimate history item to be user, got {user_item!r}")
    if str(assistant_item.get("role") or "") != "assistant":
        raise ScenarioFailure(f"Expected latest history item to be assistant, got {assistant_item!r}")
    return str(assistant_item.get("content") or "")


def _normalize_command_value(value: Any) -> str:
    return str(value or "").replace("\\_", "_").replace("\\", "").strip()


def _fixture_text(fixture_name: str) -> str:
    key = str(fixture_name or "").strip()
    for name, text in FIXTURE_DIALOG_SEQUENCE + FIXTURE_SUMMARY_SEQUENCE:
        if name == key:
            return str(text)
    return key.replace("_", " ").strip()


def _latest_transition_for_command(events: Sequence[Dict[str, Any]], command: str) -> Dict[str, Any]:
    command_norm = _normalize_command_value(command).upper()
    matches = [
        event
        for event in events
        if _normalize_command_value((event.get("data") or {}).get("command")).upper() == command_norm
    ]
    if not matches:
        raise ScenarioFailure(f"No state_transition found for command={command_norm}.")
    return matches[-1]


@dataclass(frozen=True)
class ReplayClip:
    name: str
    wav_bytes: bytes
    delay_before_s: float = 0.0

    def as_audio(self) -> UtteranceAudio:
        return UtteranceAudio(
            pcm=bytes(self.wav_bytes),
            sample_rate=16000,
            channels=1,
            sample_width=2,
        )


class ReplayMic:
    """Queue-driven mic backend for deterministic utterance replay."""

    def __init__(self) -> None:
        self._queue: "queue.Queue[ReplayClip]" = queue.Queue()

    def clear(self) -> None:
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                return

    def queue_clip(self, clip: ReplayClip) -> None:
        self._queue.put(clip)

    def queue_fixture(self, *, name: str, wav_bytes: bytes, delay_before_s: float = 0.0) -> None:
        self.queue_clip(ReplayClip(name=str(name), wav_bytes=bytes(wav_bytes), delay_before_s=float(delay_before_s)))

    @staticmethod
    def _sleep_interruptible(stop_event: Optional[threading.Event], delay_s: float) -> bool:
        remaining = max(0.0, float(delay_s))
        while remaining > 0:
            if stop_event is not None and stop_event.is_set():
                return False
            chunk = min(0.05, remaining)
            time.sleep(chunk)
            remaining -= chunk
        return True

    def _dequeue(self, *, timeout_s: float) -> ReplayClip:
        timeout = float(timeout_s)
        if timeout <= 0:
            timeout = 0.01
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                return self._queue.get(timeout=min(0.25, remaining))
            except queue.Empty:
                continue
        raise TimeoutError("no replay utterance queued")

    def capture_utterance(self, timeout_s: float = 10.0) -> UtteranceAudio:
        clip = self._dequeue(timeout_s=float(timeout_s))
        self._sleep_interruptible(None, clip.delay_before_s)
        return clip.as_audio()

    def stream_utterances(
        self,
        stop_event: threading.Event,
        *,
        timeout_s: float,
        on_utterance: Callable[[UtteranceAudio], None],
        on_timeout: Optional[Callable[[], None]] = None,
    ) -> None:
        timeout = max(0.05, min(float(timeout_s or 0.1), 0.25))
        while not stop_event.is_set():
            try:
                clip = self._dequeue(timeout_s=timeout)
            except TimeoutError:
                if callable(on_timeout):
                    on_timeout()
                continue
            if not self._sleep_interruptible(stop_event, clip.delay_before_s):
                return
            if stop_event.is_set():
                return
            on_utterance(clip.as_audio())


class ReplayMicRegistry:
    """Registry keyed by DM instance id and capture mode."""

    def __init__(self, fixtures_root: Path) -> None:
        self.fixtures_root = Path(fixtures_root)
        self._fixtures: Dict[str, bytes] = {}
        self._mics: Dict[Tuple[str, str], ReplayMic] = {}
        self._lock = threading.RLock()

    def _load_fixture_bytes(self, fixture_name: str) -> bytes:
        key = str(fixture_name or "").strip()
        if not key:
            raise ValueError("fixture_name is required")
        with self._lock:
            cached = self._fixtures.get(key)
        if cached is not None:
            return cached
        path = self.fixtures_root / f"{key}.wav"
        if not path.exists():
            raise FileNotFoundError(f"Replay fixture not found: {path}")
        data = path.read_bytes()
        with self._lock:
            self._fixtures[key] = data
        return data

    def mic_for(self, instance_id: str, *, mode: str) -> ReplayMic:
        key = (str(instance_id or "").strip() or "default", str(mode or "ptt").strip().lower() or "ptt")
        with self._lock:
            mic = self._mics.get(key)
            if mic is None:
                mic = ReplayMic()
                self._mics[key] = mic
            return mic

    def resolve_mic(self, cfg: Dict[str, Any], *, mode: str) -> Optional[ReplayMic]:
        instance_id = _cfg_instance_id(cfg)
        input_cfg = cfg.get("input") if isinstance(cfg.get("input"), dict) else {}
        mic_cfg = input_cfg.get("mic") if isinstance(input_cfg.get("mic"), dict) else {}
        params_cfg = mic_cfg.get("params") if isinstance(mic_cfg.get("params"), dict) else {}
        input_device = str(params_cfg.get("input_device") or "").strip()
        if input_device.startswith("demo_gate:"):
            instance_id = input_device.split(":", 1)[1].strip() or instance_id
        key = (instance_id, str(mode or "ptt").strip().lower() or "ptt")
        with self._lock:
            return self._mics.get(key)

    def queue_fixture(
        self,
        instance_id: str,
        *,
        mode: str,
        fixture_name: str,
        delay_before_s: float = 0.0,
    ) -> None:
        self.mic_for(instance_id, mode=mode).queue_fixture(
            name=fixture_name,
            wav_bytes=self._load_fixture_bytes(fixture_name),
            delay_before_s=float(delay_before_s),
        )

    def clear_instance(self, instance_id: str) -> None:
        instance_text = str(instance_id or "").strip() or "default"
        with self._lock:
            mics = [mic for (key, _mode), mic in self._mics.items() if key == instance_text]
        for mic in mics:
            mic.clear()


class FaultInjector:
    """Stage-specific fail-once injector used only by the harness."""

    def __init__(self) -> None:
        self._planned: Dict[str, List[Exception]] = {"stt": [], "llm": [], "tts": []}
        self._lock = threading.RLock()

    def clear(self) -> None:
        with self._lock:
            for stage in self._planned:
                self._planned[stage].clear()

    def plan_failure(self, stage: str, *, exc: Optional[Exception] = None, times: int = 1) -> None:
        stage_key = str(stage or "").strip().lower()
        if stage_key not in self._planned:
            raise ValueError(f"Unsupported fault stage: {stage!r}")
        default_exc = {
            "stt": RuntimeError("connection timed out"),
            "llm": RuntimeError("upstream llm service unavailable"),
            "tts": RuntimeError("connection timed out"),
        }[stage_key]
        with self._lock:
            for _ in range(max(0, int(times))):
                self._planned[stage_key].append(exc or default_exc)

    def _consume(self, stage: str) -> Optional[Exception]:
        stage_key = str(stage or "").strip().lower()
        with self._lock:
            queue_for_stage = self._planned.get(stage_key)
            if not queue_for_stage:
                return None
            return queue_for_stage.pop(0)

    def wrap_stt(self, backend: Any) -> Any:
        if backend is None or getattr(backend, "_demo_gate_fault_wrapped", False):
            return backend
        injector = self

        class _WrappedSTT:
            _demo_gate_fault_wrapped = True

            def __init__(self, inner: Any) -> None:
                self._inner = inner

            def transcribe(self, audio: Any) -> Any:
                planned = injector._consume("stt")
                if planned is not None:
                    raise planned
                return self._inner.transcribe(audio)

            def __getattr__(self, name: str) -> Any:
                return getattr(self._inner, name)

        return _WrappedSTT(backend)

    def wrap_pipeline(self, pipeline: Any) -> Any:
        if pipeline is None or getattr(pipeline, "_demo_gate_fault_wrapped", False):
            return pipeline

        llm_backend = getattr(pipeline, "llm", None)
        output_backend = getattr(pipeline, "output", None)

        if llm_backend is not None and not getattr(llm_backend, "_demo_gate_fault_wrapped", False):
            injector = self

            class _WrappedLLM:
                _demo_gate_fault_wrapped = True

                def __init__(self, inner: Any) -> None:
                    self._inner = inner

                def generate(self, messages: Any) -> Any:
                    planned = injector._consume("llm")
                    if planned is not None:
                        raise planned
                    return self._inner.generate(messages)

                def __getattr__(self, name: str) -> Any:
                    return getattr(self._inner, name)

            pipeline.llm = _WrappedLLM(llm_backend)

        if output_backend is not None and not getattr(output_backend, "_demo_gate_fault_wrapped", False):
            injector = self

            class _WrappedOutput:
                _demo_gate_fault_wrapped = True

                def __init__(self, inner: Any) -> None:
                    self._inner = inner

                def emit(self, text: str) -> Any:
                    planned = injector._consume("tts")
                    if planned is not None:
                        raise planned
                    return self._inner.emit(text)

                def __getattr__(self, name: str) -> Any:
                    return getattr(self._inner, name)

            pipeline.output = _WrappedOutput(output_backend)

        setattr(pipeline, "_demo_gate_fault_wrapped", True)
        return pipeline


class InProcessDMClient:
    """DMClient-compatible adapter backed by a Flask test app."""

    def __init__(self, app: Any, base_url: str, *, sid: str, timeout_s: float = 12.0) -> None:
        self.app = app
        self.base_url = _norm_url(base_url)
        self.timeout_s = float(timeout_s)
        self.sid = str(sid or "").strip() or uuid.uuid4().hex

    def _open(
        self,
        method: str,
        path: str,
        *,
        payload: Optional[Dict[str, Any]] = None,
        query: Optional[Dict[str, Any]] = None,
    ) -> Any:
        headers = {"Cookie": f"sid={self.sid}"}
        client = self.app.test_client(use_cookies=False)
        kwargs: Dict[str, Any] = {"method": method, "path": path, "headers": headers}
        if payload is not None:
            kwargs["json"] = payload
        if query:
            kwargs["query_string"] = query
        return client.open(**kwargs)

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        payload: Optional[Dict[str, Any]] = None,
        query: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        resp = self._open(method, path, payload=payload, query=query)
        try:
            data = resp.get_json()
        except Exception as exc:
            raise DMClientError(
                f"{method} {self.base_url}{path} returned non-JSON response (status={resp.status_code})"
            ) from exc
        if not isinstance(data, dict):
            raise DMClientError(f"{method} {self.base_url}{path} returned invalid payload type")
        if resp.status_code >= 400:
            err = data.get("error") or data
            detail = str(data.get("detail") or "").strip()
            if detail:
                err = f"{err}: {detail}"
            raise DMClientError(f"{method} {self.base_url}{path} failed (status={resp.status_code}): {err}")
        if data.get("ok") is False:
            raise DMClientError(f"{method} {self.base_url}{path} returned ok=false: {data.get('error')}")
        return data

    def _request_bytes(self, method: str, path: str, *, payload: Optional[Dict[str, Any]] = None) -> bytes:
        resp = self._open(method, path, payload=payload)
        if resp.status_code >= 400:
            try:
                data = resp.get_json()
            except Exception:
                data = None
            if isinstance(data, dict):
                err = data.get("error") or data
                detail = str(data.get("detail") or "").strip()
                if detail:
                    err = f"{err}: {detail}"
            else:
                err = resp.get_data(as_text=True)
            raise DMClientError(f"{method} {self.base_url}{path} failed (status={resp.status_code}): {err}")
        return bytes(resp.get_data() or b"")

    def capabilities(self, timeout_s: Optional[float] = None) -> Dict[str, Any]:
        del timeout_s
        return self._request_json("GET", "/api/script/capabilities")

    def summary_page_url(self) -> str:
        return f"{self.base_url}/summary"

    def summary_get(self, timeout_s: Optional[float] = None) -> Dict[str, Any]:
        del timeout_s
        return self._request_json("GET", "/api/summary")

    def summary_start(self, timeout_s: Optional[float] = None) -> Dict[str, Any]:
        del timeout_s
        return self._request_json("POST", "/api/summary/start", payload={})

    def summary_abort(self, timeout_s: Optional[float] = None) -> Dict[str, Any]:
        del timeout_s
        return self._request_json("POST", "/api/summary/abort", payload={})

    def script_say(
        self,
        text: str,
        *,
        timeout_s: Optional[float] = None,
        preloaded_audio_b64: Optional[str] = None,
        preloaded_audio_format: Optional[str] = None,
    ) -> Dict[str, Any]:
        del timeout_s
        payload: Dict[str, Any] = {"text": str(text or "")}
        if preloaded_audio_b64:
            payload["preloaded_audio_b64"] = str(preloaded_audio_b64)
            payload["preloaded_audio_format"] = str(preloaded_audio_format or "wav")
        return self._request_json("POST", "/api/script/say", payload=payload)

    def script_do(self, payload: Dict[str, Any], timeout_s: Optional[float] = None) -> Dict[str, Any]:
        del timeout_s
        return self._request_json("POST", "/api/script/do", payload=dict(payload or {}))

    def runtime_effective(self, timeout_s: Optional[float] = None) -> Dict[str, Any]:
        del timeout_s
        return self._request_json("GET", "/api/runtime_effective")

    def runtime_health(self, payload: Dict[str, Any], timeout_s: Optional[float] = None) -> Dict[str, Any]:
        del timeout_s
        return self._request_json("POST", "/api/runtime_health", payload=dict(payload or {}))

    def nao_command_state(self, timeout_s: Optional[float] = None) -> Dict[str, Any]:
        del timeout_s
        return self._request_json("GET", "/api/nao_command_state")

    def process_status(self, timeout_s: Optional[float] = None) -> Dict[str, Any]:
        del timeout_s
        return self._request_json("GET", "/api/process_status")

    def process_start(self, name: str, *, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        request_payload = {"name": str(name or "").strip()}
        if payload:
            request_payload.update(dict(payload))
        return self._request_json("POST", "/api/process_start", payload=request_payload)

    def process_stop(self, name: str) -> Dict[str, Any]:
        return self._request_json("POST", "/api/process_stop", payload={"name": str(name or "").strip()})

    def auto_rest_suspend_acquire(
        self,
        *,
        lease_id: str,
        owner: str,
        reason: str,
        ttl_s: float,
        timeout_s: Optional[float] = None,
    ) -> Dict[str, Any]:
        del timeout_s
        return self._request_json(
            "POST",
            "/api/auto_rest_suspend/acquire",
            payload={
                "lease_id": str(lease_id or "").strip(),
                "owner": str(owner or "").strip(),
                "reason": str(reason or "").strip(),
                "ttl_s": float(ttl_s),
            },
        )

    def auto_rest_suspend_renew(
        self,
        *,
        lease_id: str,
        owner: str,
        ttl_s: float,
        timeout_s: Optional[float] = None,
    ) -> Dict[str, Any]:
        del timeout_s
        return self._request_json(
            "POST",
            "/api/auto_rest_suspend/renew",
            payload={
                "lease_id": str(lease_id or "").strip(),
                "owner": str(owner or "").strip(),
                "ttl_s": float(ttl_s),
            },
        )

    def auto_rest_suspend_release(
        self,
        *,
        lease_id: str,
        timeout_s: Optional[float] = None,
    ) -> Dict[str, Any]:
        del timeout_s
        return self._request_json(
            "POST",
            "/api/auto_rest_suspend/release",
            payload={"lease_id": str(lease_id or "").strip()},
        )

    def state(self) -> Dict[str, Any]:
        return self._request_json("GET", "/api/state")

    def reset(self) -> Dict[str, Any]:
        return self._request_json("POST", "/api/reset", payload={})

    def continuous_start(self) -> Dict[str, Any]:
        return self._request_json("POST", "/api/continuous_start", payload={})

    def continuous_stop(self) -> Dict[str, Any]:
        return self._request_json("POST", "/api/continuous_stop", payload={})

    def continuous_state(self) -> Dict[str, Any]:
        return self._request_json("GET", "/api/continuous_state")

    def dm_events(
        self,
        *,
        limit: int = 2000,
        event: Optional[str] = None,
        source: Optional[str] = None,
    ) -> Dict[str, Any]:
        query: Dict[str, Any] = {"limit": int(limit)}
        if event:
            query["event"] = str(event)
        if source:
            query["source"] = str(source)
        return self._request_json("GET", "/api/dm_events", query=query)

    def dm_events_clear(self) -> Dict[str, Any]:
        return self._request_json("POST", "/api/dm_events_clear", payload={})

    def runtime_config_set(self, config: Dict[str, Any]) -> Dict[str, Any]:
        return self._request_json("POST", "/api/runtime_config", payload={"config": dict(config or {})})

    def summary_config_get(self) -> Dict[str, Any]:
        return self._request_json("GET", "/api/summary/config")

    def summary_config_set(self, config: Dict[str, Any]) -> Dict[str, Any]:
        return self._request_json("POST", "/api/summary/config", payload={"config": dict(config or {})})

    def summary_stop(self) -> Dict[str, Any]:
        return self._request_json("POST", "/api/summary/stop", payload={})

    def summary_transcript_update(self, transcript_text: str) -> Dict[str, Any]:
        return self._request_json(
            "POST",
            "/api/summary/transcript",
            payload={"transcript_text": str(transcript_text or "")},
        )

    def summary_generate(self) -> Dict[str, Any]:
        return self._request_json("POST", "/api/summary/generate", payload={})

    def summary_execution_mode(self, execution_mode: str) -> Dict[str, Any]:
        return self._request_json(
            "POST",
            "/api/summary/execution_mode",
            payload={"execution_mode": str(execution_mode or "").strip()},
        )

    def summary_capture_recover(self) -> Dict[str, Any]:
        return self._request_json("POST", "/api/summary/capture/recover", payload={})

    def summary_publish(self, *, draft_text: Optional[str] = None, allow_stale: bool = False) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"allow_stale": bool(allow_stale)}
        if draft_text is not None:
            payload["draft_text"] = str(draft_text)
        return self._request_json("POST", "/api/summary/publish", payload=payload)

    def summary_complete_without_publish(self) -> Dict[str, Any]:
        return self._request_json("POST", "/api/summary/complete_without_publish", payload={})

    def summary_use_fallback_summary(self) -> Dict[str, Any]:
        return self._request_json("POST", "/api/summary/use_fallback_summary", payload={})

    def summary_stop_output(self) -> Dict[str, Any]:
        return self._request_json("POST", "/api/summary/stop_output", payload={})

    def summary_clear(self) -> Dict[str, Any]:
        return self._request_json("POST", "/api/summary/clear", payload={})

    def script_tts_render(self, *, text: str) -> bytes:
        return self._request_bytes("POST", "/api/script/tts_render", payload={"text": str(text or "")})


class RunnerEventRecorder:
    def __init__(self) -> None:
        self._events: List[Dict[str, Any]] = []
        self._condition = threading.Condition()

    def append(self, event: Dict[str, Any]) -> None:
        with self._condition:
            self._events.append(dict(event or {}))
            self._condition.notify_all()

    def snapshot(self) -> List[Dict[str, Any]]:
        with self._condition:
            return list(self._events)

    def wait_for(
        self,
        predicate: Callable[[Dict[str, Any]], bool],
        *,
        timeout_s: float,
        since_index: int = 0,
    ) -> Tuple[int, Dict[str, Any]]:
        deadline = time.time() + float(timeout_s)
        with self._condition:
            while True:
                for index in range(max(0, int(since_index)), len(self._events)):
                    event = self._events[index]
                    if predicate(event):
                        return index, dict(event)
                remaining = deadline - time.time()
                if remaining <= 0:
                    break
                self._condition.wait(timeout=min(0.1, remaining))
        raise ScenarioFailure("Timed out while waiting for runner event.")


@dataclass
class AppInstance:
    base_url: str
    instance_id: str
    sid: str
    app: Any
    client: InProcessDMClient
    runtime_config: Dict[str, Any] = field(default_factory=dict)
    summary_config: Dict[str, Any] = field(default_factory=dict)
    started_processes: List[str] = field(default_factory=list)


@dataclass
class RunningScript:
    name: str
    runner: ScriptRunner
    recorder: RunnerEventRecorder
    continue_event: threading.Event
    thread: threading.Thread
    result_box: Dict[str, Any]
    auto_continue_thread: Optional[threading.Thread] = None

    def wait(self, timeout_s: float) -> RunResult:
        self.thread.join(timeout=timeout_s)
        if self.thread.is_alive():
            raise ScenarioFailure(f"Runner thread did not finish for script {self.name!r}.")
        error = self.result_box.get("error")
        if error is not None:
            raise ScenarioFailure(f"Script {self.name!r} failed: {error}")
        result = self.result_box.get("result")
        if not isinstance(result, RunResult):
            raise ScenarioFailure(f"Script {self.name!r} did not produce a RunResult.")
        return result


@dataclass
class ScenarioResult:
    profile: str
    scenario: str
    passed: bool
    duration_s: float
    detail: str = ""


class DemoGateReporter:
    def __init__(self, artifact_root: Path, *, console_write: Optional[Callable[[str], None]] = None) -> None:
        self.artifact_root = Path(artifact_root)
        self.operator_log_path = self.artifact_root / "operator_trace.log"
        self.technical_log_path = self.artifact_root / "technical_trace.log"
        self.runner_log_dir = self.artifact_root / "runner_logs"
        self._console_write = console_write or print
        self._operator_handle = None
        self._technical_handle = None

    def open(self) -> None:
        self.artifact_root.mkdir(parents=True, exist_ok=True)
        if self._operator_handle is None:
            self._operator_handle = self.operator_log_path.open("a", encoding="utf-8")
        if self._technical_handle is None:
            self._technical_handle = self.technical_log_path.open("a", encoding="utf-8")

    def close(self) -> None:
        if self._operator_handle is not None:
            self._operator_handle.close()
            self._operator_handle = None
        if self._technical_handle is not None:
            self._technical_handle.close()
            self._technical_handle = None

    def info(self, message: str) -> None:
        line = str(message or "").strip()
        if not line:
            return
        self.open()
        self._console_write(line)
        assert self._operator_handle is not None
        self._operator_handle.write(line + "\n")
        self._operator_handle.flush()

    def technical(self, message: str) -> None:
        line = str(message or "").rstrip()
        if not line:
            return
        self.open()
        assert self._technical_handle is not None
        self._technical_handle.write(line + "\n")
        self._technical_handle.flush()

    def run_start(self, *, profile: str, scenario: str) -> None:
        operator_name = SCENARIO_OPERATOR_NAME.get(str(scenario or "").strip().lower(), scenario)
        self.info(f"Demo-gate start: profiel={profile}, scenario={operator_name}.")
        self.info(f"Artifactmap: {self.artifact_root}")
        self.info(f"Operatorlog: {self.operator_log_path}")
        self.info(f"Uitgebreide runnerlogs: {self.runner_log_dir}")
        self.info(f"Technische trace: {self.technical_log_path}")

    def startup_preparing(self) -> None:
        self.info("Services en verbindingen worden voorbereid; dit kan even duren.")

    def scenario_start(self, scenario: str) -> None:
        operator_name = SCENARIO_OPERATOR_NAME.get(str(scenario or "").strip().lower(), scenario)
        self.info(f"Scenario gestart: {operator_name}.")

    def scenario_success(self, result: ScenarioResult, *, keep_artifacts: bool) -> None:
        operator_name = SCENARIO_OPERATOR_NAME.get(result.scenario, result.scenario)
        self.info(f"Scenario geslaagd: {operator_name} ({result.duration_s:.2f}s).")
        self.info(f"Uitgebreide logs: {self.artifact_root}")
        if not keep_artifacts:
            self.info("Artifacts worden na deze succesvolle run verwijderd. Kies 'Artifacts bewaren' om ze te behouden.")

    def scenario_failure(self, result: ScenarioResult) -> None:
        operator_name = SCENARIO_OPERATOR_NAME.get(result.scenario, result.scenario)
        self.info(f"Scenario mislukt: {operator_name}.")
        if str(result.detail or "").strip():
            self.info(f"Fout: {result.detail}")
        self.info(f"Uitgebreide logs: {self.artifact_root}")

    def setup_failure(self, *, scenario: str, detail: str) -> None:
        operator_name = SCENARIO_OPERATOR_NAME.get(str(scenario or "").strip().lower(), scenario)
        self.info(f"Setup mislukt voor scenario: {operator_name}.")
        if str(detail or "").strip():
            self.info(f"Fout: {detail}")
        self.info(f"Uitgebreide logs: {self.artifact_root}")

    def phase(self, label: str) -> None:
        self.info(label)

    def continuous_listening_started(self) -> None:
        self.info("Continuous listening gestart.")

    def continuous_listening_stopped(self) -> None:
        self.info("Continuous listening gestopt.")

    def simulate_user_speech(self, text: str, *, summary: bool = False, retry: bool = False) -> None:
        prefix = "Gebruikersspraak opnieuw simuleren voor summary" if retry and summary else (
            "Gebruikersspraak simuleren voor summary" if summary else "Gebruikersspraak simuleren"
        )
        self.info(f"{prefix}: '{text}'")

    def processing(self) -> None:
        self.info("Verwerken...")

    def chat_reply(self, *, llm_type: Optional[str] = None) -> None:
        model = str(llm_type or "").strip()
        if model:
            self.info(f"Chat met {model} LLM stuurde TTS-bericht.")
            return
        self.info("Chat antwoord gemaakt en via TTS uitgesproken.")

    def command_recognized(self, command: str) -> None:
        self.info(f"Commando herkend: {command}")

    def command_state(self, *, mode: Optional[str], behavior: Optional[str]) -> None:
        mode_text = str(mode or "").strip()
        behavior_text = _normalize_command_value(behavior)
        if mode_text or behavior_text:
            self.info(f"Commandostatus: mode={mode_text or '-'}, active_behavior={behavior_text or '-'}")

    def command_executed(self, *, live_robot: bool) -> None:
        if live_robot:
            self.info("Uitgevoerd op robot.")
            return
        self.info("Uitgevoerd op virtuele robot (dry run).")

    def robot_settle_wait(self, seconds: float) -> None:
        self.info(f"Wachten op robotafronding: {float(seconds):.1f}s")

    def summary_started(self) -> None:
        self.info("Summary gestart door Script Runner.")

    def summary_waiting(self) -> None:
        self.info("Script Runner wacht op de summary.")

    def summary_capturing(self) -> None:
        self.info("Summary neemt spraak op.")

    def summary_transcript_ready(self) -> None:
        self.info("Transcript klaar voor controle.")

    def summary_transcript_updated(self, note: str) -> None:
        self.info(f"Transcript bijgewerkt: {note}")

    def summary_generating(self) -> None:
        self.info("Samenvatting wordt gegenereerd.")

    def summary_generated(self) -> None:
        self.info("Samenvatting gegenereerd.")

    def summary_published(self) -> None:
        self.info("Samenvatting uitgesproken en sessie afgerond.")

    def summary_completed_without_publish(self) -> None:
        self.info("Summary afgerond zonder publish.")

    def summary_runner_completed(self) -> None:
        self.info("Script Runner heeft de samenvattingsstap afgerond.")

    def process_already_available(self, name: str, endpoint_url: str) -> None:
        self.info(f"{name} is al bereikbaar op {endpoint_url}; bestaande service wordt gebruikt.")

    def process_starting(self, name: str, endpoint_url: str) -> None:
        self.info(f"{name} wordt gestart op {endpoint_url}.")

    def process_started(self, name: str, endpoint_url: str) -> None:
        self.info(f"{name} draait op {endpoint_url}.")

    def process_stopping(self, name: str, endpoint_url: str) -> None:
        self.info(f"{name} wordt gestopt op {endpoint_url}.")

    def fallback_stage(self, stage: str) -> None:
        label = str(stage or "").strip().upper() or "ONBEKEND"
        self.info(f"{label}-service onbereikbaar, recovery gestart.")

    def fallback_action(self, action_text: str) -> None:
        self.info(f"Herstelactie: {action_text}")

    def workshop_started(self) -> None:
        self.info("Workshopscript gestart.")

    def workshop_waiting_for_summary(self) -> None:
        self.info("Workshopscript wacht op een summary.")

    def workshop_completed(self) -> None:
        self.info("Workshopscript afgerond.")

class DemoGateHarness:
    def __init__(
        self,
        *,
        profile: str,
        runtime_preset: Optional[str] = None,
        summary_preset_id: Optional[str] = None,
        summary_script_path: Path = DEFAULT_SUMMARY_SCRIPT,
        workshop_script_path: Path = DEFAULT_WORKSHOP_SCRIPT,
        fixtures_root: Path = DEFAULT_AUDIO_FIXTURES_ROOT,
        nao_ip: Optional[str] = None,
        keep_artifacts: bool = False,
    ) -> None:
        self.profile = str(profile or "").strip().lower()
        if self.profile not in PROFILES:
            raise ValueError(f"Unsupported profile: {profile!r}")
        self.runtime_preset = runtime_preset
        self.summary_preset_id = str(summary_preset_id or "").strip() or None
        self.summary_script_path = Path(summary_script_path)
        self.workshop_script_path = Path(workshop_script_path)
        self.fixtures_root = Path(fixtures_root)
        self.nao_ip = str(nao_ip or "").strip() or None
        self.keep_artifacts = bool(keep_artifacts)
        self.summary_script = _load_json(self.summary_script_path)
        self.workshop_script = _load_json(self.workshop_script_path)
        self.base_cfg = _make_local_paths_absolute(_load_json(DEFAULT_DM_CONFIG_PATH))
        self.summary_execution_mode = "local" if self.profile == PROFILE_OFFLINE else "cloud"
        self.replay_registry = ReplayMicRegistry(self.fixtures_root)
        self.fault_injector = FaultInjector()
        self.instances: Dict[str, AppInstance] = {}
        self._original_make_mic = None
        self._original_make_stt = None
        self._original_build_pipeline = None
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.artifact_root = DM_ROOT / "logs" / "demo_gate" / f"run_{stamp}_{self.profile}"
        self.runtime_state_dir = self.artifact_root / "runtime_state"
        self.script_log_dir = self.artifact_root / "runner_logs"
        self.reporter = DemoGateReporter(self.artifact_root)
        self.runtime_config = self._build_runtime_config_for_url(self._primary_url())
        self.summary_config = self._build_summary_config(self.runtime_config)

    def _prepare_runner_script(self, script: Dict[str, Any]) -> Dict[str, Any]:
        return copy.deepcopy(script)

    def _resolve_runtime_preset_path(self) -> Path:
        if self.runtime_preset:
            candidate = Path(self.runtime_preset)
            if candidate.exists():
                return candidate
            runtime_dir = DM_ROOT / "configs" / "runtime"
            with_ext = runtime_dir / f"{self.runtime_preset}.json"
            if with_ext.exists():
                return with_ext
            named = runtime_dir / str(self.runtime_preset)
            if named.exists():
                return named
            raise FileNotFoundError(f"Runtime preset not found: {self.runtime_preset}")
        return DEFAULT_RUNTIME_PRESET_BY_PROFILE[self.profile]

    def _resolve_runtime_preset_path_for_url(self, dm_url: str) -> Path:
        if self.runtime_preset:
            return self._resolve_runtime_preset_path()
        if self.profile == PROFILE_LIVE_ROBOT and len(self._script_urls()) > 1:
            raise DemoGateError(
                "live_robot ondersteunt in demo-gate alleen single-robot scripts zonder expliciete runtime-override. "
                "Gebruik een single-robot script of start robots buiten demo-gate om met hun eigen DM-configs."
            )
        return DEFAULT_RUNTIME_PRESET_BY_PROFILE[self.profile]

    def _build_runtime_config_for_url(self, dm_url: str) -> Dict[str, Any]:
        runtime_cfg = copy.deepcopy(_load_json(self._resolve_runtime_preset_path_for_url(dm_url)))
        runtime_cfg.update(
            {
                "listen_mode": "continuous",
                "wake_mode": "never",
                "confirm_policy": "never",
            }
        )
        if self.profile in {PROFILE_OFFLINE, PROFILE_LIVE_SERVICES}:
            runtime_cfg.update(
                {
                    "base_enabled": False,
                    "behavior_enabled": False,
                    "nao_ip_enabled": False,
                    "base_autostart": False,
                    "behavior_autostart": False,
                    "output_target": "server",
                }
            )
        if self.profile == PROFILE_OFFLINE:
            runtime_cfg.update(
                {
                    "stt_type": "vosk",
                    "llm_type": "echo",
                    "llm_model": "",
                    "tts_engine": "piper",
                }
            )
        elif self.profile == PROFILE_LIVE_SERVICES:
            runtime_cfg.update(
                {
                    "stt_type": "azure",
                    "llm_type": "ollama_cloud",
                    "llm_model": str(runtime_cfg.get("llm_model") or "mistral-large-3:675b-cloud"),
                    "tts_engine": "azure",
                }
            )
        elif self.profile == PROFILE_LIVE_ROBOT and self.nao_ip:
            runtime_cfg["nao_ip"] = self.nao_ip
            runtime_cfg["nao_ip_enabled"] = True
        return runtime_cfg

    def _load_summary_preset(self) -> Dict[str, Any]:
        presets_payload = _load_json(DEFAULT_SUMMARY_PRESETS_PATH)
        presets = list(presets_payload.get("presets") or [])
        if not presets:
            raise DemoGateError("No summary presets configured.")
        if self.summary_preset_id:
            for item in presets:
                if str(item.get("id") or "").strip() == self.summary_preset_id:
                    config = item.get("config")
                    if isinstance(config, dict):
                        return copy.deepcopy(config)
            raise DemoGateError(f"Summary preset not found: {self.summary_preset_id}")
        config = presets[0].get("config")
        if not isinstance(config, dict):
            raise DemoGateError("Summary preset config is invalid.")
        return copy.deepcopy(config)

    def _piper_ref(self, runtime_config: Dict[str, Any]) -> str:
        model_path = str(runtime_config.get("piper_model_path") or "").strip()
        return f"piper:{model_path}" if model_path else "piper:default"

    def _build_summary_config(self, runtime_config: Dict[str, Any]) -> Dict[str, Any]:
        config = self._load_summary_preset()
        models = config.setdefault("models", {})
        if not isinstance(models, dict):
            raise DemoGateError("Summary preset models section is invalid.")
        models["stt_fallback"] = "vosk:default"
        models["tts_fallback"] = self._piper_ref(runtime_config)
        if self.profile == PROFILE_OFFLINE:
            models["llm_fallback"] = "echo:default"
            models["stt_primary"] = models["stt_fallback"]
            models["llm_primary"] = models["llm_fallback"]
            models["tts_primary"] = models["tts_fallback"]
        config["selected_preset_id"] = self.summary_preset_id
        return config

    def _script_urls(self) -> List[str]:
        urls: List[str] = []
        for script in (self.summary_script, self.workshop_script):
            robots = script.get("robots") if isinstance(script.get("robots"), dict) else {}
            for robot_cfg in robots.values():
                if not isinstance(robot_cfg, dict):
                    continue
                dm_url = _norm_url(robot_cfg.get("dm_url") or "")
                if dm_url and dm_url not in urls:
                    urls.append(dm_url)
        return urls

    def _summary_url(self) -> str:
        robots = self.summary_script.get("robots") if isinstance(self.summary_script.get("robots"), dict) else {}
        for robot_cfg in robots.values():
            if isinstance(robot_cfg, dict):
                dm_url = _norm_url(robot_cfg.get("dm_url") or "")
                if dm_url:
                    return dm_url
        raise DemoGateError("Summary script does not define a DM URL.")

    def _primary_url(self) -> str:
        robots = self.workshop_script.get("robots") if isinstance(self.workshop_script.get("robots"), dict) else {}
        for robot_cfg in robots.values():
            if isinstance(robot_cfg, dict):
                dm_url = _norm_url(robot_cfg.get("dm_url") or "")
                if dm_url:
                    return dm_url
        return self._summary_url()

    def _patch_factories(self) -> None:
        self._original_make_mic = webapp_server._make_mic_from_config
        self._original_make_stt = webapp_server.make_stt_backend_from_config
        self._original_build_pipeline = webapp_server.build_pipeline_from_config

        replay_registry = self.replay_registry
        fault_injector = self.fault_injector
        original_make_mic = self._original_make_mic
        original_make_stt = self._original_make_stt
        original_build_pipeline = self._original_build_pipeline

        def _patched_make_mic(cfg: Dict[str, Any], *, mode: str = "ptt") -> Any:
            replay_mic = replay_registry.resolve_mic(cfg, mode=mode)
            if replay_mic is not None:
                return replay_mic
            return original_make_mic(cfg, mode=mode)

        def _patched_make_stt(cfg: Dict[str, Any]) -> Any:
            return fault_injector.wrap_stt(original_make_stt(cfg))

        def _patched_build_pipeline(cfg: Dict[str, Any], *args: Any, **kwargs: Any) -> Any:
            pipeline = original_build_pipeline(cfg, *args, **kwargs)
            self._attach_behavior_logger(pipeline)
            return fault_injector.wrap_pipeline(pipeline)

        webapp_server._make_mic_from_config = _patched_make_mic
        webapp_server.make_stt_backend_from_config = _patched_make_stt
        webapp_server.build_pipeline_from_config = _patched_build_pipeline

    def _restore_factories(self) -> None:
        if self._original_make_mic is not None:
            webapp_server._make_mic_from_config = self._original_make_mic
        if self._original_make_stt is not None:
            webapp_server.make_stt_backend_from_config = self._original_make_stt
        if self._original_build_pipeline is not None:
            webapp_server.build_pipeline_from_config = self._original_build_pipeline

    def setup(self) -> None:
        self.artifact_root.mkdir(parents=True, exist_ok=True)
        self.runtime_state_dir.mkdir(parents=True, exist_ok=True)
        self.script_log_dir.mkdir(parents=True, exist_ok=True)
        self.reporter.open()
        self._patch_factories()
        try:
            for dm_url in self._script_urls():
                instance_id = _instance_id_for_url(dm_url)
                cfg = copy.deepcopy(self.base_cfg)
                cfg.setdefault("run", {})["instance_id"] = instance_id
                app, _base_stt, _base_pipeline = webapp_server.create_app(
                    cfg=cfg,
                    config_path=str(DEFAULT_DM_CONFIG_PATH),
                    instance_id=instance_id,
                    runtime_state_dir=str(self.runtime_state_dir),
                )
                sid = f"demo_gate_{instance_id}"
                client = InProcessDMClient(app, dm_url, sid=sid)
                self.instances[_norm_url(dm_url)] = AppInstance(
                    base_url=_norm_url(dm_url),
                    instance_id=instance_id,
                    sid=sid,
                    app=app,
                    client=client,
                )
                self.replay_registry.mic_for(instance_id, mode="continuous")
                self.replay_registry.mic_for(instance_id, mode="ptt")
            for instance in self.instances.values():
                runtime_config = self._build_runtime_config_for_url(instance.base_url)
                runtime_config["input_device"] = f"demo_gate:{instance.instance_id}"
                summary_config = self._build_summary_config(runtime_config)
                devices_cfg = summary_config.setdefault("devices", {})
                if isinstance(devices_cfg, dict):
                    devices_cfg["input_audio_device"] = f"demo_gate:{instance.instance_id}"
                instance.runtime_config = copy.deepcopy(runtime_config)
                instance.summary_config = copy.deepcopy(summary_config)
                instance.client.runtime_config_set(runtime_config)
                instance.client.summary_config_set(summary_config)
                instance.client.dm_events_clear()
                instance.client.reset()
                instance.client.summary_abort()
                instance.client.summary_clear()
            primary = self.primary
            self.runtime_config = copy.deepcopy(primary.runtime_config)
            self.summary_config = copy.deepcopy(primary.summary_config)
            if _is_live_robot_profile(self.profile):
                self._ensure_live_robot_processes_started()
        except Exception:
            self.reporter.technical(traceback.format_exc())
            self.close(keep_artifacts=True)
            raise

    def close(self, *, keep_artifacts: Optional[bool] = None) -> None:
        keep = self.keep_artifacts if keep_artifacts is None else bool(keep_artifacts)
        for instance in self.instances.values():
            try:
                instance.client.continuous_stop()
            except Exception:
                pass
            try:
                instance.client.summary_abort()
            except Exception:
                pass
            for process_name in reversed(list(instance.started_processes)):
                try:
                    self.reporter.process_stopping(
                        process_name,
                        self._process_endpoint_url(process_name, instance),
                    )
                    instance.client.process_stop(process_name)
                except Exception:
                    pass
            instance.started_processes.clear()
        self.instances.clear()
        self._restore_factories()
        self.reporter.close()
        if not keep and self.artifact_root.exists():
            shutil.rmtree(self.artifact_root, ignore_errors=True)

    def _attach_behavior_logger(self, pipeline: Any) -> None:
        behavior_executor = getattr(pipeline, "_behavior_executor", None)
        setter = getattr(behavior_executor, "set_logger", None)
        if callable(setter):
            setter(self.reporter.technical)

    def _live_robot_runtime_payload(self, instance: AppInstance) -> Dict[str, Any]:
        runtime_config = instance.runtime_config or self.runtime_config
        return {
            "nao_base_url": runtime_config.get("nao_base_url"),
            "behavior_manager_url": runtime_config.get("behavior_manager_url"),
            "nao_ip": runtime_config.get("nao_ip"),
            "nao_ip_enabled": runtime_config.get("nao_ip_enabled"),
            "base_enabled": runtime_config.get("base_enabled"),
            "behavior_enabled": runtime_config.get("behavior_enabled"),
        }

    def _process_endpoint_url(self, process_name: str, instance: AppInstance) -> str:
        runtime_config = instance.runtime_config or self.runtime_config
        if str(process_name or "").strip().lower() == "behavior":
            return _norm_url(runtime_config.get("behavior_manager_url") or "") or "<behavior_manager_url ontbreekt>"
        return _norm_url(runtime_config.get("nao_base_url") or "") or "<nao_base_url ontbreekt>"

    def _process_status_entry(self, instance: AppInstance, process_name: str) -> Dict[str, Any]:
        payload = instance.client.process_status()
        processes = payload.get("processes") if isinstance(payload.get("processes"), dict) else {}
        entry = processes.get(str(process_name or "").strip().lower())
        return dict(entry or {}) if isinstance(entry, dict) else {}

    def _wait_for_process_running(
        self,
        instance: AppInstance,
        process_name: str,
        *,
        timeout_s: float = 20.0,
    ) -> None:
        latest: Dict[str, Any] = {}
        deadline = time.time() + float(timeout_s)
        while time.time() < deadline:
            latest = self._process_status_entry(instance, process_name)
            if bool(latest.get("running")):
                return
            if latest.get("exit_code") is not None:
                raise ScenarioFailure(
                    f"{process_name} process exited during startup for {instance.base_url}: {latest}"
                )
            time.sleep(0.2)
        raise ScenarioFailure(
            f"Timed out waiting for {process_name} process to run for {instance.base_url}; latest={latest}"
        )

    def _live_robot_ready(self, instance: AppInstance) -> bool:
        try:
            health = instance.client.runtime_health(self._live_robot_runtime_payload(instance))
        except DMClientError:
            return False
        if not bool(health.get("ok")):
            return False
        try:
            nao_state = instance.client.nao_command_state()
        except DMClientError:
            return False
        return bool(nao_state.get("reachable"))

    def _wait_for_live_robot_ready(self, instance: AppInstance, *, timeout_s: float = 20.0) -> None:
        deadline = time.time() + float(timeout_s)
        while time.time() < deadline:
            if self._live_robot_ready(instance):
                return
            time.sleep(0.5)
        health = instance.client.runtime_health(self._live_robot_runtime_payload(instance))
        nao_state = instance.client.nao_command_state()
        raise ScenarioFailure(
            f"Robot backend did not become ready for {instance.base_url}; health={health}, nao_state={nao_state}"
        )

    def _ensure_process_started(
        self,
        instance: AppInstance,
        process_name: str,
        *,
        payload: Dict[str, Any],
    ) -> None:
        process_key = str(process_name or "").strip().lower()
        endpoint_url = self._process_endpoint_url(process_key, instance)
        if bool(self._process_status_entry(instance, process_key).get("running")):
            self.reporter.process_already_available(process_key, endpoint_url)
            return
        if self._live_robot_ready(instance):
            self.reporter.process_already_available(process_key, endpoint_url)
            return
        self.reporter.process_starting(process_key, endpoint_url)
        try:
            instance.client.process_start(process_key, payload=payload)
        except DMClientError:
            if self._live_robot_ready(instance):
                self.reporter.process_already_available(process_key, endpoint_url)
                return
            raise
        instance.started_processes.append(process_key)
        self._wait_for_process_running(instance, process_key)
        self.reporter.process_started(process_key, endpoint_url)

    def _ensure_live_robot_processes_started(self) -> None:
        for instance in self.instances.values():
            runtime_config = instance.runtime_config or {}
            payload = self._live_robot_runtime_payload(instance)
            if bool(runtime_config.get("base_enabled")):
                if bool(runtime_config.get("base_autostart")):
                    self._ensure_process_started(instance, "base", payload=payload)
                elif not self._live_robot_ready(instance):
                    raise ScenarioFailure(
                        "Base controller is not reachable and base_autostart is disabled; "
                        f"start it manually for {self._process_endpoint_url('base', instance)}."
                    )
            if bool(runtime_config.get("behavior_enabled")):
                if bool(runtime_config.get("behavior_autostart")):
                    self._ensure_process_started(instance, "behavior", payload=payload)
                elif not self._live_robot_ready(instance):
                    raise ScenarioFailure(
                        "Behavior manager is not reachable and behavior_autostart is disabled; "
                        f"start it manually for {self._process_endpoint_url('behavior', instance)}."
                    )
            self._wait_for_live_robot_ready(instance)

    def get_instance(self, base_url: str) -> AppInstance:
        instance = self.instances.get(_norm_url(base_url))
        if instance is None:
            raise DemoGateError(f"No app instance configured for {base_url!r}")
        return instance

    @property
    def primary(self) -> AppInstance:
        return self.get_instance(self._primary_url())

    @property
    def summary_target(self) -> AppInstance:
        return self.get_instance(self._summary_url())

    def queue_fixture(self, instance: AppInstance, fixture_name: str, *, delay_before_s: float = 0.0) -> None:
        self.replay_registry.queue_fixture(
            instance.instance_id,
            mode="continuous",
            fixture_name=fixture_name,
            delay_before_s=delay_before_s,
        )

    def _wait_for_history_growth(self, client: InProcessDMClient, *, before_len: int, timeout_s: float = 20.0) -> Dict[str, Any]:
        latest: Dict[str, Any] = {}
        deadline = time.time() + float(timeout_s)
        while time.time() < deadline:
            latest = client.state()
            history = list(latest.get("history") or [])
            if len(history) >= before_len + 2:
                return latest
            time.sleep(0.05)
        raise ScenarioFailure(
            f"Timed out waiting for history growth; before={before_len}, latest={len(latest.get('history') or [])}"
        )

    def _wait_for_dialog_observation(
        self,
        client: InProcessDMClient,
        *,
        before_len: int,
        expect_command: Optional[str] = None,
        before_transition_count: int = 0,
        expect_stop_label: Optional[str] = None,
        expect_stop_available: Optional[bool] = None,
        timeout_s: float = 20.0,
    ) -> Tuple[Dict[str, Any], bool]:
        latest: Dict[str, Any] = {}
        latest_transition_commands: List[str] = []
        deadline = time.time() + float(timeout_s)
        expected_command_norm = _normalize_command_value(expect_command).upper()
        expected_stop_label_norm = _normalize_command_value(expect_stop_label)
        while time.time() < deadline:
            latest = client.state()
            history = list(latest.get("history") or [])
            history_ready = len(history) >= before_len + 2
            stop_ok = True
            if expect_stop_available is not None and bool(latest.get("command_stop_available")) != bool(expect_stop_available):
                stop_ok = False
            if expect_stop_label is not None and _normalize_command_value(latest.get("command_stop_label")) != expected_stop_label_norm:
                stop_ok = False
            if history_ready and stop_ok:
                return latest, True
            if expected_command_norm:
                transition_events = list(client.dm_events(event="state_transition").get("events") or [])
                if len(transition_events) > before_transition_count:
                    new_events = transition_events[before_transition_count:]
                    latest_transition_commands = [
                        _normalize_command_value((event.get("data") or {}).get("command")).upper()
                        for event in new_events
                    ]
                    if expected_command_norm in latest_transition_commands and stop_ok:
                        return latest, False
            time.sleep(0.05)
        if expected_command_norm:
            raise ScenarioFailure(
                "Timed out waiting for dialog command completion; "
                f"command={expected_command_norm!r}, before_history={before_len}, "
                f"latest_history={len(latest.get('history') or [])}, "
                f"latest_transition_commands={latest_transition_commands!r}, "
                f"command_stop_available={latest.get('command_stop_available')!r}, "
                f"command_stop_label={latest.get('command_stop_label')!r}"
            )
        raise ScenarioFailure(
            f"Timed out waiting for history growth; before={before_len}, latest={len(latest.get('history') or [])}"
        )

    def _wait_for_summary_status(
        self,
        client: InProcessDMClient,
        status: str,
        *,
        timeout_s: float = 30.0,
    ) -> Dict[str, Any]:
        target = str(status or "").strip().lower()
        latest: Dict[str, Any] = {}
        deadline = time.time() + float(timeout_s)
        while time.time() < deadline:
            latest = client.summary_get()
            session = latest.get("session") if isinstance(latest.get("session"), dict) else {}
            if str(session.get("status") or "").strip().lower() == target:
                return latest
            time.sleep(0.05)
        raise ScenarioFailure(f"Timed out waiting for summary status={target!r}; latest={latest!r}")

    def _prepare_dialog_client(self) -> InProcessDMClient:
        client = self.primary.client
        client.continuous_stop()
        client.reset()
        client.dm_events_clear()
        self.replay_registry.clear_instance(self.primary.instance_id)
        return client

    def _prepare_summary_client(self, instance: Optional[AppInstance] = None) -> InProcessDMClient:
        target_instance = instance or self.summary_target
        client = target_instance.client
        try:
            client.summary_stop_output()
        except Exception:
            pass
        client.summary_abort()
        client.summary_clear()
        client.dm_events_clear()
        self.replay_registry.clear_instance(target_instance.instance_id)
        return client

    def _summary_generate_timeout_s(
        self,
        *,
        execution_mode: Optional[str] = None,
        instance: Optional[AppInstance] = None,
    ) -> float:
        mode = str(execution_mode or self.summary_execution_mode or "").strip().lower()
        summary_config = instance.summary_config if instance is not None else self.summary_config
        models = summary_config.get("models") if isinstance(summary_config.get("models"), dict) else {}
        if mode == "cloud":
            return SUMMARY_GENERATE_TIMEOUT_CLOUD_S
        llm_ref = str(models.get("llm_fallback") or "").strip().lower()
        if llm_ref.startswith("echo:"):
            return SUMMARY_GENERATE_TIMEOUT_ECHO_S
        return SUMMARY_GENERATE_TIMEOUT_LOCAL_LLM_S

    def _summary_instance_for_event(self, event: Dict[str, Any], *, fallback: Optional[AppInstance] = None) -> AppInstance:
        summary_dm_url = _norm_url(event.get("summary_dm_url") or "")
        if summary_dm_url:
            return self.get_instance(summary_dm_url)
        robot_id = str(event.get("robot_id") or "").strip()
        if robot_id:
            robots = self.workshop_script.get("robots") if isinstance(self.workshop_script.get("robots"), dict) else {}
            robot_cfg = robots.get(robot_id) if isinstance(robots.get(robot_id), dict) else {}
            dm_url = _norm_url((robot_cfg or {}).get("dm_url") or "")
            if dm_url:
                return self.get_instance(dm_url)
        if fallback is not None:
            return fallback
        return self.summary_target

    def _queue_summary_fixtures_with_logging(
        self,
        instance: AppInstance,
        fixture_names: Sequence[str],
        *,
        retry: bool = False,
    ) -> None:
        for index, fixture_name in enumerate(fixture_names):
            self.reporter.simulate_user_speech(_fixture_text(fixture_name), summary=True, retry=retry)
            self.queue_fixture(instance, fixture_name, delay_before_s=0.0 if index == 0 else 0.2)

    def _dialog_step(
        self,
        client: InProcessDMClient,
        instance: AppInstance,
        fixture_name: str,
        *,
        expect_reply_only: bool = False,
        expect_command: Optional[str] = None,
        expect_mode: Optional[str] = None,
        expect_behavior: Optional[str] = None,
        expect_stop_label: Optional[str] = None,
        expect_stop_available: Optional[bool] = None,
        post_wait_s: float = 0.0,
    ) -> Dict[str, Any]:
        spoken_text = _fixture_text(fixture_name)
        self.reporter.simulate_user_speech(spoken_text)
        self.reporter.processing()
        before = client.state()
        before_len = len(before.get("history") or [])
        before_transition_count = 0
        if expect_command:
            before_transition_count = len(client.dm_events(event="state_transition").get("events") or [])
        self.queue_fixture(instance, fixture_name)
        after, history_ready = self._wait_for_dialog_observation(
            client,
            before_len=before_len,
            expect_command=expect_command,
            before_transition_count=before_transition_count,
            expect_stop_label=expect_stop_label,
            expect_stop_available=expect_stop_available,
        )
        history = list(after.get("history") or [])
        reply = _assistant_reply_from_growth(before_len, history) if history_ready else ""
        if expect_reply_only and not str(reply or "").strip():
            raise ScenarioFailure(f"Expected non-empty dialog reply for fixture {fixture_name!r}.")
        if expect_stop_available is not None and bool(after.get("command_stop_available")) != bool(expect_stop_available):
            raise ScenarioFailure(
                f"Expected command_stop_available={expect_stop_available} after {fixture_name!r}, got {after.get('command_stop_available')!r}"
            )
        if expect_stop_label is not None and _normalize_command_value(after.get("command_stop_label")) != _normalize_command_value(expect_stop_label):
            raise ScenarioFailure(
                f"Expected command_stop_label={expect_stop_label!r}, got {after.get('command_stop_label')!r}"
            )
        if expect_command:
            transition_events = list(client.dm_events(event="state_transition").get("events") or [])
            transition = _latest_transition_for_command(transition_events, expect_command)
            data = transition.get("data") if isinstance(transition.get("data"), dict) else {}
            self.reporter.command_recognized(expect_command)
            if expect_mode is not None and str(data.get("new_mode") or "") != str(expect_mode):
                raise ScenarioFailure(
                    f"Expected new_mode={expect_mode!r} for command={expect_command!r}, got {data.get('new_mode')!r}"
                )
            if expect_behavior is not None and _normalize_command_value(data.get("new_active_behavior")) != _normalize_command_value(expect_behavior):
                raise ScenarioFailure(
                    f"Expected new_active_behavior={expect_behavior!r} for command={expect_command!r}, got {data.get('new_active_behavior')!r}"
                )
            self.reporter.command_state(mode=data.get("new_mode"), behavior=data.get("new_active_behavior"))
            self.reporter.command_executed(live_robot=_is_live_robot_profile(self.profile))
        else:
            llm_type = instance.runtime_config.get("llm_type") if instance.runtime_config else self.runtime_config.get("llm_type")
            self.reporter.chat_reply(llm_type=llm_type)
        if post_wait_s > 0:
            self.reporter.robot_settle_wait(post_wait_s)
            time.sleep(float(post_wait_s))
        return after

    def _new_runner(
        self,
        script: Dict[str, Any],
        *,
        name: str,
    ) -> RunningScript:
        recorder = RunnerEventRecorder()
        continue_event = threading.Event()
        result_box: Dict[str, Any] = {}

        def _client_factory(base_url: str, timeout_s: float) -> InProcessDMClient:
            instance = self.get_instance(base_url)
            return InProcessDMClient(instance.app, instance.base_url, sid=instance.sid, timeout_s=timeout_s)

        runner = ScriptRunner(
            script=self._prepare_runner_script(script),
            client_factory=_client_factory,
            continue_event=continue_event,
            event_sink=recorder.append,
            log_dir=self.script_log_dir,
            on_error_prompt_policy="abort",
            summary_publish_policy="prompt",
            log_to_stdout=False,
        )

        def _run() -> None:
            try:
                result_box["result"] = runner.run()
            except Exception as exc:
                result_box["error"] = str(exc)
            finally:
                try:
                    runner.close()
                except Exception:
                    pass

        thread = threading.Thread(target=_run, name=f"demo_gate_{name}", daemon=True)
        return RunningScript(
            name=name,
            runner=runner,
            recorder=recorder,
            continue_event=continue_event,
            thread=thread,
            result_box=result_box,
        )

    def _start_runner(self, running: RunningScript, *, preflight: bool = False, auto_continue_manual: bool = False) -> None:
        if preflight:
            running.runner.preflight()
        running.thread.start()
        if auto_continue_manual:
            monitor_stop = threading.Event()

            def _auto_continue() -> None:
                since = 0
                while not monitor_stop.is_set() and running.thread.is_alive():
                    try:
                        index, _event = running.recorder.wait_for(
                            lambda item: str(item.get("type") or "") == "waiting"
                            and bool(item.get("waiting_for_next"))
                            and str(item.get("waiting_reason") or "") == "manual_start",
                            timeout_s=0.2,
                            since_index=since,
                        )
                    except ScenarioFailure:
                        continue
                    since = index + 1
                    time.sleep(0.2)
                    running.continue_event.set()

            thread = threading.Thread(target=_auto_continue, name=f"{running.name}_auto_continue", daemon=True)
            thread.start()
            running.auto_continue_thread = thread

    def _wait_for_summary_wait(self, running: RunningScript, *, timeout_s: float = 10.0) -> Dict[str, Any]:
        _index, event = running.recorder.wait_for(
            lambda item: str(item.get("type") or "") == "summary_state" and bool(item.get("summary_waiting")),
            timeout_s=timeout_s,
        )
        return event

    def _missing_summary_fixtures(self, session: Dict[str, Any], fixture_names: Sequence[str]) -> List[str]:
        transcript_text = str(session.get("transcript_text") or "").strip().lower()
        missing: List[str] = []
        for fixture_name in fixture_names:
            variants = SUMMARY_TRANSCRIPT_VARIANTS.get(str(fixture_name or "").strip(), ())
            if not any(variant in transcript_text for variant in variants):
                missing.append(str(fixture_name))
        return missing

    def _drive_summary_edit_flow(self, *, client: InProcessDMClient, instance: Optional[AppInstance] = None) -> None:
        target_instance = instance or self.summary_target
        if self.summary_execution_mode == "local":
            client.summary_execution_mode("local")
        initial_fixtures = [fixture_name for fixture_name, _text in FIXTURE_SUMMARY_SEQUENCE[:3]]
        self._queue_summary_fixtures_with_logging(target_instance, initial_fixtures)
        captured_payload: Dict[str, Any] = {}

        def _capture_stable() -> bool:
            nonlocal captured_payload
            captured_payload = client.summary_get()
            session = captured_payload.get("session") if isinstance(captured_payload.get("session"), dict) else {}
            transcript_lines = session.get("transcript_lines") if isinstance(session.get("transcript_lines"), list) else []
            stats = session.get("capture_stats") if isinstance(session.get("capture_stats"), dict) else {}
            return len(transcript_lines) >= 3 and int(stats.get("stt_queue_size") or 0) == 0

        try:
            _wait_until(_capture_stable, timeout_s=12.0, description="summary transcript capture")
        except ScenarioFailure:
            session = captured_payload.get("session") if isinstance(captured_payload.get("session"), dict) else {}
            missing_fixtures = self._missing_summary_fixtures(session, initial_fixtures)
            if not missing_fixtures:
                raise
            self.reporter.info("Niet alle summary-zinnen kwamen door; ontbrekende spraak wordt opnieuw aangeboden.")
            self._queue_summary_fixtures_with_logging(target_instance, missing_fixtures, retry=True)
            _wait_until(_capture_stable, timeout_s=8.0, description="summary transcript capture retry")
        stopped = client.summary_stop()
        session = stopped.get("session") if isinstance(stopped.get("session"), dict) else {}
        if str(session.get("status") or "") != "transcript_review":
            raise ScenarioFailure(f"Expected transcript_review after summary stop, got {session!r}")
        self.reporter.summary_transcript_ready()
        transcript_text = str(session.get("transcript_text") or "")
        transcript_lines = session.get("transcript_lines") if isinstance(session.get("transcript_lines"), list) else []
        if len(transcript_lines) < 3:
            raise ScenarioFailure(f"Transcript did not contain three captured lines: {session!r}")
        edited = transcript_text.rstrip()
        edited = f"{edited}\nzin vier" if edited else "zin vier"
        updated = client.summary_transcript_update(edited)
        updated_session = updated.get("session") if isinstance(updated.get("session"), dict) else {}
        if "zin vier" not in str(updated_session.get("transcript_text") or ""):
            raise ScenarioFailure("Transcript edit did not append 'zin vier'.")
        self.reporter.summary_transcript_updated("zin vier toegevoegd.")
        self.reporter.summary_generating()
        client.summary_generate()
        generated = self._wait_for_summary_status(
            client,
            "summary_review",
            timeout_s=self._summary_generate_timeout_s(
                execution_mode=self.summary_execution_mode,
                instance=target_instance,
            ),
        )
        generated_session = generated.get("session") if isinstance(generated.get("session"), dict) else {}
        if not str(generated_session.get("draft_text") or "").strip():
            raise ScenarioFailure("Generated summary draft was empty.")
        self.reporter.summary_generated()
        published = client.summary_publish()
        published_session = published.get("session") if isinstance(published.get("session"), dict) else {}
        if str(published_session.get("status") or "") != "completed":
            raise ScenarioFailure(f"Expected completed summary after publish, got {published_session!r}")
        self.reporter.summary_published()

    def run_happy_path_dialog(self) -> None:
        client = self._prepare_dialog_client()
        instance = self.primary
        live_robot = _is_live_robot_profile(self.profile)
        self.reporter.continuous_listening_started()
        client.continuous_start()
        try:
            self._dialog_step(client, instance, "hoi", expect_reply_only=True)
            self._dialog_step(
                client,
                instance,
                "sta_op",
                expect_command="STAND_UP",
                expect_mode="PERFORMING",
                expect_behavior="STAND_UP",
                post_wait_s=LIVE_ROBOT_SETTLE_STAND_UP_S if live_robot else 0.0,
            )
            self._dialog_step(
                client,
                instance,
                "loop_met_me_mee",
                expect_command="WALK_WITH_ME",
                expect_mode="PERFORMING",
                expect_behavior="WALK_WITH_ME",
                expect_stop_available=True,
                expect_stop_label="WALK_WITH_ME",
                post_wait_s=LIVE_ROBOT_SETTLE_WALK_WITH_ME_S if live_robot else 0.0,
            )
            self._dialog_step(
                client,
                instance,
                "gezellig_zo",
                expect_reply_only=True,
                expect_stop_available=True,
                expect_stop_label="WALK_WITH_ME",
            )
            self._dialog_step(
                client,
                instance,
                "stop_maar",
                expect_command="STOP",
                expect_mode="DIALOG",
                expect_behavior="",
                expect_stop_available=False,
                post_wait_s=LIVE_ROBOT_SETTLE_STOP_S if live_robot else 0.0,
            )
            self._dialog_step(
                client,
                instance,
                "motoren_uit",
                expect_command="REST",
                expect_mode="PERFORMING",
                expect_behavior="REST",
                post_wait_s=LIVE_ROBOT_SETTLE_REST_S if live_robot else 0.0,
            )
        finally:
            client.continuous_stop()
            self.reporter.continuous_listening_stopped()

    def run_summary_edit_flow(self) -> None:
        client = self._prepare_summary_client()
        running = self._new_runner(self.summary_script, name="summary_edit_flow")
        self.reporter.summary_started()
        self._start_runner(running, auto_continue_manual=True)
        event = self._wait_for_summary_wait(running)
        if not bool(event.get("summary_waiting")):
            raise ScenarioFailure("Script runner did not report summary_waiting.")
        self.reporter.summary_waiting()
        payload = self._wait_for_summary_status(client, "capturing", timeout_s=10.0)
        session = payload.get("session") if isinstance(payload.get("session"), dict) else {}
        if str(session.get("status") or "") != "capturing":
            raise ScenarioFailure(f"Expected capturing summary session, got {session!r}")
        self.reporter.summary_capturing()
        self._drive_summary_edit_flow(client=client)
        result = running.wait(timeout_s=20.0)
        if int(result.completed_steps) != int(result.total_steps):
            raise ScenarioFailure(f"Summary script did not complete all steps: {result}")
        self.reporter.summary_runner_completed()

    def run_service_loss_recovery(self) -> None:
        client = self._prepare_summary_client()
        running = self._new_runner(self.summary_script, name="service_loss_recovery")
        self.reporter.summary_started()
        self._start_runner(running, auto_continue_manual=True)
        self._wait_for_summary_wait(running)
        self.reporter.summary_waiting()
        self._wait_for_summary_status(client, "capturing", timeout_s=10.0)
        self.reporter.summary_capturing()

        if self.summary_execution_mode == "local":
            client.summary_execution_mode("local")
        self.fault_injector.plan_failure("stt", times=2)
        self.reporter.info("STT-service wordt bewust onderbroken voor de fallbacktest.")
        self.reporter.simulate_user_speech(_fixture_text("zin_een"), summary=True)
        self.queue_fixture(self.summary_target, "zin_een")
        deadline = time.time() + 10.0
        stt_payload: Dict[str, Any] = {}
        while time.time() < deadline:
            stt_payload = client.summary_get()
            session = stt_payload.get("session") if isinstance(stt_payload.get("session"), dict) else {}
            recovery = session.get("recovery") if isinstance(session.get("recovery"), dict) else {}
            banner = stt_payload.get("incident_banner") if isinstance(stt_payload.get("incident_banner"), dict) else {}
            if str(recovery.get("stage") or "").strip().lower() == "stt" and banner:
                break
            time.sleep(0.05)
        else:
            raise ScenarioFailure("Summary STT recovery state did not appear.")

        self.reporter.fallback_stage("stt")
        client.summary_execution_mode("local")
        self.reporter.fallback_action("capture hervatten in lokale modus.")
        client.summary_capture_recover()
        self._queue_summary_fixtures_with_logging(
            self.summary_target,
            [fixture_name for fixture_name, _text in FIXTURE_SUMMARY_SEQUENCE[1:3]],
        )

        recovered_payload: Dict[str, Any] = {}

        def _capture_recovered_and_drained() -> bool:
            nonlocal recovered_payload
            recovered_payload = client.summary_get()
            session = recovered_payload.get("session") if isinstance(recovered_payload.get("session"), dict) else {}
            transcript_lines = session.get("transcript_lines") if isinstance(session.get("transcript_lines"), list) else []
            stats = session.get("capture_stats") if isinstance(session.get("capture_stats"), dict) else {}
            return session.get("recovery") is None and len(transcript_lines) >= 2 and int(stats.get("stt_queue_size") or 0) == 0

        _wait_until(
            _capture_recovered_and_drained,
            timeout_s=10.0,
            description="summary capture recovery clear and transcript drain",
        )
        self.reporter.info("Summary capture is hersteld.")
        client.summary_stop()
        self.reporter.summary_transcript_ready()
        self.fault_injector.plan_failure("llm", times=2)
        self.reporter.info("LLM-service wordt bewust onderbroken voor de fallbacktest.")
        self.reporter.summary_generating()
        client.summary_generate()
        deadline = time.time() + 10.0
        while time.time() < deadline:
            llm_payload = client.summary_get()
            session = llm_payload.get("session") if isinstance(llm_payload.get("session"), dict) else {}
            recovery = session.get("recovery") if isinstance(session.get("recovery"), dict) else {}
            if str(recovery.get("stage") or "").strip().lower() == "llm":
                break
            time.sleep(0.05)
        else:
            raise ScenarioFailure("Summary LLM recovery state did not appear.")

        self.reporter.fallback_stage("llm")
        client.summary_execution_mode("local")
        self.reporter.fallback_action("samenvatting opnieuw genereren in lokale modus.")
        self.reporter.summary_generating()
        client.summary_generate()
        generated = self._wait_for_summary_status(
            client,
            "summary_review",
            timeout_s=self._summary_generate_timeout_s(
                execution_mode="local",
                instance=self.summary_target,
            ),
        )
        generated_session = generated.get("session") if isinstance(generated.get("session"), dict) else {}
        if not str(generated_session.get("draft_text") or "").strip():
            raise ScenarioFailure("Summary draft missing after LLM recovery.")
        self.reporter.summary_generated()

        self.fault_injector.plan_failure("tts", times=2)
        self.reporter.info("TTS-service wordt bewust onderbroken voor de fallbacktest.")
        try:
            client.summary_publish()
        except DMClientError:
            pass
        tts_payload = client.summary_get()
        tts_session = tts_payload.get("session") if isinstance(tts_payload.get("session"), dict) else {}
        tts_recovery = tts_session.get("recovery") if isinstance(tts_session.get("recovery"), dict) else {}
        if str(tts_recovery.get("stage") or "").strip().lower() != "tts":
            raise ScenarioFailure("Summary TTS recovery state did not appear.")
        self.reporter.fallback_stage("tts")
        self.reporter.fallback_action("summary afronden zonder publish.")
        completed = client.summary_complete_without_publish()
        completed_session = completed.get("session") if isinstance(completed.get("session"), dict) else {}
        if str(completed_session.get("status") or "") != "completed":
            raise ScenarioFailure(f"Expected completed summary after TTS recovery, got {completed_session!r}")
        self.reporter.summary_completed_without_publish()
        result = running.wait(timeout_s=20.0)
        if int(result.completed_steps) != int(result.total_steps):
            raise ScenarioFailure(f"Service-loss summary script did not complete all steps: {result}")
        self.reporter.summary_runner_completed()

    def _run_workshop_script(self) -> RunResult:
        running = self._new_runner(self.workshop_script, name="full_demo_rehearsal")
        self.reporter.workshop_started()
        self._start_runner(running, preflight=True, auto_continue_manual=True)
        try:
            event = self._wait_for_summary_wait(running, timeout_s=25.0)
        except ScenarioFailure:
            result = running.wait(timeout_s=45.0)
            self.reporter.workshop_completed()
            return result
        if not bool(event.get("summary_waiting")):
            raise ScenarioFailure("Workshop script did not enter summary waiting state.")
        self.reporter.workshop_waiting_for_summary()
        summary_instance = self._summary_instance_for_event(event, fallback=self.summary_target)
        summary_client = summary_instance.client
        self.replay_registry.clear_instance(summary_instance.instance_id)
        self._wait_for_summary_status(summary_client, "capturing", timeout_s=15.0)
        self.reporter.summary_capturing()
        self._drive_summary_edit_flow(client=summary_client, instance=summary_instance)
        result = running.wait(timeout_s=60.0)
        self.reporter.workshop_completed()
        return result

    def _preflight_live_robot(self) -> None:
        for instance in self.instances.values():
            health = instance.client.runtime_health(self._live_robot_runtime_payload(instance))
            if not health.get("ok"):
                raise ScenarioFailure(f"Runtime health failed for {instance.base_url}: {health}")
            nao_state = instance.client.nao_command_state()
            if not bool(nao_state.get("reachable")):
                raise ScenarioFailure(f"Robot not reachable for {instance.base_url}: {nao_state}")
            auto_rest = nao_state.get("auto_rest") if isinstance(nao_state.get("auto_rest"), dict) else {}
            if bool(auto_rest.get("suspended")):
                raise ScenarioFailure(f"Auto-rest is already suspended before rehearsal: {nao_state}")

    def run_full_demo_rehearsal(self) -> None:
        live_robot = _is_live_robot_profile(self.profile)
        if live_robot:
            self.reporter.phase("Voorcontrole: live robot verbinding controleren.")
            self._preflight_live_robot()
        self.reporter.phase("Fase 1: chatflow")
        self.run_happy_path_dialog()
        self.reporter.phase("Fase 2: summaryflow")
        self.run_summary_edit_flow()
        self.reporter.phase("Fase 3: workshopscript")
        result = self._run_workshop_script()
        if int(result.completed_steps) != int(result.total_steps):
            raise ScenarioFailure(f"Workshop script did not complete all steps: {result}")

        self.reporter.phase("Fase 4: eindcontroles")
        client = self._prepare_dialog_client()
        self.reporter.continuous_listening_started()
        client.continuous_start()
        try:
            self._dialog_step(client, self.primary, "hoi", expect_reply_only=True)
            self._dialog_step(
                client,
                self.primary,
                "sta_op",
                expect_command="STAND_UP",
                expect_mode="PERFORMING",
                expect_behavior="STAND_UP",
                post_wait_s=LIVE_ROBOT_SETTLE_STAND_UP_S if live_robot else 0.0,
            )
            self._dialog_step(
                client,
                self.primary,
                "motoren_uit",
                expect_command="REST",
                expect_mode="PERFORMING",
                expect_behavior="REST",
                post_wait_s=LIVE_ROBOT_SETTLE_REST_S if live_robot else 0.0,
            )
        finally:
            client.continuous_stop()
            self.reporter.continuous_listening_stopped()

        summary_payload = self.summary_target.client.summary_get()
        summary_session = summary_payload.get("session") if isinstance(summary_payload.get("session"), dict) else {}
        status = str(summary_session.get("status") or "").strip().lower()
        if status not in {"completed", "idle", "aborted", "error"}:
            raise ScenarioFailure(f"Summary session left active after rehearsal: {summary_session}")
        state = client.state()
        if bool(state.get("command_stop_available")):
            raise ScenarioFailure(f"Command stop scope still active after rehearsal: {state}")
        if live_robot:
            for instance in self.instances.values():
                health = instance.client.runtime_health(
                    self._live_robot_runtime_payload(instance)
                )
                auto_rest = health.get("auto_rest") if isinstance(health.get("auto_rest"), dict) else {}
                if bool(auto_rest.get("suspended")):
                    raise ScenarioFailure(f"Auto-rest suspend lease still active after rehearsal: {health}")
                renew_failures = list(instance.client.dm_events(event="auto_rest_suspend_renew_failed").get("events") or [])
                if renew_failures:
                    raise ScenarioFailure(f"Auto-rest renew failures occurred during rehearsal: {renew_failures[-1]}")
        self.reporter.info("Rehearsal compleet; alle eindcontroles zijn geslaagd.")

    def run_scenario(self, scenario: str) -> ScenarioResult:
        started = time.perf_counter()
        scenario_name = str(scenario or "").strip().lower()
        if scenario_name not in SCENARIOS:
            raise ValueError(f"Unsupported scenario: {scenario!r}")
        detail = ""
        self.reporter.scenario_start(scenario_name)
        try:
            self.fault_injector.clear()
            if scenario_name == SCENARIO_HAPPY_PATH:
                self.run_happy_path_dialog()
            elif scenario_name == SCENARIO_SUMMARY_EDIT:
                self.run_summary_edit_flow()
            elif scenario_name == SCENARIO_SERVICE_LOSS:
                self.run_service_loss_recovery()
            elif scenario_name == SCENARIO_FULL_REHEARSAL:
                self.run_full_demo_rehearsal()
            passed = True
        except Exception as exc:
            passed = False
            detail = str(exc)
        return ScenarioResult(
            profile=self.profile,
            scenario=scenario_name,
            passed=passed,
            duration_s=time.perf_counter() - started,
            detail=detail,
        )


def _scenario_selection(raw_value: str) -> List[str]:
    value = str(raw_value or "").strip().lower()
    if value == "all":
        return list(SCENARIOS)
    if value not in SCENARIOS:
        raise ValueError(f"Unsupported scenario: {raw_value!r}")
    return [value]


def _profile_selection(raw_value: str) -> List[str]:
    value = str(raw_value or "").strip().lower()
    if value == "all":
        return list(PROFILES)
    if value not in PROFILES:
        raise ValueError(f"Unsupported profile: {raw_value!r}")
    return [value]


def _scenarios_for_profile(profile: str, requested: Sequence[str]) -> List[str]:
    del profile
    items: List[str] = []
    seen: set[str] = set()
    for raw in requested or []:
        name = str(raw or "").strip().lower()
        if not name or name in seen:
            continue
        seen.add(name)
        items.append(name)
    if SCENARIO_FULL_REHEARSAL in items:
        items = [
            name
            for name in items
            if name not in {SCENARIO_HAPPY_PATH, SCENARIO_SUMMARY_EDIT}
        ]
    return items


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run opt-in DM/SR demo-gate rehearsal scenarios.")
    parser.add_argument("--profile", default="offline", help="offline|live_services|live_robot|all")
    parser.add_argument("--scenario", default="all", help="happy_path_dialog|summary_edit_flow|service_loss_recovery|full_demo_rehearsal|all")
    parser.add_argument("--nao-ip", dest="nao_ip", default=None, help="Override NAO IP for live_robot.")
    parser.add_argument("--runtime-preset", default=None, help="Runtime preset path or preset name.")
    parser.add_argument("--summary-preset-id", default=None, help="Summary preset id from summary_presets.json.")
    parser.add_argument("--summary-script", default=str(DEFAULT_SUMMARY_SCRIPT), help="Path to the summary script JSON.")
    parser.add_argument("--workshop-script", default=str(DEFAULT_WORKSHOP_SCRIPT), help="Path to the workshop script JSON.")
    parser.add_argument(
        "--audio-fixtures-root",
        default=str(DEFAULT_AUDIO_FIXTURES_ROOT),
        help="Directory containing golden WAV replay fixtures.",
    )
    parser.add_argument("--keep-artifacts", action="store_true", help="Keep runtime/session artifacts on success.")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_argument_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    profiles = _profile_selection(args.profile)
    scenarios = _scenario_selection(args.scenario)
    summary_script = Path(args.summary_script)
    workshop_script = Path(args.workshop_script)
    fixtures_root = Path(args.audio_fixtures_root)
    if not summary_script.exists():
        raise SystemExit(f"Summary script not found: {summary_script}")
    if not workshop_script.exists():
        raise SystemExit(f"Workshop script not found: {workshop_script}")
    if not fixtures_root.exists():
        raise SystemExit(f"Audio fixtures directory not found: {fixtures_root}")

    failures: List[ScenarioResult] = []
    for profile in profiles:
        profile_scenarios = _scenarios_for_profile(profile, scenarios)
        if not profile_scenarios:
            continue
        for scenario in profile_scenarios:
            harness = DemoGateHarness(
                profile=profile,
                runtime_preset=args.runtime_preset,
                summary_preset_id=args.summary_preset_id,
                summary_script_path=summary_script,
                workshop_script_path=workshop_script,
                fixtures_root=fixtures_root,
                nao_ip=args.nao_ip,
                keep_artifacts=bool(args.keep_artifacts),
            )
            harness.reporter.run_start(profile=profile, scenario=scenario)
            harness.reporter.startup_preparing()
            keep_artifacts = bool(args.keep_artifacts)
            try:
                harness.setup()
            except Exception as exc:
                detail = str(exc).strip() or exc.__class__.__name__
                harness.reporter.setup_failure(scenario=scenario, detail=detail)
                failures.append(
                    ScenarioResult(
                        profile=profile,
                        scenario=scenario,
                        passed=False,
                        duration_s=0.0,
                        detail=detail,
                    )
                )
                break
            try:
                result = harness.run_scenario(scenario)
                if not result.passed:
                    harness.reporter.scenario_failure(result)
                    failures.append(result)
                    keep_artifacts = True
                    break
                harness.reporter.scenario_success(result, keep_artifacts=keep_artifacts)
            finally:
                harness.close(keep_artifacts=keep_artifacts)
            if failures:
                break

    if failures:
        print("Demo-gate mislukt.")
        for result in failures:
            operator_name = SCENARIO_OPERATOR_NAME.get(result.scenario, result.scenario)
            print(f"Mislukt scenario: {operator_name} (profiel={result.profile}). {result.detail}")
        return 1
    print("Demo-gate klaar: alle gevraagde scenario's zijn geslaagd.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
