from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Callable, Dict, Iterable, List, Optional

import pytest

import webapp_server
from dialog.interfaces import CommandDecision, LLMResult, RouteDecision, STTResult, UtteranceAudio


pytestmark = pytest.mark.integration


_FAKE_WAV_BYTES = b"RIFF\x24\x00\x00\x00WAVEfmt "


class StubLLMBackend:
    def __init__(self, reply: str = "mocked-reasoning-reply") -> None:
        self.reply = str(reply)
        self.calls = 0

    def generate(self, messages):
        self.calls += 1
        return LLMResult(reply=self.reply, messages=list(messages))


class QueueSTTBackend:
    def __init__(self, texts: Iterable[str]) -> None:
        self._queue = list(texts)
        self.calls = 0

    def transcribe(self, _audio):
        self.calls += 1
        text = self._queue.pop(0) if self._queue else ""
        return STTResult(text=text, language="nl", confidence=0.99)


class QueueMic:
    def __init__(self, utterance_count: int) -> None:
        self.calls = 0
        self._remaining = max(0, int(utterance_count))

    def capture_utterance(self, timeout_s: float = 10.0) -> UtteranceAudio:
        del timeout_s
        self.calls += 1
        if self._remaining > 0:
            self._remaining -= 1
            return UtteranceAudio(pcm=_FAKE_WAV_BYTES, sample_rate=16000, channels=1, sample_width=2)
        time.sleep(0.01)
        raise TimeoutError("no utterance queued")


class ScriptedCmdRec:
    def __init__(
        self,
        *,
        route_map: Optional[Dict[str, RouteDecision]] = None,
        dance_catalog: Optional[List[Dict[str, str]]] = None,
    ) -> None:
        self.calls = 0
        self.bundle_path = None
        self._route_map = {(k or "").strip().casefold(): v for (k, v) in (route_map or {}).items()}
        self._dance_catalog = list(dance_catalog or [])

    def route(self, text: str, _mode: str, _active_behavior):
        self.calls += 1
        key = (text or "").strip().casefold()
        if key in self._route_map:
            return self._route_map[key]
        return RouteDecision(
            is_command=False,
            command=None,
            reason="disabled",
            top3=[("NONE", 1.0)],
        )

    def is_guarded(self, _label: str) -> bool:
        return False

    def get_dance_catalog(self):
        return list(self._dance_catalog)


class SpyBehaviorExecutor:
    def __init__(self) -> None:
        self.calls = 0
        self.labels: List[str] = []
        self.resolved: List[Dict[str, str]] = []

    def set_custom_life_management_enabled(self, _enabled: bool) -> None:
        return

    def execute(self, cmd: CommandDecision) -> None:
        self.calls += 1
        self.labels.append(str(cmd.label))
        self.resolved.append(dict(cmd.resolved or {}))


def _decision(
    label: str,
    *,
    raw_text: Optional[str] = None,
    confidence: float = 0.99,
    resolved: Optional[Dict[str, str]] = None,
) -> RouteDecision:
    return RouteDecision(
        is_command=True,
        command=CommandDecision(
            label=label,
            confidence=float(confidence),
            raw_text=str(raw_text or label),
            resolved=resolved,
        ),
        reason="mock_rule",
        top3=[(label, float(confidence))],
    )


def _make_app(
    monkeypatch,
    *,
    stt_texts: Optional[Iterable[str]] = None,
    mic_utterances: int = 0,
    cmdrec: Optional[ScriptedCmdRec] = None,
    behavior_executor: Optional[SpyBehaviorExecutor] = None,
):
    llm = StubLLMBackend()
    stt = QueueSTTBackend(stt_texts or [])
    base_pipeline = SimpleNamespace(
        llm=llm,
        output=None,
        status_to_console=True,
        system_prompt="SYSTEM",
        log_messages_path=None,
        log_meta={},
        max_history_turns=None,
        _cmdrec=cmdrec,
        _debug_cmdrec=bool(cmdrec),
        _behavior_executor=behavior_executor,
    )

    monkeypatch.setattr(webapp_server, "build_pipeline_from_config", lambda *_a, **_k: base_pipeline)
    monkeypatch.setattr(webapp_server, "make_stt_backend_from_config", lambda *_a, **_k: stt)

    mic = QueueMic(utterance_count=mic_utterances) if mic_utterances > 0 else None
    if mic is not None:
        monkeypatch.setattr(webapp_server, "_make_mic_from_config", lambda *_a, **_k: mic)

    app, _, _ = webapp_server.create_app(cfg={}, config_path="<memory>")
    return app, llm, stt, mic, cmdrec, behavior_executor


def _prime_sid(client) -> None:
    resp = client.get("/api/state")
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True


def _clear_dm_events(client) -> None:
    resp = client.post("/api/dm_events_clear")
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True


