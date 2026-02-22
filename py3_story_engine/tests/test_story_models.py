from __future__ import annotations

import pytest

from storyteller.story_models import (
    BEAT_SEQUENCE,
    DEFAULT_BEAT_GATE_POLICY,
    MAX_ACTIVE_QUESTS,
    MAX_RELATIONSHIPS,
    MAX_THREADS,
    apply_story_patch,
    default_story_state,
    parse_state_updater_patch,
    parse_storyteller_output,
    validate_story_state,
)


def test_validate_story_state_rejects_phase_mismatch():
    state = default_story_state()
    state["phase"] = "trigger"
    with pytest.raises(ValueError):
        validate_story_state(state)


def test_parse_storyteller_output_requires_exactly_two_choices():
    with pytest.raises(ValueError):
        parse_storyteller_output('{"scene":"Hallo","choices":["a"]}', strict_json=True)

    parsed = parse_storyteller_output('{"scene":"Hallo","choices":["a","b"]}', strict_json=True)
    assert parsed["scene"] == "Hallo"
    assert parsed["choices"] == ["a", "b"]


def test_parse_storyteller_output_rejects_english_when_nl_required():
    with pytest.raises(ValueError):
        parse_storyteller_output(
            '{"scene":"The dragon descends from the mountain and watches you in silence.","choices":["Ask for peace talks","Draw your sword"]}',
            strict_json=True,
            required_language="nl",
        )


def test_parse_state_updater_patch_rejects_unknown_keys_in_strict_mode():
    raw = """
    {
      "last_scene_summary": "x",
      "facts": {"add": [], "remove": []},
      "threads": {"open": [], "resolve": []},
      "active_quests": {"add": [], "remove": []},
      "relationships": {"add": [], "remove": []},
      "player_traits": {"add": [], "remove": []},
      "player_inventory": {"add": [], "remove": []},
      "recommended_plot": {"advance": "stay", "reason": "ok"},
      "unexpected": true
    }
    """
    with pytest.raises(ValueError):
        parse_state_updater_patch(raw, strict_json=True)


def test_parse_state_updater_patch_requires_global_summary_when_enabled():
    raw = """
    {
      "last_scene_summary": "Korte scene samenvatting.",
      "facts": {"add": [], "remove": []},
      "threads": {"open": [], "resolve": []},
      "active_quests": {"add": [], "remove": []},
      "relationships": {"add": [], "remove": []},
      "player_traits": {"add": [], "remove": []},
      "player_inventory": {"add": [], "remove": []},
      "recommended_plot": {"advance": "stay", "reason": "ok"}
    }
    """
    with pytest.raises(ValueError):
        parse_state_updater_patch(raw, strict_json=True, require_global_summary=True)


def test_parse_state_updater_patch_rejects_english_summary_when_nl_required():
    raw = """
    {
      "global_summary": "You stand at the ancient gate while the city burns behind you.",
      "last_scene_summary": "You ask the guard for passage.",
      "facts": {"add": [], "remove": []},
      "threads": {"open": [], "resolve": []},
      "active_quests": {"add": [], "remove": []},
      "relationships": {"add": [], "remove": []},
      "player_traits": {"add": [], "remove": []},
      "player_inventory": {"add": [], "remove": []},
      "recommended_plot": {"advance": "stay", "reason": "ok"}
    }
    """
    with pytest.raises(ValueError):
        parse_state_updater_patch(
            raw,
            strict_json=True,
            required_language="nl",
            require_global_summary=True,
        )


def test_parse_state_updater_patch_normalizes_threads_resolve_objects_to_ids():
    patch = parse_state_updater_patch(
        """
        {
          "global_summary": "Samenvatting.",
          "last_scene_summary": "Scene.",
          "facts": {"add": [], "remove": []},
          "threads": {
            "open": [],
            "resolve": [
              {"id": "Wurgstokkel verstoort golem's pulsatie", "notes": ""},
              "Nibs vormt waterbarrière en verjaagt golem",
              {"id": "Wurgstokkel verstoort golem's pulsatie", "notes": "dup"}
            ]
          },
          "active_quests": {"add": [], "remove": []},
          "relationships": {"add": [], "remove": []},
          "player_traits": {"add": [], "remove": []},
          "player_inventory": {"add": [], "remove": []},
          "recommended_plot": {"advance": "stay", "reason": "ok"}
        }
        """,
        strict_json=True,
        required_language="nl",
        require_global_summary=True,
    )
    assert patch["threads"]["resolve"] == [
        "Wurgstokkel verstoort golem's pulsatie",
        "Nibs vormt waterbarrière en verjaagt golem",
    ]


