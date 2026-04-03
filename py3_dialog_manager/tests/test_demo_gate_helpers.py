from __future__ import annotations

import threading
import time
from pathlib import Path
from types import SimpleNamespace

import demo_gate
import webapp_server
from dialog.interfaces import LLMResult
import pytest


class _StubLLM:
    def generate(self, messages):
        return LLMResult(reply="ok", messages=list(messages))


class _StubSTT:
    def transcribe(self, audio):  # pragma: no cover - not used in these tests
        return SimpleNamespace(text="", language="nl", confidence=1.0)


def _make_app(monkeypatch, tmp_path: Path, *, web_port: int = 5301):
    base_pipeline = SimpleNamespace(
        llm=_StubLLM(),
        output=None,
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
    monkeypatch.setattr(webapp_server, "make_stt_backend_from_config", lambda *_a, **_k: _StubSTT())
    app, _stt, _pipeline = webapp_server.create_app(
        cfg={},
        config_path=str(tmp_path / "default.json"),
        web_port=web_port,
        runtime_state_dir=str(tmp_path / "state"),
    )
    return app


def _write_demo_gate_test_config_root(tmp_path: Path) -> Path:
    dm_root = tmp_path
    configs_dir = dm_root / "configs"
    configs_dir.mkdir(parents=True, exist_ok=True)
    (configs_dir / "default.json").write_text("{}", encoding="utf-8")
    (configs_dir / "summary_presets.json").write_text(
        """
        {
          "presets": [
            {
              "id": "default",
              "config": {
                "selected_preset_id": "default",
                "devices": {
                  "input_audio_device": null,
                  "output_audio_device": null
                },
                "models": {
                  "stt_primary": "azure:default",
                  "stt_fallback": "vosk:default",
                  "llm_primary": "ollama_cloud:mistral-large-3:675b-cloud",
                  "llm_fallback": "ollama_local:granite3.2:8b",
                  "tts_primary": "azure:default",
                  "tts_fallback": "piper:default"
                }
              }
            }
          ]
        }
        """.strip(),
        encoding="utf-8",
    )
    (configs_dir / "agent_presets.json").write_text(
        """
        {
          "presets": [
            {
              "id": "alex",
              "label": "Alex",
              "startup_allowed": true,
              "config": {
                "nao_base_url": "http://127.0.0.1:5103",
                "behavior_manager_url": "http://127.0.0.1:5203",
                "nao_ip": "192.168.0.10",
                "nao_ip_enabled": true,
                "base_enabled": true,
                "behavior_enabled": false,
                "base_autostart": true,
                "behavior_autostart": false,
                "output_target": "nao",
                "piper_model_path": ""
              }
            },
            {
              "id": "virtuele_robot",
              "label": "Virtuele robot",
              "startup_allowed": true,
              "config": {
                "base_enabled": false,
                "behavior_enabled": false,
                "nao_ip_enabled": false,
                "base_autostart": false,
                "behavior_autostart": false,
                "output_target": "server",
                "piper_model_path": ""
              }
            }
          ]
        }
        """.strip(),
        encoding="utf-8",
    )
    return dm_root


def test_replay_mic_capture_and_stream_delay(tmp_path: Path):
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    (fixtures / "hoi.wav").write_bytes(b"RIFFdemo")
    registry = demo_gate.ReplayMicRegistry(fixtures)
    mic = registry.mic_for("demo_gate_5301", mode="continuous")
    mic.queue_fixture(name="hoi", wav_bytes=b"RIFFdemo", delay_before_s=0.05)
    started = time.perf_counter()
    audio = mic.capture_utterance(timeout_s=0.2)
    elapsed = time.perf_counter() - started
    assert audio.pcm == b"RIFFdemo"
    assert elapsed >= 0.04

    seen = {"timeouts": 0, "utterances": 0}
    stop_event = threading.Event()

    def _stream():
        mic.stream_utterances(
            stop_event,
            timeout_s=0.05,
            on_utterance=lambda _audio: seen.__setitem__("utterances", seen["utterances"] + 1),
            on_timeout=lambda: seen.__setitem__("timeouts", seen["timeouts"] + 1),
        )

    worker = threading.Thread(target=_stream, daemon=True)
    worker.start()
    time.sleep(0.08)
    registry.queue_fixture("demo_gate_5301", mode="continuous", fixture_name="hoi")
    deadline = time.time() + 0.5
    while time.time() < deadline and seen["utterances"] < 1:
        time.sleep(0.01)
    stop_event.set()
    worker.join(timeout=0.5)
    assert seen["timeouts"] >= 1
    assert seen["utterances"] == 1


def test_in_process_dm_client_matches_script_runner_methods(monkeypatch, tmp_path: Path):
    app = _make_app(monkeypatch, tmp_path)
    client = demo_gate.InProcessDMClient(app, "http://127.0.0.1:5301", sid="demo_sid")

    caps = client.capabilities()
    assert caps["supports"]["say"] is True
    assert client.summary_page_url() == "http://127.0.0.1:5301/summary"

    effective = client.runtime_effective()
    assert effective["ok"] is True

    state_before = client.state()
    assert state_before["history"] == []
    reset = client.reset()
    assert reset["history"] == []

    lease = client.auto_rest_suspend_acquire(
        lease_id="lease-1",
        owner="demo_gate",
        reason="test",
        ttl_s=30,
    )
    assert lease["lease_id"] == "lease-1"
    released = client.auto_rest_suspend_release(lease_id="lease-1")
    assert released["lease_id"] == "lease-1"

    health = client.runtime_health(
        {
            "nao_base_url": "http://base:5000",
            "behavior_manager_url": "http://behavior:5001",
            "nao_ip": "192.168.1.10",
            "nao_ip_enabled": False,
            "base_enabled": False,
            "behavior_enabled": False,
        }
    )
    assert health["ok"] is True

    process_status = client.process_status()
    assert process_status["processes"]["base"]["running"] is False
    assert client.process_stop("base")["ok"] is True

    aborted = client.summary_abort()
    assert aborted["session"]["status"] == "aborted"


def test_runner_event_recorder_and_transition_helper():
    recorder = demo_gate.RunnerEventRecorder()

    def _emit_later():
        time.sleep(0.05)
        recorder.append({"type": "summary_state", "summary_waiting": True})

    worker = threading.Thread(target=_emit_later, daemon=True)
    worker.start()
    index, event = recorder.wait_for(
        lambda item: str(item.get("type") or "") == "summary_state" and bool(item.get("summary_waiting")),
        timeout_s=0.5,
    )
    worker.join(timeout=0.2)
    assert index == 0
    assert event["summary_waiting"] is True

    transition = demo_gate._latest_transition_for_command(
        [
            {"data": {"command": "REST", "new_mode": "PERFORMING"}},
            {"data": {"command": "STOP", "new_mode": "DIALOG"}},
            {"data": {"command": "STAND\\_UP", "new_mode": "PERFORMING", "new_active_behavior": "STAND\\_UP"}},
        ],
        "STAND_UP",
    )
    assert transition["data"]["new_active_behavior"] == "STAND\\_UP"

    assert demo_gate._scenarios_for_profile(
        demo_gate.PROFILE_OFFLINE,
        [
            demo_gate.SCENARIO_HAPPY_PATH,
            demo_gate.SCENARIO_SUMMARY_EDIT,
            demo_gate.SCENARIO_SERVICE_LOSS,
            demo_gate.SCENARIO_FULL_REHEARSAL,
        ],
    ) == [demo_gate.SCENARIO_SERVICE_LOSS, demo_gate.SCENARIO_FULL_REHEARSAL]
    assert demo_gate._scenarios_for_profile(
        demo_gate.PROFILE_LIVE_ROBOT,
        [demo_gate.SCENARIO_SERVICE_LOSS, demo_gate.SCENARIO_FULL_REHEARSAL, demo_gate.SCENARIO_HAPPY_PATH],
    ) == [demo_gate.SCENARIO_SERVICE_LOSS, demo_gate.SCENARIO_FULL_REHEARSAL]
    assert demo_gate._is_live_robot_profile(demo_gate.PROFILE_LIVE_ROBOT) is True
    assert demo_gate._is_live_robot_profile(demo_gate.PROFILE_OFFLINE) is False
    assert demo_gate.LIVE_ROBOT_SETTLE_STAND_UP_S == 4.0
    assert demo_gate.LIVE_ROBOT_SETTLE_WALK_WITH_ME_S == 5.0
    assert demo_gate.LIVE_ROBOT_SETTLE_STOP_S == 4.0
    assert demo_gate.LIVE_ROBOT_SETTLE_REST_S == 4.0


def test_demo_gate_reporter_writes_operator_and_technical_logs(tmp_path: Path):
    lines = []
    reporter = demo_gate.DemoGateReporter(tmp_path / "demo_gate_artifacts", console_write=lines.append)

    reporter.run_start(profile=demo_gate.PROFILE_OFFLINE, scenario=demo_gate.SCENARIO_HAPPY_PATH)
    reporter.startup_preparing()
    reporter.scenario_start(demo_gate.SCENARIO_HAPPY_PATH)
    reporter.simulate_user_speech("hoi")
    reporter.processing()
    reporter.chat_reply(llm_type="echo")
    reporter.technical("EXECUTE (dry-run): STAND_UP resolved={}")
    reporter.close()

    operator_text = reporter.operator_log_path.read_text(encoding="utf-8")
    technical_text = reporter.technical_log_path.read_text(encoding="utf-8")

    assert "Demo-gate start: profiel=offline, scenario=chat." in operator_text
    assert "Operatorlog:" in operator_text
    assert "Uitgebreide runnerlogs:" in operator_text
    assert "Services en verbindingen worden voorbereid; dit kan even duren." in operator_text
    assert "Gebruikersspraak simuleren: 'hoi'" in operator_text
    assert "Chat met echo LLM stuurde TTS-bericht." in operator_text
    assert "EXECUTE (dry-run)" not in operator_text
    assert "EXECUTE (dry-run): STAND_UP resolved={}" in technical_text
    assert any("Gebruikersspraak simuleren: 'hoi'" in line for line in lines)


def test_demo_gate_reporter_setup_failure_is_operator_friendly(tmp_path: Path):
    lines = []
    reporter = demo_gate.DemoGateReporter(tmp_path / "demo_gate_artifacts", console_write=lines.append)

    reporter.run_start(profile=demo_gate.PROFILE_LIVE_ROBOT, scenario=demo_gate.SCENARIO_FULL_REHEARSAL)
    reporter.setup_failure(scenario=demo_gate.SCENARIO_FULL_REHEARSAL, detail="Robot backend did not become ready.")
    reporter.close()

    operator_text = reporter.operator_log_path.read_text(encoding="utf-8")
    assert "Setup mislukt voor scenario: rehearsal." in operator_text
    assert "Fout: Robot backend did not become ready." in operator_text
    assert any("Setup mislukt voor scenario: rehearsal." in line for line in lines)


def test_summary_generate_timeout_prefers_cloud_budget():
    offline = demo_gate.DemoGateHarness(profile=demo_gate.PROFILE_OFFLINE)
    offline_models = offline.summary_config["models"]
    assert offline_models["llm_fallback"] == "echo:default"
    assert offline._summary_generate_timeout_s(execution_mode="local") == 3.0
    assert offline._summary_generate_timeout_s(execution_mode="cloud") == 45.0

    live = demo_gate.DemoGateHarness(profile=demo_gate.PROFILE_LIVE_SERVICES)
    live_models = live.summary_config["models"]
    assert live_models["llm_fallback"] == "ollama_local:granite3.2:8b"
    assert live._summary_generate_timeout_s(execution_mode="local") == 180.0
    assert live._summary_generate_timeout_s(execution_mode="cloud") == 45.0


def test_live_robot_uses_selected_startup_preset_without_port_runtime_override(tmp_path: Path, monkeypatch):
    dm_root = _write_demo_gate_test_config_root(tmp_path)
    summary_script = tmp_path / "summary.json"
    summary_script.write_text(
        """
        {
          "version": 1,
          "robots": {
            "nao1": { "dm_url": "http://127.0.0.1:5301" }
          },
          "steps": []
        }
        """.strip(),
        encoding="utf-8",
    )
    workshop_script = tmp_path / "workshop.json"
    workshop_script.write_text(
        """
        {
          "version": 1,
          "robots": {
            "nao1": { "dm_url": "http://127.0.0.1:5301" },
            "nao2": { "dm_url": "http://127.0.0.1:5302" }
          },
          "steps": []
        }
        """.strip(),
        encoding="utf-8",
    )
    runtime_port_override = dm_root / "configs" / "runtime" / "runtime_port_5301.json"
    runtime_port_override.parent.mkdir(parents=True, exist_ok=True)
    runtime_port_override.write_text(
        """
        {
          "nao_base_url": "http://127.0.0.1:5999",
          "behavior_manager_url": "http://127.0.0.1:5998",
          "nao_ip": "192.168.0.250",
          "nao_ip_enabled": true,
          "base_enabled": true,
          "behavior_enabled": false,
          "base_autostart": true,
          "behavior_autostart": false,
          "output_target": "nao",
          "piper_model_path": ""
        }
        """.strip(),
        encoding="utf-8",
    )
    monkeypatch.setattr(demo_gate, "DM_ROOT", dm_root)
    monkeypatch.setattr(demo_gate, "DEFAULT_DM_CONFIG_PATH", dm_root / "configs" / "default.json")
    monkeypatch.setattr(demo_gate, "DEFAULT_SUMMARY_PRESETS_PATH", dm_root / "configs" / "summary_presets.json")
    monkeypatch.setitem(
        demo_gate.DEFAULT_STARTUP_PRESET_BY_PROFILE,
        demo_gate.PROFILE_LIVE_ROBOT,
        "alex",
    )
    monkeypatch.setattr(
        webapp_server,
        "_load_startup_agent_preset_config",
        lambda preset_id: {
            "nao_base_url": "http://127.0.0.1:5103",
            "behavior_manager_url": "http://127.0.0.1:5203",
            "nao_ip": "192.168.0.10",
            "nao_ip_enabled": True,
            "base_enabled": True,
            "behavior_enabled": False,
            "base_autostart": True,
            "behavior_autostart": False,
            "output_target": "nao",
            "piper_model_path": "",
        },
    )

    harness = demo_gate.DemoGateHarness(
        profile=demo_gate.PROFILE_LIVE_ROBOT,
        preset="alex",
        summary_script_path=summary_script,
        workshop_script_path=summary_script,
    )

    assert harness.runtime_config["nao_base_url"] == "http://127.0.0.1:5103"
    assert harness.runtime_config["nao_ip"] == "192.168.0.10"


def test_live_robot_rejects_multi_robot_script_without_custom_orchestration(tmp_path: Path, monkeypatch):
    dm_root = _write_demo_gate_test_config_root(tmp_path)
    summary_script = tmp_path / "summary.json"
    summary_script.write_text(
        """
        {
          "version": 1,
          "robots": {
            "nao1": { "dm_url": "http://127.0.0.1:5301" }
          },
          "steps": []
        }
        """.strip(),
        encoding="utf-8",
    )
    workshop_script = tmp_path / "workshop.json"
    workshop_script.write_text(
        """
        {
          "version": 1,
          "robots": {
            "nao1": { "dm_url": "http://127.0.0.1:5301" },
            "nao2": { "dm_url": "http://127.0.0.1:5302" }
          },
          "steps": []
        }
        """.strip(),
        encoding="utf-8",
    )
    monkeypatch.setattr(demo_gate, "DM_ROOT", dm_root)
    monkeypatch.setattr(demo_gate, "DEFAULT_DM_CONFIG_PATH", dm_root / "configs" / "default.json")
    monkeypatch.setattr(demo_gate, "DEFAULT_SUMMARY_PRESETS_PATH", dm_root / "configs" / "summary_presets.json")
    monkeypatch.setitem(
        demo_gate.DEFAULT_STARTUP_PRESET_BY_PROFILE,
        demo_gate.PROFILE_LIVE_ROBOT,
        "alex",
    )
    monkeypatch.setattr(
        webapp_server,
        "_load_startup_agent_preset_config",
        lambda preset_id: {
            "nao_base_url": "http://127.0.0.1:5103",
            "behavior_manager_url": "http://127.0.0.1:5203",
            "nao_ip": "192.168.0.10",
            "nao_ip_enabled": True,
            "base_enabled": True,
            "behavior_enabled": False,
            "base_autostart": True,
            "behavior_autostart": False,
            "output_target": "nao",
            "piper_model_path": "",
        },
    )

    with pytest.raises(demo_gate.DemoGateError, match="single-robot scripts"):
        demo_gate.DemoGateHarness(
            profile=demo_gate.PROFILE_LIVE_ROBOT,
            summary_script_path=summary_script,
            workshop_script_path=workshop_script,
        )
