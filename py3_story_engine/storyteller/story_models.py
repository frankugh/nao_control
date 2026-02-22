from __future__ import annotations

import copy
import json
import re
from typing import Any, Dict, List, Optional, Tuple

StoryState = Dict[str, Any]
StoryPatch = Dict[str, Any]

BEAT_SEQUENCE: List[str] = [
    "setup",
    "trigger",
    "plan",
    "complication",
    "reversal",
    "dark_moment",
    "climax",
    "aftermath",
]

DEFAULT_BEAT_GATE_POLICY = "conservative_v1"

MAX_PINNED_FACTS = 12
MAX_PLAYER_TRAITS = 5
MAX_PLAYER_INVENTORY = 5
MAX_ACTIVE_QUESTS = 10
MAX_RELATIONSHIPS = 12
MAX_THREADS = 12

_NL_STOPWORDS = {
    "de",
    "het",
    "een",
    "en",
    "van",
    "op",
    "in",
    "met",
    "voor",
    "dat",
    "die",
    "dit",
    "als",
    "maar",
    "dan",
    "om",
    "naar",
    "aan",
    "te",
    "tot",
    "door",
    "uit",
    "bij",
    "niet",
    "wel",
    "nog",
    "hier",
    "daar",
    "ik",
    "jij",
    "je",
    "wij",
    "we",
    "jullie",
    "hun",
    "mijn",
    "zijn",
    "haar",
    "ons",
    "hoe",
    "wat",
    "waarom",
    "welke",
}

_EN_STOPWORDS = {
    "the",
    "a",
    "an",
    "and",
    "or",
    "of",
    "to",
    "for",
    "from",
    "with",
    "in",
    "on",
    "at",
    "by",
    "is",
    "are",
    "was",
    "were",
    "be",
    "been",
    "being",
    "this",
    "that",
    "these",
    "those",
    "you",
    "your",
    "we",
    "our",
    "they",
    "their",
    "as",
    "if",
    "then",
    "than",
}

_OBSTACLE_KEYWORDS_NL = {
    "obstakel",
    "hindernis",
    "tegenstand",
    "tegenwerking",
    "gevaar",
    "dreiging",
    "barriere",
    "blokkade",
    "versperd",
    "mislukt",
    "mislukking",
    "vastgelopen",
    "aanval",
    "strijd",
}

_REVERSAL_KEYWORDS_NL = {
    "wending",
    "ommekeer",
    "omkering",
    "onthulling",
    "verraad",
    "plotseling",
    "keert",
    "draait",
    "blijkt",
    "toch",
}

_SETBACK_KEYWORDS_NL = {
    "verlies",
    "verliest",
    "verloren",
    "hopeloos",
    "impasse",
    "wanhoop",
    "faalt",
    "mislukt",
    "verslagen",
    "laagste punt",
    "opgegeven",
    "instorting",
    "duister",
}

_USER_COMMIT_PATTERNS = (
    "ik kies",
    "ik ga",
    "ik wil",
    "we gaan",
    "laten we",
    "doe",
    "probeer",
    "start",
    "vraag",
    "zoek",
    "confronteer",
    "pak aan",
    "ik neem",
)


def default_story_state() -> StoryState:
    return {
        "beat_idx": 0,
        "phase": BEAT_SEQUENCE[0],
        "beats": list(BEAT_SEQUENCE),
        "global_summary": "",
        "last_scene_summary": "",
        "pinned_facts": [],
        "threads": [],
        "active_quests": [],
        "relationships": [],
        "player_traits": [],
        "player_inventory": [],
    }


def _clean_text(value: Any, *, max_len: int = 4000) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return text[:max_len]


def _split_sentences(text: str) -> List[str]:
    raw = _clean_text(text, max_len=4000)
    if not raw:
        return []
    chunks = re.split(r"(?<=[.!?])\s+", raw)
    out: List[str] = []
    for chunk in chunks:
        sentence = _clean_text(chunk, max_len=500)
        if sentence:
            out.append(sentence)
    return out


