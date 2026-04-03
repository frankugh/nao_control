from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from types import SimpleNamespace

from dialog.interfaces import LLMResult

import webapp_server


class StubLLMBackend:
    def __init__(self, *, reply: str = "ok", error: Exception | None = None) -> None:
        self.reply = reply
        self.error = error
        self.calls = []

    def generate(self, messages):
        self.calls.append(list(messages))
        if self.error is not None:
            raise self.error
        return LLMResult(reply=self.reply, messages=list(messages))


class StubSTTBackend:
    def __init__(self, texts):
        self._texts = list(texts)

    def transcribe(self, _audio):
        text = self._texts.pop(0) if self._texts else ""
        return SimpleNamespace(text=text, language="nl", confidence=0.9)


class PlannedSTTBackend:
    def __init__(self, outcomes):
        self._outcomes = list(outcomes)

    def transcribe(self, _audio):
        item = self._outcomes.pop(0) if self._outcomes else ""
        if isinstance(item, Exception):
            raise item
        return SimpleNamespace(text=str(item or ""), language="nl", confidence=0.9)


class StubOutput:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.emitted = []

    def emit(self, text):
        if self.error is not None:
            raise self.error
        self.emitted.append(text)
        return True

    def last_error_message(self):
        return ""


class PlannedLLMBackend:
    def __init__(self, replies):
        self._replies = list(replies)
        self.calls = []

    def generate(self, messages):
        self.calls.append(list(messages))
        reply = self._replies.pop(0) if self._replies else "ok"
        if isinstance(reply, Exception):
            raise reply
        return LLMResult(reply=str(reply), messages=list(messages))


def _summary_state_root(tmp_path: Path, instance_id: str) -> Path:
    safe = re.sub(r"[^a-zA-Z0-9_.-]+", "_", instance_id) or "default"
    return tmp_path / "state" / f"summary_{safe}"


def _summary_config() -> dict:
    return {
        "selected_preset_id": None,
        "devices": {
            "input_audio_device": None,
            "output_audio_device": None,
        },
        "models": {
            "stt_primary": "whisper:small",
            "stt_fallback": "vosk:default",
            "llm_primary": "echo:default",
            "llm_fallback": "ollama_local:gemma:2b",
            "tts_primary": "piper:default",
            "tts_fallback": "nao_native:default",
        },
        "prompts": {
            "master_prompt_file": "summary_prompts/workshop_summary_system.txt",
            "master_prompt_text": "",
            "summary_instruction": "Maak een compacte samenvatting.",
            "input_prompt_template": "Transcript:\n{transcript}\n\nOpdracht:\n{instruction}",
        },
        "fallbacks": {
            "fallback_summary": "Fallback summary.",
        },
        "advanced": {
            "pre_roll_ms": int(webapp_server._SUMMARY_CAPTURE_MIN_PRE_ROLL_MS),
            "stop_silence_ms": int(webapp_server._SUMMARY_CAPTURE_MIN_STOP_SILENCE_MS),
            "min_speech_ms": None,
            "max_utterance_s": None,
            "start_timeout_s": None,
        },
    }


def _make_app(
    monkeypatch,
    tmp_path: Path,
    *,
    stt_backend=None,
    llm_backend=None,
    output_backend=None,
    instance_id: str = "demo",
    cfg=None,
    built_pipeline_cfgs=None,
    repo_root_override: Path | None = None,
):
    if repo_root_override is not None:
        prompts_dir = repo_root_override / "py3_dialog_manager" / "configs" / "summary_prompts"
        prompts_dir.mkdir(parents=True, exist_ok=True)
        (prompts_dir / "workshop_summary_system.txt").write_text("SYSTEM", encoding="utf-8")
        monkeypatch.setattr(
            webapp_server,
            "__file__",
            str(repo_root_override / "py3_dialog_manager" / "webapp_server.py"),
        )
    base_pipeline = SimpleNamespace(
        llm=llm_backend or StubLLMBackend(),
        output=output_backend or StubOutput(),
        status_to_console=True,
        system_prompt="SYSTEM",
        log_messages_path=None,
        log_meta={},
        max_history_turns=None,
        _behavior_executor=None,
        _cmdrec=None,
        _debug_cmdrec=False,
    )
    def _build_pipeline(cfg_obj, *_a, **_k):
        if built_pipeline_cfgs is not None:
            built_pipeline_cfgs.append(json.loads(json.dumps(cfg_obj)))
        return base_pipeline

    monkeypatch.setattr(webapp_server, "build_pipeline_from_config", _build_pipeline)
    monkeypatch.setattr(
        webapp_server,
        "make_stt_backend_from_config",
        lambda *_a, **_k: stt_backend or StubSTTBackend([]),
    )
    app, _, _ = webapp_server.create_app(
        cfg=cfg or {"output": {"target": "server"}},
        config_path=str(tmp_path / "cfg.json"),
        instance_id=instance_id,
        runtime_state_dir=str(tmp_path / "state"),
    )
    return app


def _reset_summary_cloud_probe_state(monkeypatch, *, interval_s: float = 0.05, timeout_s: float = 0.05) -> None:
    if hasattr(webapp_server, "_SUMMARY_CLOUD_PROBE_INTERVAL_S"):
        monkeypatch.setattr(webapp_server, "_SUMMARY_CLOUD_PROBE_INTERVAL_S", float(interval_s))
    if hasattr(webapp_server, "_SUMMARY_CLOUD_PROBE_TIMEOUT_S"):
        monkeypatch.setattr(webapp_server, "_SUMMARY_CLOUD_PROBE_TIMEOUT_S", float(timeout_s))
    if hasattr(webapp_server, "_summary_cloud_probe_lock") and hasattr(webapp_server, "_summary_cloud_probe_next_allowed"):
        with webapp_server._summary_cloud_probe_lock:
            webapp_server._summary_cloud_probe_next_allowed.clear()


def _wait_for_summary_session(client, predicate, *, timeout_s: float = 1.5):
    deadline = time.time() + float(timeout_s)
    latest = None
    while time.time() < deadline:
        resp = client.get("/api/summary")
        assert resp.status_code == 200
        latest = resp.get_json()["session"]
        if predicate(latest):
            return latest
        time.sleep(0.02)
    assert latest is not None
    raise AssertionError(f"summary session did not reach expected state before timeout; latest={latest!r}")


def _wait_for_summary_status(client, status: str, *, timeout_s: float = 1.5):
    target = str(status or "").strip().lower()
    return _wait_for_summary_session(
        client,
        lambda session: str((session or {}).get("status") or "").strip().lower() == target,
        timeout_s=timeout_s,
    )


def test_summary_config_persists_per_instance(monkeypatch, tmp_path: Path):
    app = _make_app(monkeypatch, tmp_path, instance_id="demo_5301")
    client = app.test_client()
    cfg = _summary_config()
    cfg["models"]["tts_primary"] = "piper:custom_model.onnx"
    cfg["prompts"]["summary_instruction"] = "Vat dit rustig samen."

    resp = client.post(
        "/api/summary/config",
        json={"config": cfg},
    )
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["config"]["models"]["tts_primary"] == "piper:custom_model.onnx"
    assert payload["config"]["prompts"]["summary_instruction"] == "Vat dit rustig samen."
    config_path = _summary_state_root(tmp_path, "demo_5301") / "summary_runtime.config.json"
    assert config_path.exists()
    stored = json.loads(config_path.read_text(encoding="utf-8"))
    assert stored["models"]["tts_primary"] == "piper:custom_model.onnx"

    app2 = _make_app(monkeypatch, tmp_path, instance_id="demo_5301")
    client2 = app2.test_client()
    cfg_resp = client2.get("/api/summary/config")
    assert cfg_resp.status_code == 200
    cfg = cfg_resp.get_json()["config"]
    assert cfg["models"]["tts_primary"] == "piper:custom_model.onnx"
    assert cfg["prompts"]["summary_instruction"] == "Vat dit rustig samen."


def test_summary_legacy_config_migrates_on_load(monkeypatch, tmp_path: Path):
    root = _summary_state_root(tmp_path, "demo")
    root.mkdir(parents=True, exist_ok=True)
    (root / "config.json").write_text(
        json.dumps(
            {
                "runtime_overrides": {
                    "stt_type": "azure",
                    "llm_type": "ollama_cloud",
                    "llm_model": "gpt-oss:120b-cloud",
                    "output_target": "server",
                    "tts_engine": "piper",
                },
                "system_prompt_file": "summary_prompts/workshop_summary_system.txt",
            }
        ),
        encoding="utf-8",
    )

    app = _make_app(monkeypatch, tmp_path)
    client = app.test_client()
    resp = client.get("/api/summary/config")
    assert resp.status_code == 200
    cfg = resp.get_json()["config"]
    assert cfg["models"]["stt_primary"] == "azure:default"
    assert cfg["models"]["llm_primary"] == "ollama_cloud:gpt-oss:120b-cloud"
    assert cfg["prompts"]["master_prompt_file"] == "summary_prompts/workshop_summary_system.txt"


def test_summary_prompt_content_reads_summary_prompt_namespace(monkeypatch, tmp_path: Path):
    app = _make_app(monkeypatch, tmp_path)
    client = app.test_client()

    resp = client.get("/api/summary_prompt_content?name=summary_prompts/workshop_summary_system.txt")
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["ok"] is True
    assert payload["name"] == "summary_prompts/workshop_summary_system.txt"
    assert "workshop-assistent" in payload["content"]


def test_summary_prompt_save_writes_summary_prompt_file(monkeypatch, tmp_path: Path):
    repo_root = tmp_path / "repo_root"
    app = _make_app(monkeypatch, tmp_path, repo_root_override=repo_root)
    client = app.test_client()

    resp = client.post(
        "/api/summary_prompt_save",
        json={
            "save_as": "nieuw_summary_prompt.txt",
            "content": "Hallo prompt",
        },
    )
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["ok"] is True
    assert payload["name"] == "summary_prompts/nieuw_summary_prompt.txt"
    assert "summary_prompts/nieuw_summary_prompt.txt" in payload["prompts"]
    saved_path = repo_root / "py3_dialog_manager" / "configs" / "summary_prompts" / "nieuw_summary_prompt.txt"
    assert saved_path.read_text(encoding="utf-8") == "Hallo prompt"


