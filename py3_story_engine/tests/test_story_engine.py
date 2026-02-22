from __future__ import annotations

import copy

import pytest

from storyteller.interfaces import LLMResult
from storyteller.story_engine import StoryLLMOutputRejectedError, StoryTurnCancelledError, run_story_turn
from storyteller.story_models import BEAT_SEQUENCE, DEFAULT_BEAT_GATE_POLICY, default_story_state


class SeqLLM:
    def __init__(self, replies):
        self._replies = list(replies)
        self.calls = 0

    def generate(self, messages):
        idx = self.calls
        self.calls += 1
        if idx >= len(self._replies):
            reply = self._replies[-1]
        else:
            reply = self._replies[idx]
        return LLMResult(reply=str(reply), messages=list(messages))


def _prompt_state_json(prompt: str):
    marker_start = "[STATE_JSON]\n"
    marker_end = "\n[RECENT_HISTORY]"
    if marker_start not in prompt or marker_end not in prompt:
        return {}
    tail = prompt.split(marker_start, 1)[1]
    blob = tail.split(marker_end, 1)[0].strip()
    import json

    return json.loads(blob)


def test_storyteller_invalid_then_retry_success():
    storyteller = SeqLLM([
        '{"scene":"The dragon watches you from the tower while the wind howls.","choices":["Ask for peace","Fight now"]}',
        '{"scene":"De draak kijkt je aan vanaf de toren en de wind giert over het plein.","choices":["Vraag om vrede","Trek je zwaard"]}',
    ])
    updater = SeqLLM(['{}'])

    turn = run_story_turn(
        storyteller_llm=storyteller,
        state_updater_llm=updater,
        storyteller_system_prompt="sys",
        state_updater_system_prompt="sys",
        state=default_story_state(),
        last_scene_text="",
        user_input="ik kies 1",
        history=[],
        strict_json=True,
        max_history_turns=3,
        required_language="nl",
        invalid_retry_count=1,
        require_global_summary=True,
        beat_gate_policy=DEFAULT_BEAT_GATE_POLICY,
    )

    validation = turn["logs"]["validation"]["storyteller"]
    assert validation["passed"] is True
    assert validation["attempts"] == 2
    assert len(validation["errors"]) == 1
    storyteller_log = turn["logs"]["storyteller"]
    retry_contexts = storyteller_log.get("retry_contexts") or []
    assert isinstance(retry_contexts, list)
    assert len(retry_contexts) == 1
    assert "Corrigeer volledig" in str(retry_contexts[0].get("repair_prompt") or "")
    next_input = retry_contexts[0].get("next_attempt_input") or {}
    assert str(next_input.get("user_prompt") or "").startswith("Vorige output was ongeldig.")
    attempt_inputs = storyteller_log.get("attempt_inputs") or []
    assert isinstance(attempt_inputs, list)
    assert len(attempt_inputs) == 2


def test_state_updater_invalid_then_retry_success():
    updater = SeqLLM([
        '{"last_scene_summary":"De held kijkt rond.","facts":{"add":[],"remove":[]},"threads":{"open":[],"resolve":[]},"active_quests":{"add":[],"remove":[]},"relationships":{"add":[],"remove":[]},"player_traits":{"add":[],"remove":[]},"player_inventory":{"add":[],"remove":[]},"recommended_plot":{"advance":"stay","reason":"ok"}}',
        '{"global_summary":"De held staat op het plein en zoekt een route naar de poort.","last_scene_summary":"De held kijkt rond en kiest een voorzichtige aanpak.","facts":{"add":[],"remove":[]},"threads":{"open":[],"resolve":[]},"active_quests":{"add":[],"remove":[]},"relationships":{"add":[],"remove":[]},"player_traits":{"add":[],"remove":[]},"player_inventory":{"add":[],"remove":[]},"recommended_plot":{"advance":"stay","reason":"Voorzichtig blijven."}}',
    ])
    storyteller = SeqLLM([
        '{"scene":"Je blijft rustig en verkent de omgeving stap voor stap.","choices":["Loop naar de poort","Vraag een passant om hulp"]}'
    ])

    turn = run_story_turn(
        storyteller_llm=storyteller,
        state_updater_llm=updater,
        storyteller_system_prompt="sys",
        state_updater_system_prompt="sys",
        state=default_story_state(),
        last_scene_text="De vorige scene staat hier en die is in het Nederlands.",
        user_input="1",
        history=[],
        strict_json=True,
        max_history_turns=3,
        required_language="nl",
        invalid_retry_count=1,
        require_global_summary=True,
        beat_gate_policy=DEFAULT_BEAT_GATE_POLICY,
    )

    validation = turn["logs"]["validation"]["state_updater"]
    assert validation["passed"] is True
    assert validation["attempts"] == 2
    assert len(validation["errors"]) == 1
    updater_log = turn["logs"]["state_updater"]
    retry_contexts = updater_log.get("retry_contexts") or []
    assert isinstance(retry_contexts, list)
    assert len(retry_contexts) == 1
    assert "Corrigeer volledig" in str(retry_contexts[0].get("repair_prompt") or "")
    next_input = retry_contexts[0].get("next_attempt_input") or {}
    assert str(next_input.get("user_prompt") or "").startswith("Vorige output was ongeldig.")
    attempt_inputs = updater_log.get("attempt_inputs") or []
    assert isinstance(attempt_inputs, list)
    assert len(attempt_inputs) == 2