def _merge_global_summary(current: str, latest_scene_summary: str) -> str:
    current_sentences = _split_sentences(current)
    latest_sentences = _split_sentences(latest_scene_summary)
    if not latest_sentences:
        return _clean_text(current, max_len=1200)

    merged: List[str] = []
    seen = set()
    for sentence in current_sentences + latest_sentences:
        key = _norm_key(sentence)
        if not key or key in seen:
            continue
        seen.add(key)
        merged.append(sentence)

    # Houd het compact en stabiel voor context: laatste 6 zinnen.
    merged = merged[-6:]
    return _clean_text(" ".join(merged), max_len=1200)


def _norm_key(value: Any) -> str:
    return " ".join(str(value or "").strip().casefold().split())


def _dedupe_str_list(raw: Any, *, max_items: Optional[int] = None) -> List[str]:
    if not isinstance(raw, list):
        return []
    out: List[str] = []
    seen = set()
    for item in raw:
        text = _clean_text(item, max_len=300)
        if not text:
            continue
        key = _norm_key(text)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(text)
        if max_items is not None and len(out) >= max_items:
            break
    return out


def _normalize_thread_open_item(raw: Any) -> Optional[Dict[str, str]]:
    if isinstance(raw, str):
        thread_id = _clean_text(raw, max_len=120)
        if not thread_id:
            return None
        return {"id": thread_id, "notes": ""}
    if not isinstance(raw, dict):
        return None
    thread_id = _clean_text(raw.get("id"), max_len=120)
    notes = _clean_text(raw.get("notes"), max_len=500)
    if not thread_id:
        return None
    return {"id": thread_id, "notes": notes}


def _normalize_thread_resolve_item(raw: Any) -> str:
    if isinstance(raw, dict):
        return _clean_text(raw.get("id"), max_len=120)
    return _clean_text(raw, max_len=120)


def _normalize_thread_resolve_list(raw: Any) -> List[str]:
    if not isinstance(raw, list):
        return []
    out: List[str] = []
    seen = set()
    for item in raw:
        thread_id = _normalize_thread_resolve_item(item)
        key = _norm_key(thread_id)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(thread_id)
        if len(out) >= MAX_THREADS:
            break
    return out


def _normalize_threads(raw: Any) -> List[Dict[str, str]]:
    if not isinstance(raw, list):
        return []
    out: List[Dict[str, str]] = []
    seen = set()
    for item in raw:
        entry = _normalize_thread_open_item(item)
        if not entry:
            continue
        key = _norm_key(entry["id"])
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(entry)
        if len(out) >= MAX_THREADS:
            break
    return out


def _extract_json_object(raw_text: str) -> Dict[str, Any]:
    text = str(raw_text or "").strip()
    if not text:
        raise ValueError("Lege LLM output.")

    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, flags=re.IGNORECASE | re.DOTALL)
    if fence:
        text = fence.group(1).strip()

    try:
        data = json.loads(text)
    except Exception as exc:
        raise ValueError(f"JSON parse mislukt: {exc}") from exc

    if not isinstance(data, dict):
        raise ValueError("LLM output moet een JSON object zijn.")
    return data


def _tokenize_words(text: str) -> List[str]:
    return re.findall(r"[a-zA-Z\u00c0-\u024f']+", str(text or "").casefold())


def _is_dutch_text(text: str) -> bool:
    raw = _clean_text(text, max_len=2500)
    if not raw:
        return True

    words = _tokenize_words(raw)
    if len(words) < 4:
        return True

    nl_hits = sum(1 for w in words if w in _NL_STOPWORDS)
    en_hits = sum(1 for w in words if w in _EN_STOPWORDS)
    if en_hits >= 3 and nl_hits == 0:
        return False
    if en_hits >= 4 and en_hits > (nl_hits * 2):
        return False
    return True