def test_apply_story_patch_trigger_to_plan_requires_commit_and_quest_signal():
    state = default_story_state()
    state["beat_idx"] = 1
    state["phase"] = BEAT_SEQUENCE[1]
    patch = parse_state_updater_patch(
        """
        {
          "global_summary": "De held staat voor een keuze bij de poort.",
          "last_scene_summary": "De held overweegt het plan met de gids.",
          "facts": {"add": [], "remove": []},
          "threads": {"open": [{"id":"verken_poort","notes":""}], "resolve": []},
          "active_quests": {"add": ["Bereik de stad"], "remove": []},
          "relationships": {"add": [], "remove": []},
          "player_traits": {"add": [], "remove": []},
          "player_inventory": {"add": [], "remove": []},
          "recommended_plot": {"advance": "next", "reason": "De speler kiest een aanpak."}
        }
        """,
        strict_json=True,
        required_language="nl",
        require_global_summary=True,
    )
    out, beat_decision = apply_story_patch(
        state,
        patch,
        user_input="1",
        beat_gate_policy=DEFAULT_BEAT_GATE_POLICY,
        summary_policy="updater_only",
    )
    assert out["beat_idx"] == 2
    assert out["phase"] == BEAT_SEQUENCE[2]
    assert beat_decision["applied_advance"] == "next"


def test_apply_story_patch_trigger_to_plan_blocks_without_commit():
    state = default_story_state()
    state["beat_idx"] = 1
    state["phase"] = BEAT_SEQUENCE[1]
    patch = parse_state_updater_patch(
        """
        {
          "global_summary": "De held staat voor een keuze bij de poort.",
          "last_scene_summary": "De held overweegt het plan met de gids.",
          "facts": {"add": [], "remove": []},
          "threads": {"open": [{"id":"verken_poort","notes":""}], "resolve": []},
          "active_quests": {"add": ["Bereik de stad"], "remove": []},
          "relationships": {"add": [], "remove": []},
          "player_traits": {"add": [], "remove": []},
          "player_inventory": {"add": [], "remove": []},
          "recommended_plot": {"advance": "next", "reason": "De speler twijfelt nog."}
        }
        """,
        strict_json=True,
        required_language="nl",
        require_global_summary=True,
    )
    out, beat_decision = apply_story_patch(
        state,
        patch,
        user_input="hm misschien later",
        beat_gate_policy=DEFAULT_BEAT_GATE_POLICY,
        summary_policy="updater_only",
    )
    assert out["beat_idx"] == 1
    assert out["phase"] == BEAT_SEQUENCE[1]
    assert beat_decision["applied_advance"] == "stay"
    assert beat_decision["beat_gate"]["failed_checks"]


def test_apply_story_patch_next_on_last_beat_stays_last():
    state = default_story_state()
    state["beat_idx"] = len(BEAT_SEQUENCE) - 1
    state["phase"] = BEAT_SEQUENCE[-1]
    patch = parse_state_updater_patch(
        """
        {
          "global_summary": "De verhaallijn is afgerond.",
          "last_scene_summary": "Het slot is bereikt.",
          "facts": {"add": [], "remove": []},
          "threads": {"open": [], "resolve": []},
          "active_quests": {"add": [], "remove": []},
          "relationships": {"add": [], "remove": []},
          "player_traits": {"add": [], "remove": []},
          "player_inventory": {"add": [], "remove": []},
          "recommended_plot": {"advance": "next", "reason": "probeer door"}
        }
        """,
        strict_json=True,
        required_language="nl",
        require_global_summary=True,
    )
    out, beat_decision = apply_story_patch(state, patch, user_input="1", beat_gate_policy=DEFAULT_BEAT_GATE_POLICY)
    assert out["beat_idx"] == len(BEAT_SEQUENCE) - 1
    assert out["phase"] == BEAT_SEQUENCE[-1]
    assert beat_decision["applied_advance"] == "stay"