def test_state_updater_invalid_twice_raises_and_preserves_input_state():
    state = default_story_state()
    snapshot = copy.deepcopy(state)
    updater = SeqLLM([
        '{"last_scene_summary":"The hero asks a question.","facts":{"add":[],"remove":[]},"threads":{"open":[],"resolve":[]},"active_quests":{"add":[],"remove":[]},"relationships":{"add":[],"remove":[]},"player_traits":{"add":[],"remove":[]},"player_inventory":{"add":[],"remove":[]},"recommended_plot":{"advance":"next","reason":"go"}}',
        '{"last_scene_summary":"Still english output.","facts":{"add":[],"remove":[]},"threads":{"open":[],"resolve":[]},"active_quests":{"add":[],"remove":[]},"relationships":{"add":[],"remove":[]},"player_traits":{"add":[],"remove":[]},"player_inventory":{"add":[],"remove":[]},"recommended_plot":{"advance":"next","reason":"go"}}',
    ])
    storyteller = SeqLLM(['{"scene":"Dit mag niet bereikt worden.","choices":["a","b"]}'])

    with pytest.raises(ValueError):
        run_story_turn(
            storyteller_llm=storyteller,
            state_updater_llm=updater,
            storyteller_system_prompt="sys",
            state_updater_system_prompt="sys",
            state=state,
            last_scene_text="Vorige scene in Nederlands.",
            user_input="1",
            history=[],
            strict_json=True,
            max_history_turns=3,
            required_language="nl",
            invalid_retry_count=1,
            require_global_summary=True,
            beat_gate_policy=DEFAULT_BEAT_GATE_POLICY,
        )

    assert state == snapshot


def test_turn_contains_beat_gate_details_when_updater_runs():
    state = default_story_state()
    state["beat_idx"] = 1
    state["phase"] = BEAT_SEQUENCE[1]

    updater = SeqLLM([
        '{"global_summary":"De held staat bij de poort en wil naar binnen.","last_scene_summary":"De held kiest om het plan te volgen.","facts":{"add":[],"remove":[]},"threads":{"open":[{"id":"plan_poort","notes":""}],"resolve":[]},"active_quests":{"add":["Bereik de binnenstad"],"remove":[]},"relationships":{"add":[],"remove":[]},"player_traits":{"add":[],"remove":[]},"player_inventory":{"add":[],"remove":[]},"recommended_plot":{"advance":"next","reason":"De speler kiest actie."}}'
    ])
    storyteller = SeqLLM([
        '{"scene":"Je zet de eerste stap van het plan richting de stadspoort.","choices":["Ga door","Stop en observeer"]}'
    ])

    turn = run_story_turn(
        storyteller_llm=storyteller,
        state_updater_llm=updater,
        storyteller_system_prompt="sys",
        state_updater_system_prompt="sys",
        state=state,
        last_scene_text="De poortwachter kijkt je aan.",
        user_input="1",
        history=[],
        strict_json=True,
        max_history_turns=3,
        required_language="nl",
        invalid_retry_count=1,
        require_global_summary=True,
        beat_gate_policy=DEFAULT_BEAT_GATE_POLICY,
    )

    gate = turn["beat_decision"]["beat_gate"]
    assert isinstance(gate, dict)
    assert gate["transition"] == "trigger->plan"
    assert gate["requested"] == "next"
    assert gate["applied"] == "next"


def test_storyteller_projection_omits_empty_optional_state_fields():
    state = default_story_state()
    storyteller = SeqLLM(
        ['{"scene":"De poort staat open en je kijkt voorzichtig om je heen.","choices":["Loop verder","Blijf staan"]}']
    )
    updater = SeqLLM(["{}"])

    turn = run_story_turn(
        storyteller_llm=storyteller,
        state_updater_llm=updater,
        storyteller_system_prompt="sys",
        state_updater_system_prompt="sys",
        state=state,
        last_scene_text="",
        user_input="1",
        history=[],
        strict_json=True,
        max_history_turns=3,
        required_language="nl",
        invalid_retry_count=1,
        require_global_summary=True,
        beat_gate_policy=DEFAULT_BEAT_GATE_POLICY,
    )
    state_json = _prompt_state_json(turn["logs"]["storyteller"]["user_prompt"])
    assert "relationships" not in state_json
    assert "player_traits" not in state_json
    assert "player_inventory" not in state_json