def test_summary_start_creates_persistent_active_session_and_relaxed_capture_defaults(monkeypatch, tmp_path: Path):
    app = _make_app(monkeypatch, tmp_path)
    client = app.test_client()
    seen = {}

    class FakeMic:
        def capture_utterance(self, timeout_s=10.0):
            time.sleep(0.01)
            raise TimeoutError("no speech")

    def _fake_make_mic(cfg, **kwargs):
        seen["cfg"] = cfg
        seen["mode"] = kwargs.get("mode")
        return FakeMic()

    monkeypatch.setattr(webapp_server, "_make_mic_from_config", _fake_make_mic)

    resp = client.post("/api/summary/start")
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["session"]["status"] == "capturing"
    assert seen["mode"] == "continuous"
    cont = (((seen["cfg"] or {}).get("input") or {}).get("mic") or {}).get("params_continuous") or {}
    assert int(cont.get("stop_silence_ms", 0)) >= int(webapp_server._SUMMARY_CAPTURE_MIN_STOP_SILENCE_MS)
    assert int(cont.get("pre_roll_ms", 0)) >= int(webapp_server._SUMMARY_CAPTURE_MIN_PRE_ROLL_MS)

    active_path = _summary_state_root(tmp_path, "demo") / "active_session.json"
    assert active_path.exists()
    active = json.loads(active_path.read_text(encoding="utf-8"))
    assert active["status"] == "capturing"

    assert client.post("/api/summary/abort").status_code == 200


def test_summary_start_returns_409_while_session_active(monkeypatch, tmp_path: Path):
    app = _make_app(monkeypatch, tmp_path)
    client = app.test_client()

    class FakeMic:
        def capture_utterance(self, timeout_s=10.0):
            time.sleep(0.01)
            raise TimeoutError("no speech")

    monkeypatch.setattr(webapp_server, "_make_mic_from_config", lambda *_a, **_k: FakeMic())

    assert client.post("/api/summary/start").status_code == 200
    second = client.post("/api/summary/start")
    assert second.status_code == 409
    assert second.get_json()["error"] == "summary_session_already_active"
    assert client.post("/api/summary/abort").status_code == 200


def test_summary_options_returns_device_markers_and_prompt_list(monkeypatch, tmp_path: Path):
    fake_sd = SimpleNamespace(
        default=SimpleNamespace(hostapi=0, device=[3, 4]),
        query_devices=lambda: [
            {"name": "Input A", "hostapi": 0, "max_input_channels": 2, "max_output_channels": 0},
            {"name": "Output A", "hostapi": 0, "max_input_channels": 0, "max_output_channels": 2},
            {"name": "Input B", "hostapi": 0, "max_input_channels": 2, "max_output_channels": 0},
            {"name": "Chat Mic", "hostapi": 0, "max_input_channels": 2, "max_output_channels": 0},
            {"name": "Chat Speaker", "hostapi": 0, "max_input_channels": 0, "max_output_channels": 2},
        ],
    )
    monkeypatch.setattr(webapp_server, "sd", fake_sd)
    app = _make_app(
        monkeypatch,
        tmp_path,
        cfg={
            "input": {
                "type": "audio",
                "params": {},
                "mic": {"type": "laptop", "params": {"input_device": 3}},
                "stt": {"type": "whisper", "params": {"model_name": "small"}},
            },
            "llm": {"type": "echo"},
            "output": {"type": "router", "target": "server", "engine": "piper", "device": 4, "params": {}},
        },
    )
    client = app.test_client()
    resp = client.get("/api/summary/options")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["default_config"]["prompts"]["master_prompt_file"] == "summary_prompts/workshop_summary_system.txt"
    assert data["chat_devices"]["input_audio_device"] == 3
    assert data["chat_devices"]["output_audio_device"] == 4
    assert any(item == "summary_prompts/workshop_summary_system.txt" for item in data["master_prompts"])
    assert data["input_devices"][0]["value"] is None
    assert "Default mic" in data["input_devices"][0]["label"]
    assert data["output_devices"][0]["value"] is None
    assert "Default speakers" in data["output_devices"][0]["label"]
    assert not any(item["value"] == "nao_ssh" for item in data["input_devices"])
    assert not any(item["value"] == "nao_speaker" for item in data["output_devices"])
    assert "stt_primary" in data["model_suggestions"]
    assert "llm_primary" in data["model_suggestions"]
    assert "tts_primary" in data["model_suggestions"]
    assert "capabilities" in data


def test_summary_stop_empty_transcript_keeps_review_state(monkeypatch, tmp_path: Path):
    app = _make_app(monkeypatch, tmp_path)
    client = app.test_client()

    class FakeMic:
        def capture_utterance(self, timeout_s=10.0):
            time.sleep(0.01)
            raise TimeoutError("no speech")

    monkeypatch.setattr(webapp_server, "_make_mic_from_config", lambda *_a, **_k: FakeMic())

    assert client.post("/api/summary/start").status_code == 200
    time.sleep(0.05)
    stop_resp = client.post("/api/summary/stop")
    assert stop_resp.status_code == 200
    session = stop_resp.get_json()["session"]
    assert session["status"] == "transcript_review"
    assert session["transcript_text"] == ""
    assert int(session["capture_stats"]["transcript_count"]) == 0
    assert client.post("/api/summary/abort").status_code == 200


def test_summary_transcript_edit_and_generate_success(monkeypatch, tmp_path: Path):
    app = _make_app(monkeypatch, tmp_path, stt_backend=StubSTTBackend(["eerste regel"]))
    client = app.test_client()

    class FakeMic:
        def __init__(self) -> None:
            self.calls = 0

        def capture_utterance(self, timeout_s=10.0):
            self.calls += 1
            if self.calls == 1:
                return SimpleNamespace(pcm=b"wav", sample_rate=16000, channels=1, sample_width=2)
            time.sleep(0.01)
            raise TimeoutError("no speech")

    monkeypatch.setattr(webapp_server, "_make_mic_from_config", lambda *_a, **_k: FakeMic())

    assert client.post("/api/summary/start").status_code == 200
    time.sleep(0.05)
    assert client.post("/api/summary/stop").status_code == 200
    edit_resp = client.post("/api/summary/transcript", json={"transcript_text": "regel 1\n\nregel 2"})
    assert edit_resp.status_code == 200
    session = edit_resp.get_json()["session"]
    assert [line["text"] for line in session["transcript_lines"]] == ["regel 1", "regel 2"]

    gen_resp = client.post("/api/summary/generate")
    assert gen_resp.status_code == 200
    gen_session = _wait_for_summary_status(client, "summary_review")
    assert gen_session["status"] == "summary_review"
    assert gen_session["draft_text"] == "ok"
    assert gen_session["draft_meta"]["system_prompt_name"] == "summary_prompts/workshop_summary_system.txt"


def test_summary_generate_failure_reverts_to_transcript_review(monkeypatch, tmp_path: Path):
    app = _make_app(
        monkeypatch,
        tmp_path,
        stt_backend=StubSTTBackend(["tekst"]),
        llm_backend=StubLLMBackend(error=RuntimeError("llm boom")),
    )
    client = app.test_client()

    class FakeMic:
        def __init__(self) -> None:
            self.calls = 0

        def capture_utterance(self, timeout_s=10.0):
            self.calls += 1
            if self.calls == 1:
                return SimpleNamespace(pcm=b"wav", sample_rate=16000, channels=1, sample_width=2)
            time.sleep(0.01)
            raise TimeoutError("no speech")

    monkeypatch.setattr(webapp_server, "_make_mic_from_config", lambda *_a, **_k: FakeMic())

    assert client.post("/api/summary/start").status_code == 200
    time.sleep(0.05)
    assert client.post("/api/summary/stop").status_code == 200
    gen_resp = client.post("/api/summary/generate")
    assert gen_resp.status_code == 200
    session = _wait_for_summary_status(client, "transcript_review")
    assert session["status"] == "transcript_review"
    assert session["last_error_stage"] == "generate"


def test_summary_publish_success_and_save_archive(monkeypatch, tmp_path: Path):
    output = StubOutput()
    app = _make_app(monkeypatch, tmp_path, stt_backend=StubSTTBackend(["tekst"]), output_backend=output)
    client = app.test_client()

    class FakeMic:
        def __init__(self) -> None:
            self.calls = 0

        def capture_utterance(self, timeout_s=10.0):
            self.calls += 1
            if self.calls == 1:
                return SimpleNamespace(pcm=b"wav", sample_rate=16000, channels=1, sample_width=2)
            time.sleep(0.01)
            raise TimeoutError("no speech")

    monkeypatch.setattr(webapp_server, "_make_mic_from_config", lambda *_a, **_k: FakeMic())

    assert client.post("/api/summary/start").status_code == 200
    time.sleep(0.05)
    assert client.post("/api/summary/stop").status_code == 200
    assert client.post("/api/summary/generate").status_code == 200
    _wait_for_summary_status(client, "summary_review")

    publish_resp = client.post("/api/summary/publish")
    assert publish_resp.status_code == 200
    session = publish_resp.get_json()["session"]
    assert session["status"] == "completed"
    assert output.emitted == ["ok"]

    save_resp = client.post("/api/summary/save")
    assert save_resp.status_code == 200
    saved_meta = save_resp.get_json()["session"]["save_meta"]
    assert saved_meta["saved_at"]
    assert Path(saved_meta["path"]).exists()
    assert Path(saved_meta["text_path"]).exists()
    assert saved_meta["download_url"].endswith(".txt")
    assert saved_meta["filename"] == "summary.txt"
    txt_resp = client.get(saved_meta["download_url"])
    assert txt_resp.status_code == 200
    assert txt_resp.get_data(as_text=True).strip() == "ok"


def test_summary_publish_inherits_chat_output_when_device_is_null(monkeypatch, tmp_path: Path):
    built_cfgs = []
    app = _make_app(
        monkeypatch,
        tmp_path,
        stt_backend=StubSTTBackend(["tekst"]),
        cfg={
            "output": {
                "type": "router",
                "target": "nao",
                "engine": "piper",
                "device": None,
                "params": {},
            }
        },
        built_pipeline_cfgs=built_cfgs,
    )
    client = app.test_client()
    summary_cfg = _summary_config()
    summary_cfg["devices"]["output_audio_device"] = None
    assert client.post("/api/summary/config", json={"config": summary_cfg}).status_code == 200

    class FakeMic:
        def __init__(self) -> None:
            self.calls = 0

        def capture_utterance(self, timeout_s=10.0):
            self.calls += 1
            if self.calls == 1:
                return SimpleNamespace(pcm=b"wav", sample_rate=16000, channels=1, sample_width=2)
            time.sleep(0.01)
            raise TimeoutError("no speech")

    monkeypatch.setattr(webapp_server, "_make_mic_from_config", lambda *_a, **_k: FakeMic())

    assert client.post("/api/summary/start").status_code == 200
    time.sleep(0.05)
    assert client.post("/api/summary/stop").status_code == 200
    assert client.post("/api/summary/generate").status_code == 200
    _wait_for_summary_status(client, "summary_review")
    publish_resp = client.post("/api/summary/publish")
    assert publish_resp.status_code == 502

    output_cfg = built_cfgs[-1]["output"]
    assert output_cfg["params"]["target"] == "nao"
    assert output_cfg["params"]["output_device"] is None