def test_apply_story_patch_uses_updater_global_summary_when_provided():
    state = default_story_state()
    state["global_summary"] = "Oude summary."
    patch = parse_state_updater_patch(
        """
        {
          "global_summary": "Nieuwe compacte samenvatting over de laatste turns.",
          "last_scene_summary": "Laatste scene.",
          "facts": {"add": [], "remove": []},
          "threads": {"open": [], "resolve": []},
          "active_quests": {"add": [], "remove": []},
          "relationships": {"add": [], "remove": []},
          "player_traits": {"add": [], "remove": []},
          "player_inventory": {"add": [], "remove": []},
          "recommended_plot": {"advance": "stay", "reason": "updater beslist"}
        }
        """,
        strict_json=True,
        required_language="nl",
        require_global_summary=True,
    )
    out, _ = apply_story_patch(state, patch, beat_gate_policy=DEFAULT_BEAT_GATE_POLICY)
    assert out["global_summary"] == "Nieuwe compacte samenvatting over de laatste turns."


def test_apply_story_patch_caps_lists_for_state_fields():
    state = default_story_state()
    patch = parse_state_updater_patch(
        """
        {
          "global_summary": "Samenvatting voor cap test.",
          "last_scene_summary": "Cap test scene.",
          "facts": {"add": [], "remove": []},
          "threads": {
            "open": [
              {"id":"t1","notes":""}, {"id":"t2","notes":""}, {"id":"t3","notes":""},
              {"id":"t4","notes":""}, {"id":"t5","notes":""}, {"id":"t6","notes":""},
              {"id":"t7","notes":""}, {"id":"t8","notes":""}, {"id":"t9","notes":""},
              {"id":"t10","notes":""}, {"id":"t11","notes":""}, {"id":"t12","notes":""},
              {"id":"t13","notes":""}
            ],
            "resolve": []
          },
          "active_quests": {
            "add": ["q1","q2","q3","q4","q5","q6","q7","q8","q9","q10","q11"],
            "remove": []
          },
          "relationships": {
            "add": ["r1","r2","r3","r4","r5","r6","r7","r8","r9","r10","r11","r12","r13"],
            "remove": []
          },
          "player_traits": {"add": [], "remove": []},
          "player_inventory": {"add": [], "remove": []},
          "recommended_plot": {"advance": "stay", "reason": "cap"}
        }
        """,
        strict_json=True,
        required_language="nl",
        require_global_summary=True,
    )
    out, _ = apply_story_patch(state, patch, beat_gate_policy=DEFAULT_BEAT_GATE_POLICY)
    assert len(out["active_quests"]) == MAX_ACTIVE_QUESTS
    assert len(out["relationships"]) == MAX_RELATIONSHIPS
    assert len(out["threads"]) == MAX_THREADS


def test_apply_story_patch_resolve_fallback_accepts_object_entries():
    state = default_story_state()
    state["threads"] = [{"id": "t1", "notes": ""}, {"id": "t2", "notes": ""}]
    patch = parse_state_updater_patch(
        """
        {
          "global_summary": "Samenvatting.",
          "last_scene_summary": "Scene.",
          "facts": {"add": [], "remove": []},
          "threads": {"open": [], "resolve": ["t1"]},
          "active_quests": {"add": [], "remove": []},
          "relationships": {"add": [], "remove": []},
          "player_traits": {"add": [], "remove": []},
          "player_inventory": {"add": [], "remove": []},
          "recommended_plot": {"advance": "stay", "reason": "ok"}
        }
        """,
        strict_json=True,
        required_language="nl",
        require_global_summary=True,
    )
    patch["threads"]["resolve"] = [{"id": "t1", "notes": ""}]
    out, _ = apply_story_patch(state, patch, beat_gate_policy=DEFAULT_BEAT_GATE_POLICY)
    assert out["threads"] == [{"id": "t2", "notes": ""}]