def test_storyteller_projection_includes_nonempty_optional_state_fields():
    state = default_story_state()
    state["relationships"] = ["De gids vertrouwt je."]
    state["player_traits"] = ["moedig"]
    state["player_inventory"] = ["kompas"]
    storyteller = SeqLLM(
        ['{"scene":"De gids knikt en wijst naar het pad door de mist.","choices":["Volg het pad","Vraag om meer uitleg"]}']
    )
    updater = SeqLLM(["{}"])

    turn = run_story_turn(
        storyteller_llm=storyteller,
        state_updater_llm=updater,
        storyteller_system_prompt="sys",
        state_updater_system_prompt="sys",
        state=state,
        last_scene_text="",
        user_input="de gids vertrouwt me, ik kies 1",
        history=[],
        strict_json=True,
        max_history_turns=3,
        required_language="nl",
        invalid_retry_count=1,
        require_global_summary=True,
        beat_gate_policy=DEFAULT_BEAT_GATE_POLICY,
    )
    state_json = _prompt_state_json(turn["logs"]["storyteller"]["user_prompt"])
    assert state_json.get("relationships") == ["De gids vertrouwt je."]
    assert state_json.get("player_traits") == ["moedig"]
    assert state_json.get("player_inventory") == ["kompas"]


def test_story_turn_cancelled_after_updater_skips_storyteller_call():
    state = default_story_state()
    updater = SeqLLM(
        [
            '{"global_summary":"De held staat op het plein.","last_scene_summary":"De held kiest een route.","facts":{"add":[],"remove":[]},"threads":{"open":[],"resolve":[]},"active_quests":{"add":[],"remove":[]},"relationships":{"add":[],"remove":[]},"player_traits":{"add":[],"remove":[]},"player_inventory":{"add":[],"remove":[]},"recommended_plot":{"advance":"stay","reason":"Nog geen beat shift."}}'
        ]
    )
    storyteller = SeqLLM(
        ['{"scene":"Dit mag niet meer gegenereerd worden.","choices":["A","B"]}']
    )
    checks = {"count": 0}

    def should_abort() -> bool:
        checks["count"] += 1
        return checks["count"] >= 3

    with pytest.raises(StoryTurnCancelledError):
        run_story_turn(
            storyteller_llm=storyteller,
            state_updater_llm=updater,
            storyteller_system_prompt="sys",
            state_updater_system_prompt="sys",
            state=state,
            last_scene_text="Vorige scene is aanwezig.",
            user_input="ik kies 1",
            history=[],
            strict_json=True,
            max_history_turns=3,
            required_language="nl",
            invalid_retry_count=1,
            require_global_summary=True,
            beat_gate_policy=DEFAULT_BEAT_GATE_POLICY,
            should_abort=should_abort,
        )

    assert updater.calls == 1
    assert storyteller.calls == 0


def test_story_rejected_error_exposes_llm_attempt_outputs():
    state = default_story_state()
    updater = SeqLLM(
        [
            '{"last_scene_summary":"This is english.","facts":{"add":[],"remove":[]},"threads":{"open":[],"resolve":[]},"active_quests":{"add":[],"remove":[]},"relationships":{"add":[],"remove":[]},"player_traits":{"add":[],"remove":[]},"player_inventory":{"add":[],"remove":[]},"recommended_plot":{"advance":"stay","reason":"still english"}}'
        ]
    )
    storyteller = SeqLLM(
        ['{"scene":"Deze call mag niet gebeuren.","choices":["A","B"]}']
    )

    with pytest.raises(StoryLLMOutputRejectedError) as excinfo:
        run_story_turn(
            storyteller_llm=storyteller,
            state_updater_llm=updater,
            storyteller_system_prompt="sys",
            state_updater_system_prompt="sys",
            state=state,
            last_scene_text="Vorige scene is aanwezig.",
            user_input="ik kies 1",
            history=[],
            strict_json=True,
            max_history_turns=3,
            required_language="nl",
            invalid_retry_count=0,
            require_global_summary=True,
            beat_gate_policy=DEFAULT_BEAT_GATE_POLICY,
        )

    err = excinfo.value
    assert err.role_label == "StateUpdater"
    assert err.attempts == 1
    assert isinstance(err.attempt_outputs, list)
    assert len(err.attempt_outputs) == 1
    assert "english" in err.attempt_outputs[0].lower()
    assert isinstance(err.attempt_inputs, list)
    assert len(err.attempt_inputs) == 1
    assert isinstance(err.retry_contexts, list)
    assert err.retry_contexts == []
    assert storyteller.calls == 0