def test_summary_publish_explicit_output_device_overrides_chat_output(monkeypatch, tmp_path: Path):
    built_cfgs = []
    class FakeSoundDevice:
        default = SimpleNamespace(hostapi=0, device=[None, 11])

        @staticmethod
        def query_devices():
            raw = []
            for idx in range(12):
                raw.append(
                    {
                        "name": f"Device {idx}",
                        "hostapi": 0,
                        "max_input_channels": 0,
                        "max_output_channels": 2 if idx == 11 else 0,
                    }
                )
            return raw

    def _raise_probe_error(*_a, **_k):
        raise RuntimeError("skip probe snapshot")

    monkeypatch.setattr(webapp_server, "sd", FakeSoundDevice())
    monkeypatch.setattr(webapp_server.subprocess, "check_output", _raise_probe_error)
    app = _make_app(
        monkeypatch,
        tmp_path,
        stt_backend=StubSTTBackend(["tekst"]),
        cfg={
            "output": {
                "type": "router",
                "target": "nao",
                "engine": "piper",
                "device": None,
                "params": {},
            }
        },
        built_pipeline_cfgs=built_cfgs,
    )
    client = app.test_client()
    summary_cfg = _summary_config()
    summary_cfg["devices"]["output_audio_device"] = 11
    assert client.post("/api/summary/config", json={"config": summary_cfg}).status_code == 200

    class FakeMic:
        def __init__(self) -> None:
            self.calls = 0

        def capture_utterance(self, timeout_s=10.0):
            self.calls += 1
            if self.calls == 1:
                return SimpleNamespace(pcm=b"wav", sample_rate=16000, channels=1, sample_width=2)
            time.sleep(0.01)
            raise TimeoutError("no speech")

    monkeypatch.setattr(webapp_server, "_make_mic_from_config", lambda *_a, **_k: FakeMic())

    assert client.post("/api/summary/start").status_code == 200
    time.sleep(0.05)
    assert client.post("/api/summary/stop").status_code == 200
    assert client.post("/api/summary/generate").status_code == 200
    _wait_for_summary_status(client, "summary_review")
    assert client.post("/api/summary/publish").status_code == 200

    output_cfg = built_cfgs[-1]["output"]
    assert output_cfg["params"]["target"] == "server"
    assert output_cfg["params"]["output_device"] == 11


def test_summary_publish_unavailable_output_device_falls_back_to_chat_output(monkeypatch, tmp_path: Path):
    built_cfgs = []
    monkeypatch.setattr(webapp_server, "sd", None)
    app = _make_app(
        monkeypatch,
        tmp_path,
        stt_backend=StubSTTBackend(["tekst"]),
        cfg={
            "output": {
                "type": "router",
                "target": "nao",
                "engine": "azure",
                "device": None,
                "params": {},
            }
        },
        built_pipeline_cfgs=built_cfgs,
    )
    client = app.test_client()
    summary_cfg = _summary_config()
    summary_cfg["devices"]["output_audio_device"] = 4
    cfg_resp = client.post("/api/summary/config", json={"config": summary_cfg})
    assert cfg_resp.status_code == 200
    assert cfg_resp.get_json()["config"]["devices"]["output_audio_device"] is None

    class FakeMic:
        def __init__(self) -> None:
            self.calls = 0

        def capture_utterance(self, timeout_s=10.0):
            self.calls += 1
            if self.calls == 1:
                return SimpleNamespace(pcm=b"wav", sample_rate=16000, channels=1, sample_width=2)
            time.sleep(0.01)
            raise TimeoutError("no speech")

    monkeypatch.setattr(webapp_server, "_make_mic_from_config", lambda *_a, **_k: FakeMic())

    assert client.post("/api/summary/start").status_code == 200
    time.sleep(0.05)
    assert client.post("/api/summary/stop").status_code == 200
    assert client.post("/api/summary/generate").status_code == 200
    _wait_for_summary_status(client, "summary_review")
    publish_resp = client.post("/api/summary/publish")
    assert publish_resp.status_code == 502

    output_cfg = built_cfgs[-1]["output"]
    assert output_cfg["params"]["target"] == "nao"
    assert output_cfg["params"]["output_device"] is None


def test_summary_publish_failure_reverts_to_summary_review(monkeypatch, tmp_path: Path):
    output = StubOutput(error=RuntimeError("tts boom"))
    app = _make_app(monkeypatch, tmp_path, stt_backend=StubSTTBackend(["tekst"]), output_backend=output)
    client = app.test_client()

    class FakeMic:
        def __init__(self) -> None:
            self.calls = 0

        def capture_utterance(self, timeout_s=10.0):
            self.calls += 1
            if self.calls == 1:
                return SimpleNamespace(pcm=b"wav", sample_rate=16000, channels=1, sample_width=2)
            time.sleep(0.01)
            raise TimeoutError("no speech")

    monkeypatch.setattr(webapp_server, "_make_mic_from_config", lambda *_a, **_k: FakeMic())

    assert client.post("/api/summary/start").status_code == 200
    time.sleep(0.05)
    assert client.post("/api/summary/stop").status_code == 200
    assert client.post("/api/summary/generate").status_code == 200
    _wait_for_summary_status(client, "summary_review")
    publish_resp = client.post("/api/summary/publish")
    assert publish_resp.status_code == 502
    session = publish_resp.get_json()["session"]
    assert session["status"] == "summary_review"
    assert session["last_error_stage"] == "publish"


def test_summary_publish_output_disabled_returns_clear_tts_message(monkeypatch, tmp_path: Path):
    output = StubOutput(error=RuntimeError("output disabled"))
    app = _make_app(monkeypatch, tmp_path, stt_backend=StubSTTBackend(["tekst"]), output_backend=output)
    client = app.test_client()

    class FakeMic:
        def __init__(self) -> None:
            self.calls = 0

        def capture_utterance(self, timeout_s=10.0):
            self.calls += 1
            if self.calls == 1:
                return SimpleNamespace(pcm=b"wav", sample_rate=16000, channels=1, sample_width=2)
            time.sleep(0.01)
            raise TimeoutError("no speech")

    monkeypatch.setattr(webapp_server, "_make_mic_from_config", lambda *_a, **_k: FakeMic())

    assert client.post("/api/summary/start").status_code == 200
    time.sleep(0.05)
    assert client.post("/api/summary/stop").status_code == 200
    assert client.post("/api/summary/generate").status_code == 200
    _wait_for_summary_status(client, "summary_review")

    publish_resp = client.post("/api/summary/publish")
    assert publish_resp.status_code == 502
    payload = publish_resp.get_json()
    assert payload["detail"] == "Uitspreken mislukt: er is geen bruikbaar output-device geconfigureerd."
    assert payload["session"]["recovery"]["stage"] == "tts"
    assert payload["session"]["recovery"]["kind"] == "resource"
    assert payload["session"]["recovery"]["message"] == "Uitspreken mislukt: er is geen bruikbaar output-device geconfigureerd."


def test_summary_stop_output_stops_server_audio_for_completed_publish(monkeypatch, tmp_path: Path):
    output = StubOutput()
    app = _make_app(monkeypatch, tmp_path, stt_backend=StubSTTBackend(["tekst"]), output_backend=output)
    client = app.test_client()
    stop_calls: list[str] = []
    monkeypatch.setattr(webapp_server, "sd", SimpleNamespace(stop=lambda: stop_calls.append("stop")))

    class FakeMic:
        def __init__(self) -> None:
            self.calls = 0

        def capture_utterance(self, timeout_s=10.0):
            self.calls += 1
            if self.calls == 1:
                return SimpleNamespace(pcm=b"wav", sample_rate=16000, channels=1, sample_width=2)
            time.sleep(0.01)
            raise TimeoutError("no speech")

    monkeypatch.setattr(webapp_server, "_make_mic_from_config", lambda *_a, **_k: FakeMic())

    assert client.post("/api/summary/start").status_code == 200
    time.sleep(0.05)
    assert client.post("/api/summary/stop").status_code == 200
    assert client.post("/api/summary/generate").status_code == 200
    _wait_for_summary_status(client, "summary_review")
    publish_resp = client.post("/api/summary/publish")
    assert publish_resp.status_code == 200
    assert publish_resp.get_json()["session"]["status"] == "completed"

    stop_resp = client.post("/api/summary/stop_output")
    assert stop_resp.status_code == 200
    payload = stop_resp.get_json()
    assert payload["session"]["status"] == "completed"
    assert payload["stop_output"]["ok"] is True
    assert payload["stop_output"]["target"] == "server"
    assert stop_calls == ["stop"]


def test_summary_stop_output_interrupts_inflight_publish(monkeypatch, tmp_path: Path):
    started = threading.Event()
    released = threading.Event()

    class BlockingOutput:
        def __init__(self) -> None:
            self.emitted: list[str] = []

        def emit(self, text):
            self.emitted.append(text)
            started.set()
            released.wait(timeout=2.0)
            return True

        def last_error_message(self):
            return ""

    output = BlockingOutput()
    app = _make_app(monkeypatch, tmp_path, stt_backend=StubSTTBackend(["tekst"]), output_backend=output)
    client = app.test_client()
    monkeypatch.setattr(webapp_server, "sd", SimpleNamespace(stop=lambda: released.set()))

    class FakeMic:
        def __init__(self) -> None:
            self.calls = 0

        def capture_utterance(self, timeout_s=10.0):
            self.calls += 1
            if self.calls == 1:
                return SimpleNamespace(pcm=b"wav", sample_rate=16000, channels=1, sample_width=2)
            time.sleep(0.01)
            raise TimeoutError("no speech")

    monkeypatch.setattr(webapp_server, "_make_mic_from_config", lambda *_a, **_k: FakeMic())

    assert client.post("/api/summary/start").status_code == 200
    time.sleep(0.05)
    assert client.post("/api/summary/stop").status_code == 200
    assert client.post("/api/summary/generate").status_code == 200
    _wait_for_summary_status(client, "summary_review")

    publish_result: dict[str, object] = {}

    def _run_publish():
        local_client = app.test_client()
        publish_result["resp"] = local_client.post("/api/summary/publish")

    worker = threading.Thread(target=_run_publish, daemon=True)
    worker.start()
    assert started.wait(timeout=1.0)

    stop_resp = client.post("/api/summary/stop_output")
    assert stop_resp.status_code == 200
    assert stop_resp.get_json()["stop_output"]["ok"] is True

    worker.join(timeout=2.0)
    assert not worker.is_alive()
    publish_resp = publish_result["resp"]
    assert getattr(publish_resp, "status_code", None) == 200
    publish_payload = publish_resp.get_json()
    assert publish_payload["publish_interrupted"] is True
    assert publish_payload["session"]["status"] == "summary_review"
    assert output.emitted == ["ok"]


