from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from py3_script_runner.tts_preload import TtsPreloadService, TtsPreloadStore


def _script(*, text: str = "Welkom", script_id: str = "script-demo") -> Dict[str, Any]:
    return {
        "version": 1,
        "meta": {"script_id": script_id},
        "robots": {
            "nao1": {
                "dm_url": "http://127.0.0.1:5301",
                "runtime_config": {"tts_engine": "azure", "azure_tts_voice": "voice-a"},
            }
        },
        "defaults": {"request_timeout_s": 12, "on_error": "prompt"},
        "steps": [
            {
                "id": "say_1",
                "robot_id": "nao1",
                "start": {"mode": "manual", "delay_s": 0},
                "action": {"type": "say", "text": text},
            }
        ],
    }


class _FakeClient:
    def __init__(self, base_url: str, timeout_s: float, *, render_calls: list[tuple[str, str]]) -> None:
        self.base_url = base_url
        self.timeout_s = timeout_s
        self._render_calls = render_calls

    def script_tts_profile(self, *, runtime_config_override: Optional[Dict[str, Any]] = None, timeout_s=None):
        override = runtime_config_override if isinstance(runtime_config_override, dict) else {}
        voice = str(override.get("azure_tts_voice") or "voice-a")
        fingerprint = f"fp:{voice}"
        return {
            "ok": True,
            "supported": True,
            "engine": "azure",
            "fingerprint": fingerprint,
            "summary": f"Azure | {voice}",
            "details": {"engine": "azure", "voice": voice},
        }

    def script_tts_render(self, *, text: str, runtime_config_override: Optional[Dict[str, Any]] = None, timeout_s=None) -> bytes:
        override = runtime_config_override if isinstance(runtime_config_override, dict) else {}
        voice = str(override.get("azure_tts_voice") or "voice-a")
        self._render_calls.append((voice, str(text)))
        return f"WAV:{voice}:{text}".encode("utf-8")


def _service(tmp_path: Path, render_calls: list[tuple[str, str]]) -> TtsPreloadService:
    store = TtsPreloadStore(root=tmp_path / "preloaded_tts")
    return TtsPreloadService(
        store=store,
        client_factory=lambda url, timeout_s: _FakeClient(url, timeout_s, render_calls=render_calls),
    )


def test_generate_reuses_single_clip_for_duplicate_text_and_reorder(tmp_path):
    render_calls: list[tuple[str, str]] = []
    service = _service(tmp_path, render_calls)
    script = _script()
    script["steps"].append(
        {
            "id": "say_2",
            "robot_id": "nao1",
            "start": {"mode": "after_prev", "delay_s": 0},
            "action": {"type": "say", "text": "Welkom"},
        }
    )

    result = service.generate(script)

    assert result["generated_count"] == 1
    assert result["reused_count"] == 1
    assert render_calls == [("voice-a", "Welkom")]

    manifest = service.store.load_manifest("script-demo")
    step_entries = manifest["steps"]
    assert step_entries[0]["profiles"]["fp:voice-a"]["clip_id"] == step_entries[1]["profiles"]["fp:voice-a"]["clip_id"]

    reordered = _script()
    reordered["steps"] = list(reversed(script["steps"]))
    status = service.status(reordered)
    assert status["robots"][0]["status"] == "current_ready"
    assert status["robots"][0]["current_missing_count"] == 0


def test_generate_only_renders_missing_texts_and_keeps_old_clip(tmp_path):
    render_calls: list[tuple[str, str]] = []
    service = _service(tmp_path, render_calls)
    initial = _script(text="Hallo")
    first = service.generate(initial)
    assert first["generated_count"] == 1

    manifest_before = service.store.load_manifest("script-demo")
    old_rel_path = manifest_before["steps"][0]["profiles"]["fp:voice-a"]["clip_rel_path"]
    assert service.store.clip_exists(old_rel_path)

    changed = _script(text="Nieuwe zin")
    second = service.generate(changed)

    assert second["generated_count"] == 1
    assert second["reused_count"] == 0
    assert render_calls == [("voice-a", "Hallo"), ("voice-a", "Nieuwe zin")]
    assert service.store.clip_exists(old_rel_path)


def test_generate_removes_manifest_reference_but_prune_deletes_only_orphans(tmp_path):
    render_calls: list[tuple[str, str]] = []
    service = _service(tmp_path, render_calls)
    service.generate(_script(text="Te verwijderen"))

    script_without_say = _script(text="Blijft niet")
    script_without_say["steps"] = [
        {
            "id": "pause_1",
            "start": {"mode": "manual", "delay_s": 0},
            "action": {"type": "pause", "seconds": 1},
        }
    ]
    service.generate(script_without_say)

    manifest = service.store.load_manifest("script-demo")
    assert manifest["steps"] == []

    index = service.store.refresh_index_from_manifests()
    clip_entries = index.get("clips", {})
    assert len(clip_entries) == 1
    clip_id, clip_entry = next(iter(clip_entries.items()))
    assert clip_entry["orphaned_at"]

    old_orphaned = (datetime.now(timezone.utc) - timedelta(days=20)).isoformat()
    clip_entry["orphaned_at"] = old_orphaned
    index["clips"][clip_id] = clip_entry
    service.store.save_index(index)

    prune = service.store.prune("14d")
    assert prune["deleted_count"] == 1
    assert prune["remaining_count"] == 0


def test_status_and_resolve_run_plan_support_existing_profile_choice(tmp_path):
    render_calls: list[tuple[str, str]] = []
    service = _service(tmp_path, render_calls)
    initial = _script(text="Offline demo")
    service.generate(initial)

    switched = _script(text="Offline demo")
    switched["robots"]["nao1"]["runtime_config"]["azure_tts_voice"] = "voice-b"
    status = service.status(switched)

    robot = status["robots"][0]
    assert robot["status"] == "mismatch_existing"
    assert robot["current_profile"]["fingerprint"] == "fp:voice-b"
    assert robot["existing_profile"]["fingerprint"] == "fp:voice-a"

    plan = service.resolve_run_plan(
        switched,
        {
            "nao1": {
                "mode": "existing",
                "profile_fingerprint": "fp:voice-a",
            }
        },
    )
    assert plan["robot_modes"]["nao1"] == "preloaded_existing"
    assert "say_1" in plan["step_audio"]
    assert plan["step_audio"]["say_1"]["profile_fingerprint"] == "fp:voice-a"