def _validate_language_for_storyteller(payload: Dict[str, Any], *, required_language: str) -> None:
    if str(required_language or "").strip().lower() != "nl":
        return
    invalid_fields: List[str] = []
    scene = str(payload.get("scene") or "")
    if not _is_dutch_text(scene):
        invalid_fields.append("scene")
    for idx, choice in enumerate(payload.get("choices") or []):
        if not _is_dutch_text(str(choice or "")):
            invalid_fields.append(f"choices[{idx}]")
    if invalid_fields:
        raise ValueError(
            "Storyteller output moet Nederlands zijn. Niet-NL velden: "
            + ", ".join(invalid_fields[:8])
        )


def _validate_language_for_patch(
    patch: StoryPatch,
    *,
    required_language: str,
    require_global_summary: bool,
) -> None:
    if str(required_language or "").strip().lower() != "nl":
        return

    invalid_fields: List[str] = []

    def _check_text(path: str, value: Any, *, required: bool = False) -> None:
        text = _clean_text(value, max_len=2000)
        if not text:
            if required:
                invalid_fields.append(path)
            return
        if not _is_dutch_text(text):
            invalid_fields.append(path)

    _check_text("global_summary", patch.get("global_summary"), required=require_global_summary)
    _check_text("last_scene_summary", patch.get("last_scene_summary"))
    _check_text("recommended_plot.reason", (patch.get("recommended_plot") or {}).get("reason"))

    for key in ("facts", "active_quests", "relationships", "player_traits", "player_inventory"):
        block = patch.get(key) or {}
        for item in (block.get("add") or []):
            _check_text(f"{key}.add[]", item)
        for item in (block.get("remove") or []):
            _check_text(f"{key}.remove[]", item)

    threads = patch.get("threads") or {}
    for item in (threads.get("open") or []):
        if isinstance(item, dict):
            _check_text("threads.open[].id", item.get("id"))
            _check_text("threads.open[].notes", item.get("notes"))
        else:
            _check_text("threads.open[]", item)
    for item in (threads.get("resolve") or []):
        _check_text("threads.resolve[]", item)

    if invalid_fields:
        raise ValueError(
            "StateUpdater output moet Nederlands zijn. Niet-NL/ongeldige velden: "
            + ", ".join(invalid_fields[:12])
        )


def validate_story_state(raw_state: Dict[str, Any]) -> StoryState:
    state = default_story_state()
    if not isinstance(raw_state, dict):
        raise ValueError("Story state moet een object zijn.")

    beats_raw = raw_state.get("beats")
    if isinstance(beats_raw, list) and beats_raw and all(isinstance(v, str) and v.strip() for v in beats_raw):
        beats = [str(v).strip() for v in beats_raw]
    else:
        beats = list(BEAT_SEQUENCE)

    try:
        beat_idx = int(raw_state.get("beat_idx", 0))
    except Exception as exc:
        raise ValueError(f"Ongeldige beat_idx: {exc}") from exc
    if beat_idx < 0 or beat_idx >= len(beats):
        raise ValueError("beat_idx buiten bereik van beats.")

    phase = _clean_text(raw_state.get("phase"), max_len=80)
    expected_phase = beats[beat_idx]
    if phase and phase != expected_phase:
        raise ValueError("phase moet gelijk zijn aan beats[beat_idx].")

    state["beats"] = beats
    state["beat_idx"] = beat_idx
    state["phase"] = expected_phase
    state["global_summary"] = _clean_text(raw_state.get("global_summary"), max_len=1200)
    state["last_scene_summary"] = _clean_text(raw_state.get("last_scene_summary"), max_len=600)
    state["pinned_facts"] = _dedupe_str_list(raw_state.get("pinned_facts"), max_items=MAX_PINNED_FACTS)
    state["threads"] = _normalize_threads(raw_state.get("threads"))
    state["active_quests"] = _dedupe_str_list(raw_state.get("active_quests"), max_items=MAX_ACTIVE_QUESTS)
    state["relationships"] = _dedupe_str_list(raw_state.get("relationships"), max_items=MAX_RELATIONSHIPS)
    state["player_traits"] = _dedupe_str_list(raw_state.get("player_traits"), max_items=MAX_PLAYER_TRAITS)
    state["player_inventory"] = _dedupe_str_list(raw_state.get("player_inventory"), max_items=MAX_PLAYER_INVENTORY)
    return state