def test_summary_presets_are_shared_but_selected_preset_is_instance_specific(monkeypatch, tmp_path: Path):
    repo_root = tmp_path / "repo_root"
    app = _make_app(monkeypatch, tmp_path, instance_id="demo_a", repo_root_override=repo_root)
    client = app.test_client()
    cfg = _summary_config()
    cfg["prompts"]["summary_instruction"] = "Preset A"
    create_resp = client.post(
        "/api/summary/presets",
        json={"mode": "create", "name": "Workshop preset", "config": cfg},
    )
    assert create_resp.status_code == 200
    preset = create_resp.get_json()["preset"]
    cfg["selected_preset_id"] = preset["id"]
    assert client.post("/api/summary/config", json={"config": cfg}).status_code == 200

    app_b = _make_app(monkeypatch, tmp_path, instance_id="demo_b", repo_root_override=repo_root)
    client_b = app_b.test_client()
    presets_resp = client_b.get("/api/summary/presets")
    assert presets_resp.status_code == 200
    preset_ids = [item["id"] for item in presets_resp.get_json()["presets"]]
    assert preset["id"] in preset_ids
    cfg_resp_b = client_b.get("/api/summary/config")
    assert cfg_resp_b.status_code == 200
    assert cfg_resp_b.get_json()["config"]["selected_preset_id"] is None


def test_summary_abort_persists_terminal_state(monkeypatch, tmp_path: Path):
    app = _make_app(monkeypatch, tmp_path)
    client = app.test_client()

    class FakeMic:
        def capture_utterance(self, timeout_s=10.0):
            time.sleep(0.01)
            raise TimeoutError("no speech")

    monkeypatch.setattr(webapp_server, "_make_mic_from_config", lambda *_a, **_k: FakeMic())

    assert client.post("/api/summary/start").status_code == 200
    abort_resp = client.post("/api/summary/abort")
    assert abort_resp.status_code == 200
    assert abort_resp.get_json()["session"]["status"] == "aborted"
    assert abort_resp.get_json()["session"]["last_error"] is None
    assert abort_resp.get_json()["session"]["last_error_stage"] is None

    active = json.loads((_summary_state_root(tmp_path, "demo") / "active_session.json").read_text(encoding="utf-8"))
    assert active["status"] == "aborted"
    assert active["last_error"] is None
    assert active["last_error_stage"] is None


def test_summary_abort_clears_publish_error(monkeypatch, tmp_path: Path):
    output = StubOutput(error=RuntimeError("tts boom"))
    app = _make_app(monkeypatch, tmp_path, stt_backend=StubSTTBackend(["tekst"]), output_backend=output)
    client = app.test_client()

    class FakeMic:
        calls = 0

        def capture_utterance(self, timeout_s=10.0):
            self.calls += 1
            if self.calls == 1:
                return SimpleNamespace(pcm=b"wav", sample_rate=16000, channels=1, sample_width=2)
            time.sleep(0.01)
            raise TimeoutError("no speech")

    monkeypatch.setattr(webapp_server, "_make_mic_from_config", lambda *_a, **_k: FakeMic())

    assert client.post("/api/summary/start").status_code == 200
    time.sleep(0.05)
    assert client.post("/api/summary/stop").status_code == 200
    assert client.post("/api/summary/generate").status_code == 200
    _wait_for_summary_status(client, "summary_review")

    publish_resp = client.post("/api/summary/publish")
    assert publish_resp.status_code == 502
    publish_session = publish_resp.get_json()["session"]
    assert publish_session["status"] == "summary_review"
    assert publish_session["last_error_stage"] == "publish"

    abort_resp = client.post("/api/summary/abort")
    assert abort_resp.status_code == 200
    session = abort_resp.get_json()["session"]
    assert session["status"] == "aborted"
    assert session["last_error"] is None
    assert session["last_error_stage"] is None


def test_summary_clear_removes_terminal_recent_session(monkeypatch, tmp_path: Path):
    app = _make_app(monkeypatch, tmp_path)
    client = app.test_client()

    class FakeMic:
        def capture_utterance(self, timeout_s=10.0):
            time.sleep(0.01)
            raise TimeoutError("no speech")

    monkeypatch.setattr(webapp_server, "_make_mic_from_config", lambda *_a, **_k: FakeMic())

    assert client.post("/api/summary/start").status_code == 200
    assert client.post("/api/summary/abort").status_code == 200
    clear_resp = client.post("/api/summary/clear")
    assert clear_resp.status_code == 200
    assert clear_resp.get_json()["session"] is None
    active_path = _summary_state_root(tmp_path, "demo") / "active_session.json"
    assert not active_path.exists()


def test_summary_continuous_laptop_stream_emits_multiple_lines_before_stop(monkeypatch, tmp_path: Path):
    app = _make_app(monkeypatch, tmp_path, stt_backend=StubSTTBackend(["eerste regel", "tweede regel"]))
    client = app.test_client()
    seen = {"stream_calls": 0}

    class FakeMic:
        def stream_utterances(self, stop_event, *, timeout_s, on_utterance, on_timeout=None):
            del timeout_s, on_timeout
            seen["stream_calls"] += 1
            on_utterance(SimpleNamespace(pcm=b"a", sample_rate=16000, channels=1, sample_width=2))
            time.sleep(0.02)
            on_utterance(SimpleNamespace(pcm=b"b", sample_rate=16000, channels=1, sample_width=2))
            while not stop_event.is_set():
                time.sleep(0.005)

    monkeypatch.setattr(webapp_server, "_make_mic_from_config", lambda *_a, **_k: FakeMic())

    start_resp = client.post("/api/summary/start")
    assert start_resp.status_code == 200
    session = None
    deadline = time.time() + 0.8
    while time.time() < deadline:
        mid_resp = client.get("/api/summary")
        assert mid_resp.status_code == 200
        session = mid_resp.get_json()["session"]
        if session["transcript_text"] == "eerste regel\ntweede regel":
            break
        time.sleep(0.02)
    assert session is not None
    assert session["status"] == "capturing"
    assert session["transcript_text"] == "eerste regel\ntweede regel"
    assert int(session["capture_stats"]["audio_chunks"]) == 2
    assert int(session["capture_stats"]["stt_calls"]) == 2
    assert seen["stream_calls"] == 1

    stop_resp = client.post("/api/summary/stop")
    assert stop_resp.status_code == 200
    assert stop_resp.get_json()["session"]["status"] == "transcript_review"