def _send(client, text: str, *, emit: str = "none") -> Dict[str, object]:
    resp = client.post("/api/send", json={"text": text, "emit": emit})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    return data


def _wait_until(
    predicate: Callable[[], bool],
    *,
    timeout_s: float = 3.0,
    interval_s: float = 0.02,
) -> None:
    deadline = time.time() + float(timeout_s)
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(interval_s)
    raise AssertionError("timeout while waiting for condition")


def _history(client) -> List[Dict[str, object]]:
    resp = client.get("/api/state")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    return list(data.get("history") or [])


def _continuous_state(client) -> Dict[str, object]:
    resp = client.get("/api/continuous_state")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    return data


def _start_continuous(client) -> None:
    resp = client.post("/api/continuous_start")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["running"] is True


def _stop_continuous(client) -> None:
    resp = client.post("/api/continuous_stop")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["running"] is False


def _dm_events(client, *, event: Optional[str] = None) -> List[Dict[str, object]]:
    endpoint = "/api/dm_events?limit=2000"
    if event:
        endpoint += f"&event={event}"
    resp = client.get(endpoint)
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    return list(data.get("events") or [])


def _assistant_after_user(history: List[Dict[str, object]], user_text: str) -> str:
    for idx, item in enumerate(history):
        if item.get("role") != "user":
            continue
        if str(item.get("content") or "") != user_text:
            continue
        if idx + 1 >= len(history):
            break
        nxt = history[idx + 1]
        if nxt.get("role") == "assistant":
            return str(nxt.get("content") or "")
    raise AssertionError(f"assistant reply not found after user text: {user_text!r}")


def _latest_transition_for_command(events: List[Dict[str, object]], command: str) -> Dict[str, object]:
    command_norm = str(command or "").upper().strip()
    matches = [
        event
        for event in events
        if str(((event.get("data") or {}).get("command") or "")).upper().strip() == command_norm
    ]
    if not matches:
        raise AssertionError(f"state transition not found for command={command_norm}")
    return matches[-1]


def test_integration_flow_1_text_only_dialog(monkeypatch):
    cmdrec = ScriptedCmdRec()
    app, llm, _stt, _mic, _cmdrec, _executor = _make_app(monkeypatch, cmdrec=cmdrec)
    client = app.test_client()
    _prime_sid(client)
    _clear_dm_events(client)

    user_turns = ["hallo", "hoe gaat het", "vertel eens iets"]
    for text in user_turns:
        data = _send(client, text, emit="none")
        assert data["reply"] == "mocked-reasoning-reply"
        debug = data.get("debug") or {}
        assert debug.get("routed_as") == "dialog"

    hist = _history(client)
    assert [str(item.get("content") or "") for item in hist if item.get("role") == "user"] == user_turns
    assert all(
        str(item.get("content") or "") == "mocked-reasoning-reply"
        for item in hist
        if item.get("role") == "assistant"
    )
    assert llm.calls == 3

    transition_events = _dm_events(client, event="state_transition")
    assert transition_events == []


