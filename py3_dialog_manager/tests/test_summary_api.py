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
    monkeypatch.setattr(webapp_server, "build_pipeline_from_config", lambda *_a, **_k: base_pipeline)
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
    gen_session = gen_resp.get_json()["session"]
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
    assert gen_resp.status_code == 502
    session = gen_resp.get_json()["session"]
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
    publish_resp = client.post("/api/summary/publish")
    assert publish_resp.status_code == 502
    session = publish_resp.get_json()["session"]
    assert session["status"] == "summary_review"
    assert session["last_error_stage"] == "publish"


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
    time.sleep(0.12)
    mid_resp = client.get("/api/summary")
    assert mid_resp.status_code == 200
    session = mid_resp.get_json()["session"]
    assert session["status"] == "capturing"
    assert session["transcript_text"] == "eerste regel\ntweede regel"
    assert int(session["capture_stats"]["audio_chunks"]) == 2
    assert int(session["capture_stats"]["stt_calls"]) == 2
    assert seen["stream_calls"] == 1

    stop_resp = client.post("/api/summary/stop")
    assert stop_resp.status_code == 200
    assert stop_resp.get_json()["session"]["status"] == "transcript_review"


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