def parse_storyteller_output(
    raw_text: str,
    *,
    strict_json: bool = True,
    required_language: str = "nl",
) -> Dict[str, Any]:
    data = _extract_json_object(raw_text)
    allowed = {"scene", "choices"}
    if strict_json:
        unknown = set(data.keys()) - allowed
        if unknown:
            raise ValueError(f"Onbekende storyteller keys: {sorted(unknown)}")

    scene = _clean_text(data.get("scene"), max_len=3000)
    if not scene:
        raise ValueError("Storyteller.scene ontbreekt of is leeg.")
    word_count = len(re.findall(r"\S+", scene))
    if word_count > 120:
        raise ValueError("Storyteller.scene mag maximaal 120 woorden bevatten.")

    choices_raw = data.get("choices")
    if not isinstance(choices_raw, list):
        raise ValueError("Storyteller.choices moet een lijst zijn.")
    choices = [_clean_text(v, max_len=240) for v in choices_raw if _clean_text(v, max_len=240)]
    if len(choices) != 2:
        raise ValueError("Storyteller moet precies 2 keuzes geven.")
    parsed = {"scene": scene, "choices": choices}
    _validate_language_for_storyteller(parsed, required_language=required_language)
    return parsed


def _parse_add_remove_block(raw: Any, *, name: str, strict_json: bool) -> Dict[str, List[str]]:
    if raw is None:
        return {"add": [], "remove": []}
    if not isinstance(raw, dict):
        raise ValueError(f"{name} moet een object zijn.")
    allowed = {"add", "remove"}
    if strict_json:
        unknown = set(raw.keys()) - allowed
        if unknown:
            raise ValueError(f"Onbekende keys in {name}: {sorted(unknown)}")
    return {
        "add": _dedupe_str_list(raw.get("add")),
        "remove": _dedupe_str_list(raw.get("remove")),
    }


def _parse_threads_block(raw: Any, *, strict_json: bool) -> Dict[str, Any]:
    if raw is None:
        return {"open": [], "resolve": []}
    if not isinstance(raw, dict):
        raise ValueError("threads moet een object zijn.")
    allowed = {"open", "resolve"}
    if strict_json:
        unknown = set(raw.keys()) - allowed
        if unknown:
            raise ValueError(f"Onbekende keys in threads: {sorted(unknown)}")
    open_entries = []
    open_raw = raw.get("open")
    if isinstance(open_raw, list):
        for item in open_raw:
            entry = _normalize_thread_open_item(item)
            if entry:
                open_entries.append(entry)
                if len(open_entries) >= MAX_THREADS:
                    break
    resolve_entries = _normalize_thread_resolve_list(raw.get("resolve"))
    return {"open": open_entries, "resolve": resolve_entries}