def test_integration_flow_2_text_dialog_and_commands_with_dance_resolving(monkeypatch):
    route_map = {
        "sta op": _decision("STAND_UP", raw_text="sta op"),
        "dans eens voor me": _decision("DANCE", raw_text="dans eens voor me", resolved={}),
        "rust": _decision("REST", raw_text="rust"),
    }
    dance_catalog = [{"key": "happy", "aliases": ["happy"], "behavior": "dances/happy"}]
    cmdrec = ScriptedCmdRec(route_map=route_map, dance_catalog=dance_catalog)
    executor = SpyBehaviorExecutor()
    app, _llm, _stt, _mic, _cmdrec, executor = _make_app(
        monkeypatch,
        cmdrec=cmdrec,
        behavior_executor=executor,
    )
    client = app.test_client()
    _prime_sid(client)
    _clear_dm_events(client)

    step1 = _send(client, "vertel iets", emit="none")
    assert step1["reply"] == "mocked-reasoning-reply"
    assert (step1.get("debug") or {}).get("routed_as") == "dialog"

    step2 = _send(client, "sta op", emit="none")
    assert step2["reply"] == "OK. Uitgevoerd: STAND_UP"
    assert (step2.get("debug") or {}).get("label") == "STAND_UP"
    assert executor.labels == ["STAND_UP"]

    step3 = _send(client, "dans eens voor me", emit="none")
    assert step3["reply"] == ""
    assert (step3.get("debug") or {}).get("label") == "DANCE"
    # dance resolving state: pending dance prompt is shown, command not executed yet.
    assert executor.labels == ["STAND_UP"]
    dance_prompt = _assistant_after_user(list(step3.get("history") or []), "dans eens voor me")
    assert dance_prompt
    assert not dance_prompt.startswith("OK. Uitgevoerd: DANCE")

    step4 = _send(client, "happy", emit="none")
    assert step4["reply"] == "OK. Uitgevoerd: DANCE"
    assert (step4.get("debug") or {}).get("label") == "DANCE"
    assert executor.labels == ["STAND_UP", "DANCE"]
    assert executor.resolved[-1].get("dance_key") == "happy"
    assert executor.resolved[-1].get("dance_behavior") == "dances/happy"

    step5 = _send(client, "dank je", emit="none")
    assert step5["reply"] == "mocked-reasoning-reply"
    assert (step5.get("debug") or {}).get("routed_as") == "dialog"

    step6 = _send(client, "rust", emit="none")
    assert step6["reply"] == "OK. Uitgevoerd: REST"
    assert (step6.get("debug") or {}).get("label") == "REST"
    assert executor.labels == ["STAND_UP", "DANCE", "REST"]

    transitions = _dm_events(client, event="state_transition")
    stand_up_transition = _latest_transition_for_command(transitions, "STAND_UP")
    assert (stand_up_transition.get("data") or {}).get("new_mode") == "PERFORMING"
    assert (stand_up_transition.get("data") or {}).get("new_active_behavior") == "STAND_UP"

    dance_transition = _latest_transition_for_command(transitions, "DANCE")
    assert (dance_transition.get("data") or {}).get("new_mode") == "PERFORMING"
    assert (dance_transition.get("data") or {}).get("new_active_behavior") == "DANCE"

    rest_transition = _latest_transition_for_command(transitions, "REST")
    assert (rest_transition.get("data") or {}).get("new_mode") == "PERFORMING"
    assert (rest_transition.get("data") or {}).get("new_active_behavior") == "REST"


def test_integration_flow_3_continuous_listening_dialog_only(monkeypatch):
    stt_texts = ["hallo", "vertel iets", "nog een vraag"]
    app, llm, _stt, _mic, _cmdrec, _executor = _make_app(
        monkeypatch,
        stt_texts=stt_texts,
        mic_utterances=len(stt_texts),
        cmdrec=None,
        behavior_executor=None,
    )
    client = app.test_client()
    _prime_sid(client)
    _clear_dm_events(client)

    _start_continuous(client)
    try:
        _wait_until(lambda: len(_history(client)) >= (len(stt_texts) * 2), timeout_s=4.0)
    finally:
        _stop_continuous(client)

    hist = _history(client)
    assert [str(item.get("content") or "") for item in hist if item.get("role") == "user"] == stt_texts
    assert all(
        str(item.get("content") or "") == "mocked-reasoning-reply"
        for item in hist
        if item.get("role") == "assistant"
    )
    assert llm.calls >= len(stt_texts)

    cont = _continuous_state(client)
    phases = {str(entry.get("phase") or "") for entry in (cont.get("phase_log") or [])}
    assert "listening" in phases
    assert "transcribing" in phases
    assert "thinking" in phases
    assert cont["phase"] == "stopped"


def test_integration_flow_4_continuous_listening_dialog_and_commands(monkeypatch):
    stt_texts = ["vertel iets", "sta op", "dans eens voor me", "happy", "nog iets", "rust"]
    route_map = {
        "sta op": _decision("STAND_UP", raw_text="sta op"),
        "dans eens voor me": _decision("DANCE", raw_text="dans eens voor me", resolved={}),
        "rust": _decision("REST", raw_text="rust"),
    }
    dance_catalog = [{"key": "happy", "aliases": ["happy"], "behavior": "dances/happy"}]
    cmdrec = ScriptedCmdRec(route_map=route_map, dance_catalog=dance_catalog)
    executor = SpyBehaviorExecutor()
    app, _llm, _stt, _mic, _cmdrec, executor = _make_app(
        monkeypatch,
        stt_texts=stt_texts,
        mic_utterances=len(stt_texts),
        cmdrec=cmdrec,
        behavior_executor=executor,
    )
    client = app.test_client()
    _prime_sid(client)
    _clear_dm_events(client)

    _start_continuous(client)
    try:
        _wait_until(lambda: len(_history(client)) >= (len(stt_texts) * 2), timeout_s=4.0)
        _wait_until(lambda: len(executor.labels) >= 3, timeout_s=4.0)
    finally:
        _stop_continuous(client)

    hist = _history(client)
    assert [str(item.get("content") or "") for item in hist if item.get("role") == "user"] == stt_texts
    assert executor.labels[:3] == ["STAND_UP", "DANCE", "REST"]
    dance_idx = executor.labels.index("DANCE")
    assert executor.resolved[dance_idx].get("dance_key") == "happy"
    assert executor.resolved[dance_idx].get("dance_behavior") == "dances/happy"

    dance_prompt = _assistant_after_user(hist, "dans eens voor me")
    assert dance_prompt
    assert not dance_prompt.startswith("OK. Uitgevoerd: DANCE")
    assert _assistant_after_user(hist, "happy") == "OK. Uitgevoerd: DANCE"
    assert _assistant_after_user(hist, "sta op") == "OK. Uitgevoerd: STAND_UP"
    assert _assistant_after_user(hist, "rust") == "OK. Uitgevoerd: REST"

    cont = _continuous_state(client)
    phases = {str(entry.get("phase") or "") for entry in (cont.get("phase_log") or [])}
    assert "listening" in phases
    assert "transcribing" in phases
    assert "thinking" in phases
    assert cont["phase"] == "stopped"