def test_summary_execution_mode_and_incident_banner_stay_in_sync(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(webapp_server, "_SUMMARY_STT_QUEUE_MAX_ITEMS", 2)
    app = _make_app(
        monkeypatch,
        tmp_path,
        stt_backend=PlannedSTTBackend(
            [
                RuntimeError("connection timed out"),
                RuntimeError("connection timed out"),
                "regel na herstel",
                "tweede regel na herstel",
            ]
        ),
    )
    client = app.test_client()

    class FakeMic:
        def stream_utterances(self, stop_event, *, timeout_s, on_utterance, on_timeout=None):
            del timeout_s, on_timeout
            for payload in [b"a", b"b", b"c", b"d"]:
                on_utterance(SimpleNamespace(pcm=payload, sample_rate=16000, channels=1, sample_width=2))
            while not stop_event.is_set():
                time.sleep(0.005)

    monkeypatch.setattr(webapp_server, "_make_mic_from_config", lambda *_a, **_k: FakeMic())

    assert client.post("/api/summary/start").status_code == 200
    time.sleep(0.3)

    summary_resp = client.get("/api/summary")
    assert summary_resp.status_code == 200
    payload = summary_resp.get_json()
    session = payload["session"]
    assert session["status"] == "capturing"
    assert session["recovery"]["stage"] == "stt"
    assert session["execution_mode"] == "cloud"
    assert payload["incident_banner"]["key"] == "stt_cloud_unavailable"
    assert int(session["capture_stats"]["disk_spool_count"]) >= 1

    mode_resp = client.post("/api/summary/execution_mode", json={"execution_mode": "local"})
    assert mode_resp.status_code == 200
    assert mode_resp.get_json()["session"]["execution_mode"] == "local"

    after_mode = client.get("/api/summary").get_json()
    assert after_mode["session"]["execution_mode"] == "local"
    assert after_mode["incident_banner"]["key"] == "stt_local_unavailable"

    recover_resp = client.post("/api/summary/capture/recover")
    assert recover_resp.status_code == 200
    time.sleep(0.12)
    recovered = client.get("/api/summary").get_json()["session"]
    assert recovered["recovery"] is None
    assert "regel na herstel" in recovered["transcript_text"]
    assert recovered["execution_mode"] == "local"

    stop_resp = client.post("/api/summary/stop")
    assert stop_resp.status_code == 200
    stopped_session = stop_resp.get_json()["session"]
    assert stopped_session["status"] == "transcript_review"
    assert int(stopped_session["capture_stats"]["disk_spool_count"]) == 0


def test_summary_capture_auto_resumes_after_cloud_connectivity_probe_recovers(monkeypatch, tmp_path: Path):
    cfg = _summary_config()
    cfg["models"]["stt_primary"] = "azure:default"
    _reset_summary_cloud_probe_state(monkeypatch)
    app = _make_app(
        monkeypatch,
        tmp_path,
        stt_backend=PlannedSTTBackend(
            [
                RuntimeError("Connection failed (no connection to the remote host)"),
                RuntimeError("Connection failed (no connection to the remote host)"),
                "regel na automatisch herstel",
            ]
        ),
    )
    client = app.test_client()

    class FakeMic:
        def stream_utterances(self, stop_event, *, timeout_s, on_utterance, on_timeout=None):
            del timeout_s, on_timeout
            for payload in [b"a", b"b"]:
                on_utterance(SimpleNamespace(pcm=payload, sample_rate=16000, channels=1, sample_width=2))
            while not stop_event.is_set():
                time.sleep(0.005)

    monkeypatch.setattr(webapp_server, "_make_mic_from_config", lambda *_a, **_k: FakeMic())
    monkeypatch.setenv("AZURE_SPEECH_REGION", "westeurope")
    assert client.post("/api/summary/config", json={"config": cfg}).status_code == 200

    probe_state = {"ok": False, "calls": []}

    def connectivity_probe(host, port, *, timeout_s=2.5):
        probe_state["calls"].append((host, port, timeout_s))
        return bool(probe_state["ok"])

    monkeypatch.setattr(webapp_server, "_check_tcp_with_timeout", connectivity_probe)

    assert client.post("/api/summary/start").status_code == 200
    deadline = time.time() + 1.0
    session = {}
    active_issue_keys = set()
    while time.time() < deadline:
        payload = client.get("/api/summary").get_json()
        session = payload["session"]
        active_issue_keys = {
            str(item.get("issue_key") or "")
            for item in (payload.get("connectivity_issues") or [])
            if item.get("active")
        }
        recovery = session.get("recovery") if isinstance(session.get("recovery"), dict) else {}
        if (
            "cloud:stt" in active_issue_keys
            and str(recovery.get("stage") or "").strip().lower() == "stt"
            and str(recovery.get("kind") or "").strip().lower() == "connectivity"
        ):
            break
        time.sleep(0.05)

    assert "cloud:stt" in active_issue_keys
    assert session["status"] == "capturing"
    assert probe_state["calls"]
    assert "regel na automatisch herstel" not in str(session.get("transcript_text") or "")

    probe_state["ok"] = True
    recovered_session = {}
    cleared_keys = {"cloud:stt"}
    deadline = time.time() + 3.2
    while time.time() < deadline:
        cleared = client.get("/api/summary").get_json()
        recovered_session = cleared["session"]
        cleared_keys = {
            str(item.get("issue_key") or "")
            for item in (cleared.get("connectivity_issues") or [])
            if item.get("active")
        }
        if (
            "cloud:stt" not in cleared_keys
            and recovered_session.get("recovery") is None
            and "regel na automatisch herstel" in str(recovered_session.get("transcript_text") or "")
        ):
            break
        time.sleep(0.05)

    assert "cloud:stt" not in cleared_keys
    assert recovered_session["recovery"] is None
    assert "regel na automatisch herstel" in recovered_session["transcript_text"]
    assert recovered_session["toast"]["key"] == "internet_restored"
    assert client.post("/api/summary/abort").status_code == 200


def test_summary_cloud_connectivity_probe_is_skipped_while_mode_is_local(monkeypatch, tmp_path: Path):
    cfg = _summary_config()
    cfg["models"]["stt_primary"] = "azure:default"
    _reset_summary_cloud_probe_state(monkeypatch)
    app = _make_app(
        monkeypatch,
        tmp_path,
        stt_backend=PlannedSTTBackend(
            [
                RuntimeError("Connection failed (no connection to the remote host)"),
                RuntimeError("Connection failed (no connection to the remote host)"),
                "regel die niet automatisch mag komen",
            ]
        ),
    )
    client = app.test_client()

    class FakeMic:
        def stream_utterances(self, stop_event, *, timeout_s, on_utterance, on_timeout=None):
            del timeout_s, on_timeout
            for payload in [b"a", b"b"]:
                on_utterance(SimpleNamespace(pcm=payload, sample_rate=16000, channels=1, sample_width=2))
            while not stop_event.is_set():
                time.sleep(0.005)

    monkeypatch.setattr(webapp_server, "_make_mic_from_config", lambda *_a, **_k: FakeMic())
    monkeypatch.setenv("AZURE_SPEECH_REGION", "westeurope")
    assert client.post("/api/summary/config", json={"config": cfg}).status_code == 200
    monkeypatch.setattr(webapp_server, "_check_tcp_with_timeout", lambda host, port, *, timeout_s=2.5: False)

    assert client.post("/api/summary/start").status_code == 200
    deadline = time.time() + 1.0
    session = {}
    active_issue_keys = set()
    while time.time() < deadline:
        payload = client.get("/api/summary").get_json()
        session = payload["session"]
        active_issue_keys = {
            str(item.get("issue_key") or "")
            for item in (payload.get("connectivity_issues") or [])
            if item.get("active")
        }
        recovery = session.get("recovery") if isinstance(session.get("recovery"), dict) else {}
        if (
            "cloud:stt" in active_issue_keys
            and str(recovery.get("stage") or "").strip().lower() == "stt"
            and str(recovery.get("kind") or "").strip().lower() == "connectivity"
        ):
            break
        time.sleep(0.05)

    assert "cloud:stt" in active_issue_keys
    assert client.post("/api/summary/execution_mode", json={"execution_mode": "local"}).status_code == 200

    probe_calls = []

    def recovered_probe(host, port, *, timeout_s=2.5):
        probe_calls.append((host, port, timeout_s))
        return True

    monkeypatch.setattr(webapp_server, "_check_tcp_with_timeout", recovered_probe)
    local_payload = {}
    deadline = time.time() + 1.8
    while time.time() < deadline:
        local_payload = client.get("/api/summary").get_json()
        time.sleep(0.06)
    assert probe_calls == []
    local_keys = {
        str(item.get("issue_key") or "")
        for item in (local_payload.get("connectivity_issues") or [])
        if item.get("active")
    }
    assert "cloud:stt" in local_keys
    local_session = local_payload["session"]
    assert local_session["execution_mode"] == "local"
    assert str((local_session.get("recovery") or {}).get("stage") or "") == "stt"
    assert "regel die niet automatisch mag komen" not in str(local_session.get("transcript_text") or "")
    assert client.post("/api/summary/abort").status_code == 200


def test_summary_capture_does_not_auto_resume_for_non_connectivity_cloud_stt_failure(monkeypatch, tmp_path: Path):
    cfg = _summary_config()
    cfg["models"]["stt_primary"] = "azure:default"
    _reset_summary_cloud_probe_state(monkeypatch)
    app = _make_app(
        monkeypatch,
        tmp_path,
        stt_backend=PlannedSTTBackend(
            [
                RuntimeError("Unauthorized 401 invalid subscription key"),
                RuntimeError("Unauthorized 401 invalid subscription key"),
                "regel die nooit automatisch mag komen",
            ]
        ),
    )
    client = app.test_client()

    class FakeMic:
        def stream_utterances(self, stop_event, *, timeout_s, on_utterance, on_timeout=None):
            del timeout_s, on_timeout
            for payload in [b"a", b"b"]:
                on_utterance(SimpleNamespace(pcm=payload, sample_rate=16000, channels=1, sample_width=2))
            while not stop_event.is_set():
                time.sleep(0.005)

    monkeypatch.setattr(webapp_server, "_make_mic_from_config", lambda *_a, **_k: FakeMic())
    monkeypatch.setenv("AZURE_SPEECH_REGION", "westeurope")
    assert client.post("/api/summary/config", json={"config": cfg}).status_code == 200

    probe_calls = []

    def recovered_probe(host, port, *, timeout_s=2.5):
        probe_calls.append((host, port, timeout_s))
        return True

    monkeypatch.setattr(webapp_server, "_check_tcp_with_timeout", recovered_probe)

    assert client.post("/api/summary/start").status_code == 200
    deadline = time.time() + 1.0
    payload = {}
    while time.time() < deadline:
        payload = client.get("/api/summary").get_json()
        recovery = payload["session"].get("recovery") if isinstance(payload["session"].get("recovery"), dict) else {}
        if str(recovery.get("stage") or "").strip().lower() == "stt":
            break
        time.sleep(0.05)

    deadline = time.time() + 1.8
    while time.time() < deadline:
        payload = client.get("/api/summary").get_json()
        time.sleep(0.06)

    session = payload["session"]
    assert str((session.get("recovery") or {}).get("stage") or "") == "stt"
    assert str((session.get("recovery") or {}).get("kind") or "") == "service"
    assert "regel die nooit automatisch mag komen" not in str(session.get("transcript_text") or "")
    assert probe_calls == []
    active_issue_keys = {
        str(item.get("issue_key") or "")
        for item in (payload.get("connectivity_issues") or [])
        if item.get("active")
    }
    assert "cloud:stt" in active_issue_keys
    assert client.post("/api/summary/abort").status_code == 200


def test_summary_repair_updates_draft_and_tracks_last_prompt(monkeypatch, tmp_path: Path):
    llm = PlannedLLMBackend(["eerste draft", "herstelde draft"])
    app = _make_app(monkeypatch, tmp_path, stt_backend=StubSTTBackend(["tekst"]), llm_backend=llm)
    client = app.test_client()

    class FakeMic:
        def __init__(self) -> None:
            self.calls = 0

        def capture_utterance(self, timeout_s=10.0):
            self.calls += 1
            if self.calls == 1:
                return SimpleNamespace(pcm=b"wav", sample_rate=16000, channels=1, sample_width=2)
            time.sleep(0.01)
            raise TimeoutError("no speech")

    monkeypatch.setattr(webapp_server, "_make_mic_from_config", lambda *_a, **_k: FakeMic())

    assert client.post("/api/summary/start").status_code == 200
    time.sleep(0.05)
    assert client.post("/api/summary/stop").status_code == 200
    assert client.post("/api/summary/generate").status_code == 200
    _wait_for_summary_status(client, "summary_review")

    repair_resp = client.post("/api/summary/repair", json={"repair_prompt": "Maak het warmer en korter."})
    assert repair_resp.status_code == 200
    assert repair_resp.get_json()["session"]["status"] == "summarizing"
    session = _wait_for_summary_status(client, "summary_review")
    assert session["status"] == "summary_review"
    assert session["draft_text"] == "herstelde draft"
    assert session["last_repair_prompt"] == "Maak het warmer en korter."
    assert llm.calls[-1][-1]["content"].startswith("Repareer de bestaande samenvatting")


def test_summary_fallback_summary_publishes_without_overwriting_draft(monkeypatch, tmp_path: Path):
    llm = PlannedLLMBackend(["eerste draft"])
    app = _make_app(monkeypatch, tmp_path, stt_backend=StubSTTBackend(["tekst"]), llm_backend=llm)
    client = app.test_client()

    class FakeMic:
        def __init__(self) -> None:
            self.calls = 0

        def capture_utterance(self, timeout_s=10.0):
            self.calls += 1
            if self.calls == 1:
                return SimpleNamespace(pcm=b"wav", sample_rate=16000, channels=1, sample_width=2)
            time.sleep(0.01)
            raise TimeoutError("no speech")

    monkeypatch.setattr(webapp_server, "_make_mic_from_config", lambda *_a, **_k: FakeMic())

    assert client.post("/api/summary/start").status_code == 200
    time.sleep(0.05)
    assert client.post("/api/summary/stop").status_code == 200
    generate_resp = client.post("/api/summary/generate")
    assert generate_resp.status_code == 200
    original_draft = _wait_for_summary_status(client, "summary_review")["draft_text"]

    fallback_resp = client.post("/api/summary/use_fallback_summary")
    assert fallback_resp.status_code == 200
    fallback_session = fallback_resp.get_json()["session"]
    assert fallback_session["status"] == "completed"
    assert fallback_session["draft_text"] == original_draft
    assert fallback_session["published_text"] == webapp_server._SUMMARY_DEFAULT_FALLBACK_SUMMARY
    assert fallback_session["published_at"]


def test_summary_get_refreshes_stale_base_connectivity_issue(monkeypatch, tmp_path: Path):
    ping_state = {"ok": False}

    class FakeResp:
        def __init__(self, *, ok: bool) -> None:
            self.status_code = 200 if ok else 503
            self.ok = bool(ok)

        def json(self):
            return {"status": "ok" if self.ok else "error"}

    def fake_get(_url, timeout=0, **_kwargs):
        del timeout
        return FakeResp(ok=bool(ping_state["ok"]))

    monkeypatch.setattr(webapp_server.requests, "get", fake_get)
    app = _make_app(monkeypatch, tmp_path)
    client = app.test_client()
    cfg_resp = client.post(
        "/api/runtime_config",
        json={
            "config": {
                "output_target": "server",
                "base_enabled": True,
                "behavior_enabled": False,
                "nao_ip_enabled": False,
                "nao_base_url": "http://127.0.0.1:5103",
            }
        },
    )
    assert cfg_resp.status_code == 200

    health_resp = client.post(
        "/api/runtime_health",
        json={
            "base_enabled": True,
            "behavior_enabled": False,
            "nao_ip_enabled": False,
            "nao_base_url": "http://127.0.0.1:5103",
        },
    )
    assert health_resp.status_code == 200
    health_payload = health_resp.get_json()
    assert "dm_local:base_ping" in {
        str(item.get("issue_key") or "")
        for item in (health_payload.get("connectivity_issues") or [])
        if item.get("active")
    }

    ping_state["ok"] = True
    summary_resp = client.get("/api/summary")
    assert summary_resp.status_code == 200
    summary_payload = summary_resp.get_json()
    assert "dm_local:base_ping" not in {
        str(item.get("issue_key") or "")
        for item in (summary_payload.get("connectivity_issues") or [])
        if item.get("active")
    }
    base_pill = next(item for item in summary_payload["health"] if item["key"] == "base")
    assert base_pill["status"] == "ok"


def test_summary_complete_without_publish(monkeypatch, tmp_path: Path):
    llm = PlannedLLMBackend(["eerste draft"])
    app = _make_app(monkeypatch, tmp_path, stt_backend=StubSTTBackend(["tekst"]), llm_backend=llm)
    client = app.test_client()

    class FakeMic:
        def __init__(self) -> None:
            self.calls = 0

        def capture_utterance(self, timeout_s=10.0):
            self.calls += 1
            if self.calls == 1:
                return SimpleNamespace(pcm=b"wav", sample_rate=16000, channels=1, sample_width=2)
            time.sleep(0.01)
            raise TimeoutError("no speech")

    monkeypatch.setattr(webapp_server, "_make_mic_from_config", lambda *_a, **_k: FakeMic())

    assert client.post("/api/summary/start").status_code == 200
    time.sleep(0.05)
    assert client.post("/api/summary/stop").status_code == 200
    assert client.post("/api/summary/generate").status_code == 200
    _wait_for_summary_status(client, "summary_review")

    complete_resp = client.post("/api/summary/complete_without_publish")
    assert complete_resp.status_code == 200
    session = complete_resp.get_json()["session"]
    assert session["status"] == "completed"
    assert session["published_text"] is None
    assert session["published_at"] is None


def test_summary_navigation_marks_draft_stale_and_requires_publish_confirmation(monkeypatch, tmp_path: Path):
    output = StubOutput()
    app = _make_app(monkeypatch, tmp_path, stt_backend=StubSTTBackend(["tekst"]), llm_backend=PlannedLLMBackend(["eerste draft"]), output_backend=output)
    client = app.test_client()

    class FakeMic:
        def __init__(self) -> None:
            self.calls = 0

        def capture_utterance(self, timeout_s=10.0):
            self.calls += 1
            if self.calls == 1:
                return SimpleNamespace(pcm=b"wav", sample_rate=16000, channels=1, sample_width=2)
            time.sleep(0.01)
            raise TimeoutError("no speech")

    monkeypatch.setattr(webapp_server, "_make_mic_from_config", lambda *_a, **_k: FakeMic())

    assert client.post("/api/summary/start").status_code == 200
    time.sleep(0.05)
    assert client.post("/api/summary/stop").status_code == 200
    generate_resp = client.post("/api/summary/generate")
    assert generate_resp.status_code == 200
    generated_session = _wait_for_summary_status(client, "summary_review")
    assert generated_session["draft_stale"] is False
    assert generated_session["draft_transcript_revision"] == generated_session["transcript_revision"]

    nav_resp = client.post("/api/summary/navigate", json={"target_phase": "transcript_review"})
    assert nav_resp.status_code == 200
    nav_session = nav_resp.get_json()["session"]
    assert nav_session["status"] == "transcript_review"
    assert nav_session["draft_text"] == "eerste draft"
    assert nav_session["draft_stale"] is False

    edit_resp = client.post("/api/summary/transcript", json={"transcript_text": "tekst aangepast"})
    assert edit_resp.status_code == 200
    edited_session = edit_resp.get_json()["session"]
    assert edited_session["draft_text"] == "eerste draft"
    assert edited_session["draft_stale"] is True
    assert edited_session["draft_transcript_revision"] < edited_session["transcript_revision"]

    back_resp = client.post("/api/summary/navigate", json={"target_phase": "summary_review"})
    assert back_resp.status_code == 200
    back_session = back_resp.get_json()["session"]
    assert back_session["status"] == "summary_review"
    assert back_session["draft_stale"] is True

    publish_resp = client.post("/api/summary/publish")
    assert publish_resp.status_code == 409
    publish_payload = publish_resp.get_json()
    assert publish_payload["error"] == "summary_draft_stale"

    forced_publish = client.post("/api/summary/publish", json={"allow_stale": True})
    assert forced_publish.status_code == 200
    forced_session = forced_publish.get_json()["session"]
    assert forced_session["status"] == "completed"
    assert forced_session["published_text"] == "eerste draft"
    assert output.emitted == ["eerste draft"]


def test_summary_navigation_resume_capture_in_same_session_appends_transcript(monkeypatch, tmp_path: Path):
    app = _make_app(monkeypatch, tmp_path, stt_backend=StubSTTBackend(["eerste regel", "tweede regel"]), llm_backend=PlannedLLMBackend(["eerste draft"]))
    client = app.test_client()
    seen = {"stream_calls": 0}

    class FakeMic:
        def stream_utterances(self, stop_event, *, timeout_s, on_utterance, on_timeout=None):
            del timeout_s, on_timeout
            seen["stream_calls"] += 1
            payload = b"a" if seen["stream_calls"] == 1 else b"b"
            on_utterance(SimpleNamespace(pcm=payload, sample_rate=16000, channels=1, sample_width=2))
            while not stop_event.is_set():
                time.sleep(0.005)

    monkeypatch.setattr(webapp_server, "_make_mic_from_config", lambda *_a, **_k: FakeMic())

    assert client.post("/api/summary/start").status_code == 200
    deadline = time.time() + 0.8
    while time.time() < deadline:
        payload = client.get("/api/summary").get_json()
        if payload["session"]["transcript_text"] == "eerste regel":
            break
        time.sleep(0.02)
    assert client.post("/api/summary/stop").status_code == 200
    assert client.post("/api/summary/generate").status_code == 200
    _wait_for_summary_status(client, "summary_review")
    before_resume = client.get("/api/summary").get_json()["session"]
    session_id = before_resume["session_id"]

    resume_resp = client.post("/api/summary/navigate", json={"target_phase": "capture"})
    assert resume_resp.status_code == 200
    resume_session = resume_resp.get_json()["session"]
    assert resume_session["status"] == "capture_ready"
    assert resume_session["session_id"] == session_id
    assert seen["stream_calls"] == 1

    resume_capture_resp = client.post("/api/summary/capture/resume")
    assert resume_capture_resp.status_code == 200
    assert resume_capture_resp.get_json()["session"]["status"] == "capturing"

    deadline = time.time() + 1.0
    latest = None
    while time.time() < deadline:
        latest = client.get("/api/summary").get_json()["session"]
        if latest["transcript_text"] == "eerste regel\ntweede regel":
            break
        time.sleep(0.02)
    assert latest is not None
    assert latest["session_id"] == session_id
    assert latest["status"] == "capturing"
    assert latest["draft_stale"] is True
    assert latest["draft_transcript_revision"] < latest["transcript_revision"]
    assert seen["stream_calls"] == 2

    stop_resp = client.post("/api/summary/stop")
    assert stop_resp.status_code == 200
    stopped_session = stop_resp.get_json()["session"]
    assert stopped_session["status"] == "transcript_review"
    assert stopped_session["transcript_text"] == "eerste regel\ntweede regel"


def test_summary_navigation_reopens_completed_session_to_review_and_capture(monkeypatch, tmp_path: Path):
    output = StubOutput()
    app = _make_app(monkeypatch, tmp_path, stt_backend=StubSTTBackend(["eerste regel", "tweede regel"]), llm_backend=PlannedLLMBackend(["draft"]), output_backend=output)
    client = app.test_client()
    seen = {"stream_calls": 0}

    class FakeMic:
        def stream_utterances(self, stop_event, *, timeout_s, on_utterance, on_timeout=None):
            del timeout_s, on_timeout
            seen["stream_calls"] += 1
            payload = b"a" if seen["stream_calls"] == 1 else b"b"
            on_utterance(SimpleNamespace(pcm=payload, sample_rate=16000, channels=1, sample_width=2))
            while not stop_event.is_set():
                time.sleep(0.005)

    monkeypatch.setattr(webapp_server, "_make_mic_from_config", lambda *_a, **_k: FakeMic())

    assert client.post("/api/summary/start").status_code == 200
    deadline = time.time() + 0.8
    while time.time() < deadline:
        payload = client.get("/api/summary").get_json()
        if payload["session"]["transcript_text"] == "eerste regel":
            break
        time.sleep(0.02)
    assert client.post("/api/summary/stop").status_code == 200
    assert client.post("/api/summary/generate").status_code == 200
    _wait_for_summary_status(client, "summary_review")
    publish_resp = client.post("/api/summary/publish")
    assert publish_resp.status_code == 200
    completed_session = publish_resp.get_json()["session"]
    session_id = completed_session["session_id"]
    assert completed_session["status"] == "completed"
    assert completed_session["published_text"] == "draft"

    reopen_review = client.post("/api/summary/navigate", json={"target_phase": "summary_review"})
    assert reopen_review.status_code == 200
    reopened_review_session = reopen_review.get_json()["session"]
    assert reopened_review_session["session_id"] == session_id
    assert reopened_review_session["status"] == "summary_review"
    assert reopened_review_session["published_text"] is None
    assert reopened_review_session["published_at"] is None
    assert reopened_review_session["draft_text"] == "draft"

    reopen_capture = client.post("/api/summary/navigate", json={"target_phase": "capture"})
    assert reopen_capture.status_code == 200
    reopened_capture_session = reopen_capture.get_json()["session"]
    assert reopened_capture_session["session_id"] == session_id
    assert reopened_capture_session["status"] == "capture_ready"
    assert reopened_capture_session["published_text"] is None
    assert seen["stream_calls"] == 1

    resume_capture = client.post("/api/summary/capture/resume")
    assert resume_capture.status_code == 200
    assert resume_capture.get_json()["session"]["status"] == "capturing"

    deadline = time.time() + 1.0
    latest = None
    while time.time() < deadline:
        latest = client.get("/api/summary").get_json()["session"]
        if latest["transcript_text"] == "eerste regel\ntweede regel":
            break
        time.sleep(0.02)
    assert latest is not None
    assert latest["draft_stale"] is True
    assert latest["published_text"] is None
    assert latest["session_id"] == session_id

    assert client.post("/api/summary/stop").status_code == 200


def test_summary_navigation_rejected_while_summarizing(monkeypatch, tmp_path: Path):
    llm_started = threading.Event()
    llm_release = threading.Event()

    class BlockingLLMBackend:
        def generate(self, messages):
            llm_started.set()
            assert llm_release.wait(timeout=2.0)
            return LLMResult(reply="late draft", messages=list(messages))

    app = _make_app(monkeypatch, tmp_path, stt_backend=StubSTTBackend(["tekst"]), llm_backend=BlockingLLMBackend())
    client = app.test_client()

    class FakeMic:
        def __init__(self) -> None:
            self.calls = 0

        def capture_utterance(self, timeout_s=10.0):
            self.calls += 1
            if self.calls == 1:
                return SimpleNamespace(pcm=b"wav", sample_rate=16000, channels=1, sample_width=2)
            time.sleep(0.01)
            raise TimeoutError("no speech")

    monkeypatch.setattr(webapp_server, "_make_mic_from_config", lambda *_a, **_k: FakeMic())

    assert client.post("/api/summary/start").status_code == 200
    time.sleep(0.05)
    assert client.post("/api/summary/stop").status_code == 200

    result = {"resp": client.post("/api/summary/generate")}
    assert result["resp"].status_code == 200
    assert result["resp"].get_json()["session"]["status"] == "summarizing"
    assert llm_started.wait(timeout=1.0)

    nav_resp = client.post("/api/summary/navigate", json={"target_phase": "capture"})
    assert nav_resp.status_code == 409
    assert nav_resp.get_json()["error"] == "invalid_summary_state"

    llm_release.set()
    assert _wait_for_summary_status(client, "summary_review", timeout_s=2.0)["draft_text"] == "late draft"


def test_summary_generate_request_rejected_while_summarizing(monkeypatch, tmp_path: Path):
    llm_started = threading.Event()
    llm_release = threading.Event()

    class BlockingLLMBackend:
        def generate(self, messages):
            llm_started.set()
            assert llm_release.wait(timeout=2.0)
            return LLMResult(reply="late draft", messages=list(messages))

    app = _make_app(monkeypatch, tmp_path, stt_backend=StubSTTBackend(["tekst"]), llm_backend=BlockingLLMBackend())
    client = app.test_client()

    class FakeMic:
        def __init__(self) -> None:
            self.calls = 0

        def capture_utterance(self, timeout_s=10.0):
            self.calls += 1
            if self.calls == 1:
                return SimpleNamespace(pcm=b"wav", sample_rate=16000, channels=1, sample_width=2)
            time.sleep(0.01)
            raise TimeoutError("no speech")

    monkeypatch.setattr(webapp_server, "_make_mic_from_config", lambda *_a, **_k: FakeMic())

    assert client.post("/api/summary/start").status_code == 200
    time.sleep(0.05)
    assert client.post("/api/summary/stop").status_code == 200

    result = {"resp": client.post("/api/summary/generate")}
    assert result["resp"].status_code == 200
    assert result["resp"].get_json()["session"]["status"] == "summarizing"
    assert llm_started.wait(timeout=1.0)

    duplicate_resp = client.post("/api/summary/generate")
    assert duplicate_resp.status_code == 400
    duplicate_payload = duplicate_resp.get_json()
    assert duplicate_payload["error"] == "invalid_summary_state"
    assert duplicate_payload["phase"] == "summarizing"

    mode_resp = client.post("/api/summary/execution_mode", json={"execution_mode": "local"})
    assert mode_resp.status_code == 409
    assert mode_resp.get_json()["error"] == "invalid_summary_state"
    assert mode_resp.get_json()["phase"] == "summarizing"

    llm_release.set()
    assert _wait_for_summary_status(client, "summary_review", timeout_s=2.0)["draft_text"] == "late draft"


def test_summary_abort_during_summarizing_discards_late_local_llm_result_and_attempts_stop(monkeypatch, tmp_path: Path):
    llm_started = threading.Event()
    llm_release = threading.Event()
    stop_calls: list[tuple[list[str], dict[str, str] | None]] = []

    class BlockingLLMBackend:
        def generate(self, messages):
            llm_started.set()
            assert llm_release.wait(timeout=2.0)
            return LLMResult(reply="te late draft", messages=list(messages))

    def fake_run(args, capture_output, text, timeout, env):
        stop_calls.append((list(args), {"OLLAMA_HOST": str(env.get("OLLAMA_HOST") or "")}))
        return SimpleNamespace(returncode=0, stdout="stopped", stderr="")

    monkeypatch.setattr(webapp_server.shutil, "which", lambda name: "ollama.exe" if name == "ollama" else None)
    monkeypatch.setattr(webapp_server.subprocess, "run", fake_run)

    app = _make_app(monkeypatch, tmp_path, stt_backend=StubSTTBackend(["tekst"]), llm_backend=BlockingLLMBackend())
    client = app.test_client()

    cfg = _summary_config()
    cfg["models"]["llm_primary"] = "ollama_local:granite3.2:8b"
    assert client.post("/api/summary/config", json={"config": cfg}).status_code == 200

    class FakeMic:
        def __init__(self) -> None:
            self.calls = 0

        def capture_utterance(self, timeout_s=10.0):
            self.calls += 1
            if self.calls == 1:
                return SimpleNamespace(pcm=b"wav", sample_rate=16000, channels=1, sample_width=2)
            time.sleep(0.01)
            raise TimeoutError("no speech")

    monkeypatch.setattr(webapp_server, "_make_mic_from_config", lambda *_a, **_k: FakeMic())

    assert client.post("/api/summary/start").status_code == 200
    time.sleep(0.05)
    assert client.post("/api/summary/stop").status_code == 200

    generate_resp = client.post("/api/summary/generate")
    assert generate_resp.status_code == 200
    assert generate_resp.get_json()["session"]["status"] == "summarizing"
    assert llm_started.wait(timeout=1.0)

    abort_resp = client.post("/api/summary/abort")
    assert abort_resp.status_code == 200
    assert abort_resp.get_json()["session"]["status"] == "aborted"

    deadline = time.time() + 1.0
    while time.time() < deadline and not stop_calls:
        time.sleep(0.02)
    assert stop_calls
    assert stop_calls[0][0][-2:] == ["stop", "granite3.2:8b"]

    llm_release.set()
    time.sleep(0.1)
    latest = client.get("/api/summary").get_json()["session"]
    assert latest["status"] == "aborted"
    assert str(latest.get("draft_text") or "").strip() == ""


def test_summary_abort_during_summarizing_does_not_attempt_local_stop_for_cloud_llm(monkeypatch, tmp_path: Path):
    llm_started = threading.Event()
    llm_release = threading.Event()
    stop_calls: list[list[str]] = []

    class BlockingLLMBackend:
        def generate(self, messages):
            llm_started.set()
            assert llm_release.wait(timeout=2.0)
            return LLMResult(reply="te late draft", messages=list(messages))

    def fake_run(args, capture_output, text, timeout, env):
        stop_calls.append(list(args))
        return SimpleNamespace(returncode=0, stdout="stopped", stderr="")

    monkeypatch.setattr(webapp_server.shutil, "which", lambda name: "ollama.exe" if name == "ollama" else None)
    monkeypatch.setattr(webapp_server.subprocess, "run", fake_run)

    app = _make_app(monkeypatch, tmp_path, stt_backend=StubSTTBackend(["tekst"]), llm_backend=BlockingLLMBackend())
    client = app.test_client()

    cfg = _summary_config()
    cfg["models"]["llm_primary"] = "ollama_cloud:gpt-oss:120b-cloud"
    assert client.post("/api/summary/config", json={"config": cfg}).status_code == 200

    class FakeMic:
        def __init__(self) -> None:
            self.calls = 0

        def capture_utterance(self, timeout_s=10.0):
            self.calls += 1
            if self.calls == 1:
                return SimpleNamespace(pcm=b"wav", sample_rate=16000, channels=1, sample_width=2)
            time.sleep(0.01)
            raise TimeoutError("no speech")

    monkeypatch.setattr(webapp_server, "_make_mic_from_config", lambda *_a, **_k: FakeMic())

    assert client.post("/api/summary/start").status_code == 200
    time.sleep(0.05)
    assert client.post("/api/summary/stop").status_code == 200

    generate_resp = client.post("/api/summary/generate")
    assert generate_resp.status_code == 200
    assert llm_started.wait(timeout=1.0)

    abort_resp = client.post("/api/summary/abort")
    assert abort_resp.status_code == 200
    assert abort_resp.get_json()["session"]["status"] == "aborted"
    time.sleep(0.1)
    assert stop_calls == []

    llm_release.set()
    time.sleep(0.1)
    latest = client.get("/api/summary").get_json()["session"]
    assert latest["status"] == "aborted"


def test_summary_navigation_rejected_while_publishing(monkeypatch, tmp_path: Path):
    output_started = threading.Event()
    output_release = threading.Event()

    class BlockingOutput:
        def __init__(self) -> None:
            self.emitted = []

        def emit(self, text):
            self.emitted.append(text)
            output_started.set()
            assert output_release.wait(timeout=2.0)
            return True

        def last_error_message(self):
            return ""

    app = _make_app(monkeypatch, tmp_path, stt_backend=StubSTTBackend(["tekst"]), llm_backend=PlannedLLMBackend(["draft"]), output_backend=BlockingOutput())
    client = app.test_client()

    class FakeMic:
        def __init__(self) -> None:
            self.calls = 0

        def capture_utterance(self, timeout_s=10.0):
            self.calls += 1
            if self.calls == 1:
                return SimpleNamespace(pcm=b"wav", sample_rate=16000, channels=1, sample_width=2)
            time.sleep(0.01)
            raise TimeoutError("no speech")

    monkeypatch.setattr(webapp_server, "_make_mic_from_config", lambda *_a, **_k: FakeMic())

    assert client.post("/api/summary/start").status_code == 200
    time.sleep(0.05)
    assert client.post("/api/summary/stop").status_code == 200
    assert client.post("/api/summary/generate").status_code == 200
    _wait_for_summary_status(client, "summary_review")

    worker_client = app.test_client()
    result = {}

    def _run_publish():
        result["resp"] = worker_client.post("/api/summary/publish", json={"allow_stale": True})

    thread = threading.Thread(target=_run_publish, daemon=True)
    thread.start()
    assert output_started.wait(timeout=1.0)

    nav_resp = client.post("/api/summary/navigate", json={"target_phase": "transcript_review"})
    assert nav_resp.status_code == 409
    assert nav_resp.get_json()["error"] == "invalid_summary_state"

    output_release.set()
    thread.join(timeout=2.0)
    assert result["resp"].status_code == 200


def test_summary_publish_like_actions_are_rejected_while_publishing(monkeypatch, tmp_path: Path):
    output_started = threading.Event()
    output_release = threading.Event()

    class BlockingOutput:
        def __init__(self) -> None:
            self.emitted = []

        def emit(self, text):
            self.emitted.append(text)
            output_started.set()
            assert output_release.wait(timeout=2.0)
            return True

        def last_error_message(self):
            return ""

    app = _make_app(monkeypatch, tmp_path, stt_backend=StubSTTBackend(["tekst"]), llm_backend=PlannedLLMBackend(["draft"]), output_backend=BlockingOutput())
    client = app.test_client()

    class FakeMic:
        def __init__(self) -> None:
            self.calls = 0

        def capture_utterance(self, timeout_s=10.0):
            self.calls += 1
            if self.calls == 1:
                return SimpleNamespace(pcm=b"wav", sample_rate=16000, channels=1, sample_width=2)
            time.sleep(0.01)
            raise TimeoutError("no speech")

    monkeypatch.setattr(webapp_server, "_make_mic_from_config", lambda *_a, **_k: FakeMic())

    assert client.post("/api/summary/start").status_code == 200
    time.sleep(0.05)
    assert client.post("/api/summary/stop").status_code == 200
    assert client.post("/api/summary/generate").status_code == 200
    _wait_for_summary_status(client, "summary_review")

    worker_client = app.test_client()
    result = {}

    def _run_publish():
        result["resp"] = worker_client.post("/api/summary/publish", json={"allow_stale": True})

    thread = threading.Thread(target=_run_publish, daemon=True)
    thread.start()
    assert output_started.wait(timeout=1.0)

    duplicate_publish = client.post("/api/summary/publish", json={"allow_stale": True})
    assert duplicate_publish.status_code == 409
    assert duplicate_publish.get_json()["error"] == "invalid_summary_state"
    assert duplicate_publish.get_json()["phase"] == "publishing"

    fallback_publish = client.post("/api/summary/use_fallback_summary")
    assert fallback_publish.status_code == 400
    assert fallback_publish.get_json()["error"] == "invalid_summary_state"
    assert fallback_publish.get_json()["phase"] == "publishing"

    complete_resp = client.post("/api/summary/complete_without_publish")
    assert complete_resp.status_code == 409
    assert complete_resp.get_json()["error"] == "invalid_summary_state"
    assert complete_resp.get_json()["phase"] == "publishing"

    mode_resp = client.post("/api/summary/execution_mode", json={"execution_mode": "local"})
    assert mode_resp.status_code == 409
    assert mode_resp.get_json()["error"] == "invalid_summary_state"
    assert mode_resp.get_json()["phase"] == "publishing"

    output_release.set()
    thread.join(timeout=2.0)
    assert result["resp"].status_code == 200


def test_summary_active_session_snapshot_stays_unchanged_when_draft_config_changes(monkeypatch, tmp_path: Path):
    app = _make_app(monkeypatch, tmp_path)
    client = app.test_client()

    class FakeMic:
        def capture_utterance(self, timeout_s=10.0):
            time.sleep(0.01)
            raise TimeoutError("no speech")

    monkeypatch.setattr(webapp_server, "_make_mic_from_config", lambda *_a, **_k: FakeMic())

    cfg = _summary_config()
    cfg["prompts"]["summary_instruction"] = "Eerste instructie"
    assert client.post("/api/summary/config", json={"config": cfg}).status_code == 200
    start_resp = client.post("/api/summary/start")
    assert start_resp.status_code == 200
    snapshot = start_resp.get_json()["session"]["config_snapshot"]["config"]
    assert snapshot["prompts"]["summary_instruction"] == "Eerste instructie"

    cfg["prompts"]["summary_instruction"] = "Tweede instructie"
    assert client.post("/api/summary/config", json={"config": cfg}).status_code == 200

    summary_resp = client.get("/api/summary")
    assert summary_resp.status_code == 200
    live_snapshot = summary_resp.get_json()["session"]["config_snapshot"]["config"]
    assert live_snapshot["prompts"]["summary_instruction"] == "Eerste instructie"
    assert client.post("/api/summary/abort").status_code == 200


def test_summary_review_config_update_changes_active_session_snapshot(monkeypatch, tmp_path: Path):
    app = _make_app(monkeypatch, tmp_path, stt_backend=StubSTTBackend(["tekst"]))
    client = app.test_client()

    class FakeMic:
        def __init__(self) -> None:
            self.calls = 0

        def capture_utterance(self, timeout_s=10.0):
            self.calls += 1
            if self.calls == 1:
                return SimpleNamespace(pcm=b"wav", sample_rate=16000, channels=1, sample_width=2)
            time.sleep(0.01)
            raise TimeoutError("no speech")

    monkeypatch.setattr(webapp_server, "_make_mic_from_config", lambda *_a, **_k: FakeMic())

    cfg = _summary_config()
    cfg["prompts"]["summary_instruction"] = "Review instructie 1"
    assert client.post("/api/summary/config", json={"config": cfg}).status_code == 200
    assert client.post("/api/summary/start").status_code == 200
    time.sleep(0.05)
    assert client.post("/api/summary/stop").status_code == 200

    cfg["prompts"]["summary_instruction"] = "Review instructie 2"
    cfg_resp = client.post("/api/summary/config", json={"config": cfg})
    assert cfg_resp.status_code == 200
    assert cfg_resp.get_json()["applies_to_active_session"] is True

    summary_resp = client.get("/api/summary")
    assert summary_resp.status_code == 200
    live_snapshot = summary_resp.get_json()["session"]["config_snapshot"]["config"]
    assert live_snapshot["prompts"]["summary_instruction"] == "Review instructie 2"


def test_summary_abort_clears_spool_directory(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(webapp_server, "_SUMMARY_STT_QUEUE_MAX_ITEMS", 1)
    app = _make_app(
        monkeypatch,
        tmp_path,
        stt_backend=PlannedSTTBackend(
            [
                RuntimeError("connection reset by peer"),
                RuntimeError("connection reset by peer"),
            ]
        ),
    )
    client = app.test_client()

    class FakeMic:
        def stream_utterances(self, stop_event, *, timeout_s, on_utterance, on_timeout=None):
            del timeout_s, on_timeout
            for payload in [b"a", b"b", b"c"]:
                on_utterance(SimpleNamespace(pcm=payload, sample_rate=16000, channels=1, sample_width=2))
            while not stop_event.is_set():
                time.sleep(0.005)

    monkeypatch.setattr(webapp_server, "_make_mic_from_config", lambda *_a, **_k: FakeMic())

    assert client.post("/api/summary/start").status_code == 200
    session = None
    deadline = time.time() + 2.0
    while time.time() < deadline:
        payload = client.get("/api/summary").get_json()
        session = payload["session"]
        if int(((session.get("capture_stats") or {}).get("disk_spool_count") or 0)) >= 1:
            break
        time.sleep(0.02)
    assert session is not None
    session_id = str(session["session_id"])
    spool_dir = _summary_state_root(tmp_path, "demo") / "stt_spool" / session_id
    assert spool_dir.is_dir()
    assert any(spool_dir.iterdir())

    abort_resp = client.post("/api/summary/abort")
    assert abort_resp.status_code == 200
    aborted_session = abort_resp.get_json()["session"]
    assert aborted_session["status"] == "aborted"
    assert int(((aborted_session.get("capture_stats") or {}).get("disk_spool_count") or 0)) == 0
    assert not spool_dir.exists()


def test_summary_clear_removes_terminal_spool_directory(monkeypatch, tmp_path: Path):
    app = _make_app(monkeypatch, tmp_path)
    client = app.test_client()

    class FakeMic:
        def capture_utterance(self, timeout_s=10.0):
            time.sleep(0.01)
            raise TimeoutError("no speech")

    monkeypatch.setattr(webapp_server, "_make_mic_from_config", lambda *_a, **_k: FakeMic())

    start_resp = client.post("/api/summary/start")
    assert start_resp.status_code == 200
    session_id = start_resp.get_json()["session"]["session_id"]
    assert client.post("/api/summary/abort").status_code == 200

    spool_dir = _summary_state_root(tmp_path, "demo") / "stt_spool" / str(session_id)
    spool_dir.mkdir(parents=True, exist_ok=True)
    (spool_dir / "00000001.json").write_text("{}", encoding="utf-8")
    assert spool_dir.is_dir()

    clear_resp = client.post("/api/summary/clear")
    assert clear_resp.status_code == 200
    assert clear_resp.get_json()["session"] is None
    assert not spool_dir.exists()


def test_summary_restart_loads_recent_session_without_resuming_capture(monkeypatch, tmp_path: Path):
    root = _summary_state_root(tmp_path, "demo")
    root.mkdir(parents=True, exist_ok=True)
    active_path = root / "active_session.json"
    active_path.write_text(
        json.dumps(
            {
                "session_id": "s1",
                "status": "capturing",
                "created_at": "2025-01-01T00:00:00Z",
                "updated_at": "2025-01-01T00:00:00Z",
                "config_snapshot": {
                    "runtime_overrides": {"output_target": "server"},
                    "system_prompt_text": "system",
                    "system_prompt_meta": {"source": "default", "name": "summary_prompts/workshop_summary_system.txt"},
                },
                "transcript_lines": [{"id": "l1", "text": "regel", "source": "stt", "created_at": "2025-01-01T00:00:00Z"}],
                "transcript_text": "regel",
                "capture_stats": {"transcript_count": 1},
            }
        ),
        encoding="utf-8",
    )

    app = _make_app(monkeypatch, tmp_path)
    client = app.test_client()
    resp = client.get("/api/summary")
    assert resp.status_code == 200
    session = resp.get_json()["session"]
    assert session["status"] == "transcript_review"
    assert session["last_error_stage"] == "restart"