def parse_state_updater_patch(
    raw_text: str,
    *,
    strict_json: bool = True,
    required_language: str = "nl",
    require_global_summary: bool = False,
) -> StoryPatch:
    data = _extract_json_object(raw_text)
    allowed = {
        "global_summary",
        "last_scene_summary",
        "facts",
        "threads",
        "active_quests",
        "relationships",
        "player_traits",
        "player_inventory",
        "recommended_plot",
    }
    if strict_json:
        unknown = set(data.keys()) - allowed
        if unknown:
            raise ValueError(f"Onbekende state patch keys: {sorted(unknown)}")

    patch: StoryPatch = {
        "global_summary": _clean_text(data.get("global_summary"), max_len=1200),
        "last_scene_summary": _clean_text(data.get("last_scene_summary"), max_len=600),
        "facts": _parse_add_remove_block(data.get("facts"), name="facts", strict_json=strict_json),
        "threads": _parse_threads_block(data.get("threads"), strict_json=strict_json),
        "active_quests": _parse_add_remove_block(
            data.get("active_quests"),
            name="active_quests",
            strict_json=strict_json,
        ),
        "relationships": _parse_add_remove_block(
            data.get("relationships"),
            name="relationships",
            strict_json=strict_json,
        ),
        "player_traits": _parse_add_remove_block(
            data.get("player_traits"),
            name="player_traits",
            strict_json=strict_json,
        ),
        "player_inventory": _parse_add_remove_block(
            data.get("player_inventory"),
            name="player_inventory",
            strict_json=strict_json,
        ),
    }

    recommended_raw = data.get("recommended_plot") or {}
    if recommended_raw is None:
        recommended_raw = {}
    if not isinstance(recommended_raw, dict):
        raise ValueError("recommended_plot moet een object zijn.")
    if strict_json:
        unknown = set(recommended_raw.keys()) - {"advance", "reason"}
        if unknown:
            raise ValueError(f"Onbekende keys in recommended_plot: {sorted(unknown)}")
    advance = _clean_text(recommended_raw.get("advance"), max_len=20).lower() or "stay"
    if advance not in ("stay", "next"):
        raise ValueError("recommended_plot.advance moet 'stay' of 'next' zijn.")
    patch["recommended_plot"] = {
        "advance": advance,
        "reason": _clean_text(recommended_raw.get("reason"), max_len=500),
    }
    if require_global_summary and not patch["global_summary"]:
        raise ValueError("StateUpdater.global_summary is verplicht en mag niet leeg zijn.")
    _validate_language_for_patch(
        patch,
        required_language=required_language,
        require_global_summary=require_global_summary,
    )
    return patch


def _apply_add_remove(current: List[str], add_remove: Dict[str, List[str]], *, max_items: Optional[int] = None) -> List[str]:
    out = list(current)
    remove_keys = {_norm_key(v) for v in add_remove.get("remove", []) if _norm_key(v)}
    if remove_keys:
        out = [item for item in out if _norm_key(item) not in remove_keys]

    existing_keys = {_norm_key(v) for v in out if _norm_key(v)}
    for item in add_remove.get("add", []):
        key = _norm_key(item)
        if not key or key in existing_keys:
            continue
        existing_keys.add(key)
        out.append(item)

    if max_items is not None and len(out) > max_items:
        out = out[:max_items]
    return out


def _contains_keywords(texts: List[str], keywords: set[str]) -> bool:
    for text in texts:
        normalized = _norm_key(text)
        if not normalized:
            continue
        for keyword in keywords:
            if keyword in normalized:
                return True
    return False


def _has_user_commit(user_input: str) -> bool:
    cleaned = _norm_key(user_input)
    if not cleaned:
        return False
    if cleaned in {"1", "2", "1.", "2.", "1)", "2)"}:
        return True
    return any(pattern in cleaned for pattern in _USER_COMMIT_PATTERNS)


def _observed_signals(patch: StoryPatch, *, user_input: str) -> Dict[str, bool]:
    facts = patch.get("facts", {}) or {}
    active_quests = patch.get("active_quests", {}) or {}
    threads = patch.get("threads", {}) or {}
    last_scene_summary = str(patch.get("last_scene_summary") or "")
    facts_add = [str(v or "") for v in (facts.get("add") or [])]
    signal_texts = [last_scene_summary] + facts_add

    return {
        "S_FACT_ADD": bool(facts_add),
        "S_QUEST_ADD": bool(active_quests.get("add") or []),
        "S_QUEST_REMOVE": bool(active_quests.get("remove") or []),
        "S_THREAD_OPEN": bool(threads.get("open") or []),
        "S_THREAD_RESOLVE": bool(threads.get("resolve") or []),
        "S_USER_COMMIT": _has_user_commit(user_input),
        "S_OBSTACLE": _contains_keywords(signal_texts, _OBSTACLE_KEYWORDS_NL),
        "S_REVERSAL": _contains_keywords(signal_texts, _REVERSAL_KEYWORDS_NL),
        "S_SETBACK": _contains_keywords(signal_texts, _SETBACK_KEYWORDS_NL),
    }