def test_integration_state_walk_with_me_performing_and_back_to_dialog_on_stop(monkeypatch):
    route_map = {
        "loop met me mee": _decision("WALK_WITH_ME", raw_text="loop met me mee"),
        "stop": _decision("STOP", raw_text="stop"),
    }
    cmdrec = ScriptedCmdRec(route_map=route_map)
    executor = SpyBehaviorExecutor()
    app, _llm, _stt, _mic, _cmdrec, executor = _make_app(
        monkeypatch,
        cmdrec=cmdrec,
        behavior_executor=executor,
    )
    client = app.test_client()
    _prime_sid(client)
    _clear_dm_events(client)

    walk_resp = _send(client, "loop met me mee", emit="none")
    assert walk_resp["reply"] == "OK. Uitgevoerd: WALK_WITH_ME"
    assert walk_resp["command_stop_available"] is True
    assert walk_resp["command_stop_label"] == "WALK_WITH_ME"

    # Dialog should not clear WALK_WITH_ME stop scope.
    _send(client, "lekker weer", emit="none")
    snapshot = client.get("/api/state").get_json()
    assert snapshot["command_stop_available"] is True
    assert snapshot["command_stop_label"] == "WALK_WITH_ME"

    stop_resp = _send(client, "stop", emit="none")
    assert stop_resp["reply"] == "OK. Uitgevoerd: STOP"
    assert stop_resp["command_stop_available"] is False

    assert executor.labels[:2] == ["WALK_WITH_ME", "STOP"]

    transitions = _dm_events(client, event="state_transition")
    walk_transition = _latest_transition_for_command(transitions, "WALK_WITH_ME")
    assert (walk_transition.get("data") or {}).get("new_mode") == "PERFORMING"
    assert (walk_transition.get("data") or {}).get("new_active_behavior") == "WALK_WITH_ME"

    stop_transition = _latest_transition_for_command(transitions, "STOP")
    assert (stop_transition.get("data") or {}).get("new_mode") == "DIALOG"
    assert (stop_transition.get("data") or {}).get("new_active_behavior") == ""


def test_integration_state_locomotion_performing_and_back_to_dialog_on_stop(monkeypatch):
    route_map = {
        "loop naar voren": _decision(
            "LOCOMOTION_REQUEST",
            raw_text="loop naar voren",
            resolved={"locomotion": "forward"},
        ),
        "stop": _decision("STOP", raw_text="stop"),
    }
    cmdrec = ScriptedCmdRec(route_map=route_map)
    executor = SpyBehaviorExecutor()
    app, _llm, _stt, _mic, _cmdrec, executor = _make_app(
        monkeypatch,
        cmdrec=cmdrec,
        behavior_executor=executor,
    )
    client = app.test_client()
    _prime_sid(client)
    _clear_dm_events(client)

    loc_resp = _send(client, "loop naar voren", emit="none")
    assert loc_resp["reply"] == "OK. Uitgevoerd: LOCOMOTION_REQUEST"
    assert loc_resp["command_stop_available"] is True
    assert loc_resp["command_stop_label"] == "LOCOMOTION_REQUEST"

    stop_resp = _send(client, "stop", emit="none")
    assert stop_resp["reply"] == "OK. Uitgevoerd: STOP"
    assert stop_resp["command_stop_available"] is False

    assert executor.labels[:2] == ["LOCOMOTION_REQUEST", "STOP"]
    assert executor.resolved[0].get("locomotion") == "forward"

    transitions = _dm_events(client, event="state_transition")
    loc_transition = _latest_transition_for_command(transitions, "LOCOMOTION_REQUEST")
    assert (loc_transition.get("data") or {}).get("new_mode") == "PERFORMING"
    assert (loc_transition.get("data") or {}).get("new_active_behavior") == "LOCOMOTION_REQUEST"

    stop_transition = _latest_transition_for_command(transitions, "STOP")
    assert (stop_transition.get("data") or {}).get("new_mode") == "DIALOG"
    assert (stop_transition.get("data") or {}).get("new_active_behavior") == ""