def _apply_conservative_beat_gate(
    out: StoryState,
    *,
    patch: StoryPatch,
    requested: str,
    user_input: str,
) -> Tuple[str, Dict[str, Any]]:
    beats = list(out.get("beats") or BEAT_SEQUENCE)
    beat_idx = int(out.get("beat_idx", 0))
    current = str(out.get("phase") or beats[beat_idx])
    target = current
    if beat_idx < (len(beats) - 1):
        target = beats[beat_idx + 1]
    transition = f"{current}->{target}"
    signals = _observed_signals(patch, user_input=user_input)

    if requested != "next":
        return "stay", {
            "transition": transition,
            "requested": requested,
            "applied": "stay",
            "passed": True,
            "failed_checks": [],
            "observed_signals": signals,
        }

    if beat_idx >= (len(beats) - 1):
        return "stay", {
            "transition": transition,
            "requested": requested,
            "applied": "stay",
            "passed": False,
            "failed_checks": ["already_last_beat"],
            "observed_signals": signals,
        }

    checks: List[Tuple[str, bool]] = []
    if current == "setup":
        checks.append(("S_FACT_ADD or S_QUEST_ADD or S_THREAD_OPEN", signals["S_FACT_ADD"] or signals["S_QUEST_ADD"] or signals["S_THREAD_OPEN"]))
    elif current == "trigger":
        checks.append(("(S_QUEST_ADD or S_THREAD_OPEN)", signals["S_QUEST_ADD"] or signals["S_THREAD_OPEN"]))
        checks.append(("S_USER_COMMIT", signals["S_USER_COMMIT"]))
    elif current == "plan":
        checks.append(("S_FACT_ADD", signals["S_FACT_ADD"]))
        checks.append(("S_OBSTACLE", signals["S_OBSTACLE"]))
    elif current == "complication":
        checks.append(("S_FACT_ADD", signals["S_FACT_ADD"]))
        checks.append(("S_REVERSAL", signals["S_REVERSAL"]))
    elif current == "reversal":
        checks.append(("(S_FACT_ADD or S_QUEST_REMOVE or S_THREAD_RESOLVE)", signals["S_FACT_ADD"] or signals["S_QUEST_REMOVE"] or signals["S_THREAD_RESOLVE"]))
        checks.append(("S_SETBACK", signals["S_SETBACK"]))
    elif current == "dark_moment":
        checks.append(("(S_FACT_ADD or S_QUEST_ADD or S_THREAD_OPEN)", signals["S_FACT_ADD"] or signals["S_QUEST_ADD"] or signals["S_THREAD_OPEN"]))
        checks.append(("S_USER_COMMIT", signals["S_USER_COMMIT"]))
    elif current == "climax":
        checks.append(("S_THREAD_RESOLVE", signals["S_THREAD_RESOLVE"]))

    passed = all(passed_check for _name, passed_check in checks)
    failed_checks = [name for name, passed_check in checks if not passed_check]
    applied = "stay"
    if passed:
        out["beat_idx"] = beat_idx + 1
        out["phase"] = beats[beat_idx + 1]
        applied = "next"

    return applied, {
        "transition": transition,
        "requested": requested,
        "applied": applied,
        "passed": passed,
        "failed_checks": failed_checks,
        "observed_signals": signals,
    }


def apply_story_patch(
    state: StoryState,
    patch: StoryPatch,
    *,
    user_input: str = "",
    beat_gate_policy: str = DEFAULT_BEAT_GATE_POLICY,
    summary_policy: str = "updater_only",
) -> Tuple[StoryState, Dict[str, Any]]:
    out = validate_story_state(copy.deepcopy(state))
    if patch.get("last_scene_summary"):
        out["last_scene_summary"] = _clean_text(patch.get("last_scene_summary"), max_len=600)

    if summary_policy == "hybrid":
        if patch.get("global_summary"):
            out["global_summary"] = _clean_text(patch.get("global_summary"), max_len=1200)
        elif patch.get("last_scene_summary"):
            out["global_summary"] = _merge_global_summary(out.get("global_summary", ""), out["last_scene_summary"])
    elif patch.get("global_summary"):
        out["global_summary"] = _clean_text(patch.get("global_summary"), max_len=1200)

    out["pinned_facts"] = _apply_add_remove(
        out.get("pinned_facts", []),
        patch.get("facts", {"add": [], "remove": []}),
        max_items=MAX_PINNED_FACTS,
    )
    out["active_quests"] = _apply_add_remove(
        out.get("active_quests", []),
        patch.get("active_quests", {"add": [], "remove": []}),
        max_items=MAX_ACTIVE_QUESTS,
    )
    out["relationships"] = _apply_add_remove(
        out.get("relationships", []),
        patch.get("relationships", {"add": [], "remove": []}),
        max_items=MAX_RELATIONSHIPS,
    )
    out["player_traits"] = _apply_add_remove(
        out.get("player_traits", []),
        patch.get("player_traits", {"add": [], "remove": []}),
        max_items=MAX_PLAYER_TRAITS,
    )
    out["player_inventory"] = _apply_add_remove(
        out.get("player_inventory", []),
        patch.get("player_inventory", {"add": [], "remove": []}),
        max_items=MAX_PLAYER_INVENTORY,
    )

    threads = out.get("threads", [])
    thread_map = {_norm_key(t.get("id")): dict(t) for t in threads if isinstance(t, dict) and _norm_key(t.get("id"))}
    threads_patch = patch.get("threads", {"open": [], "resolve": []})
    for item in threads_patch.get("open", []):
        entry = _normalize_thread_open_item(item)
        if not entry:
            continue
        thread_map[_norm_key(entry["id"])] = entry
    for item in threads_patch.get("resolve", []):
        key = _norm_key(_normalize_thread_resolve_item(item))
        if key in thread_map:
            thread_map.pop(key, None)
    out["threads"] = list(thread_map.values())[:MAX_THREADS]

    recommended_plot = patch.get("recommended_plot", {}) or {}
    requested = str(recommended_plot.get("advance") or "stay").strip().lower()
    if requested not in ("stay", "next"):
        requested = "stay"

    if beat_gate_policy == DEFAULT_BEAT_GATE_POLICY:
        applied, beat_gate = _apply_conservative_beat_gate(
            out,
            patch=patch,
            requested=requested,
            user_input=user_input,
        )
    else:
        applied = "stay"
        beats = list(out.get("beats") or BEAT_SEQUENCE)
        beat_idx = int(out.get("beat_idx", 0))
        if requested == "next" and beat_idx < (len(beats) - 1):
            out["beat_idx"] = beat_idx + 1
            out["phase"] = beats[beat_idx + 1]
            applied = "next"
        beat_gate = {
            "transition": f"{out.get('phase')}->{out.get('phase')}",
            "requested": requested,
            "applied": applied,
            "passed": applied == requested,
            "failed_checks": [],
            "observed_signals": {},
        }

    out = validate_story_state(out)
    return out, {
        "requested_advance": requested,
        "applied_advance": applied,
        "beat_gate": beat_gate,
    }


def format_story_reply(scene: str, choices: List[str]) -> str:
    choice_lines = "\n".join([f"{idx}. {text}" for idx, text in enumerate(choices, start=1)])
    return f"{scene}\n\nKeuzes:\n{choice_lines}"
