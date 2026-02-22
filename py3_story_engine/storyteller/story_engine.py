from __future__ import annotations

import copy
import json
import os
from typing import Any, Callable, Dict, List, Optional, Tuple

from storyteller.story_models import (
    DEFAULT_BEAT_GATE_POLICY,
    StoryState,
    apply_story_patch,
    format_story_reply,
    parse_state_updater_patch,
    parse_storyteller_output,
    validate_story_state,
)

try:
    from storyteller.backends.llm_ollama import OllamaClient, OllamaLLMBackend
except Exception:  # pragma: no cover - optional dependency
    OllamaClient = None  # type: ignore[assignment]
    OllamaLLMBackend = None  # type: ignore[assignment]

HistoryLike = List[Dict[str, Any]]

_DEFAULT_LOCAL_MODEL = "llama3.1:8b"
_DEFAULT_LOCAL_HOST = "http://localhost:11434"
_DEFAULT_CLOUD_MODEL = "gpt-oss:120b"
_DEFAULT_CLOUD_HOST = "https://ollama.com"


_BEAT_RULES = {
    "setup": {
        "allowed": "Introduceer wereld, toon en context.",
        "forbidden": "Geen grote ontknoping of climax.",
    },
    "trigger": {
        "allowed": "Toon aanleiding of verstoring.",
        "forbidden": "Nog geen volledige oplossing.",
    },
    "plan": {
        "allowed": "Maak intentie en plan concreet.",
        "forbidden": "Geen finale confrontatie.",
    },
    "complication": {
        "allowed": "Verhoog obstakels en frictie.",
        "forbidden": "Geen afronding van kernconflict.",
    },
    "reversal": {
        "allowed": "Voeg betekenisvolle wending toe.",
        "forbidden": "Niet direct afronden.",
    },
    "dark_moment": {
        "allowed": "Toon laagste punt of impasse.",
        "forbidden": "Geen echte overwinning.",
    },
    "climax": {
        "allowed": "Beslissende confrontatie of actie.",
        "forbidden": "Geen nieuwe subplot openen.",
    },
    "aftermath": {
        "allowed": "Toon gevolgen en afronding.",
        "forbidden": "Geen nieuwe hoofdconflicten starten.",
    },
}

_STORYTELLER_CORE_KEYS = (
    "beat_idx",
    "phase",
    "beats",
    "global_summary",
    "last_scene_summary",
    "pinned_facts",
    "threads",
    "active_quests",
)

_STORYTELLER_OPTIONAL_KEYS = (
    "relationships",
    "player_traits",
    "player_inventory",
)

_STORYTELLER_OPTIONAL_MAX_ITEMS = 3


class StoryTurnCancelledError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        role_label: str = "",
        stage: str = "",
        attempts: int = 0,
        raw_output: str = "",
        attempt_outputs: Optional[List[str]] = None,
        attempt_inputs: Optional[List[Dict[str, Any]]] = None,
        retry_contexts: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        super().__init__(message)
        self.role_label = str(role_label or "")
        self.stage = str(stage or "")
        self.attempts = int(attempts or 0)
        self.raw_output = str(raw_output or "")
        self.attempt_outputs = list(attempt_outputs or [])
        self.attempt_inputs = copy.deepcopy(attempt_inputs or [])
        self.retry_contexts = copy.deepcopy(retry_contexts or [])


class StoryLLMOutputRejectedError(ValueError):
    def __init__(
        self,
        message: str,
        *,
        role_label: str = "",
        attempts: int = 0,
        errors: Optional[List[str]] = None,
        raw_output: str = "",
        attempt_outputs: Optional[List[str]] = None,
        attempt_inputs: Optional[List[Dict[str, Any]]] = None,
        retry_contexts: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        super().__init__(message)
        self.role_label = str(role_label or "")
        self.attempts = int(attempts or 0)
        self.errors = list(errors or [])
        self.raw_output = str(raw_output or "")
        self.attempt_outputs = list(attempt_outputs or [])
        self.attempt_inputs = copy.deepcopy(attempt_inputs or [])
        self.retry_contexts = copy.deepcopy(retry_contexts or [])


def build_story_llm_backend(*, llm_type: str, llm_model: str, base_params: Optional[Dict[str, Any]] = None):
    llm_type_norm = str(llm_type or "").strip().lower() or "ollama_local"
    params = copy.deepcopy(base_params or {})
    if llm_model:
        params["model"] = llm_model
    options = {
        key: params.get(key)
        for key in ("temperature", "top_p", "top_k")
        if params.get(key) is not None
    }

    if llm_type_norm == "ollama_local":
        if OllamaClient is None or OllamaLLMBackend is None:
            raise RuntimeError("Ollama dependency ontbreekt. Installeer met: pip install ollama")
        host = params.get("host", _DEFAULT_LOCAL_HOST)
        model = params.get("model", _DEFAULT_LOCAL_MODEL)
        api_key = params.get("api_key", None)
        client = OllamaClient(model=model, host=host, api_key=api_key, options=options)
        return OllamaLLMBackend(client)
    if llm_type_norm == "ollama_cloud":
        if OllamaClient is None or OllamaLLMBackend is None:
            raise RuntimeError("Ollama dependency ontbreekt. Installeer met: pip install ollama")
        api_key = params.get("api_key") or os.environ.get("OLLAMA_API_KEY")
        if not api_key:
            raise RuntimeError("OLLAMA_API_KEY ontbreekt voor story LLM.")
        host = params.get("host", os.environ.get("OLLAMA_HOST", _DEFAULT_CLOUD_HOST))
        model = params.get("model", os.environ.get("OLLAMA_MODEL", _DEFAULT_CLOUD_MODEL))
        client = OllamaClient(model=model, host=host, api_key=api_key, options=options)
        return OllamaLLMBackend(client)
    raise ValueError(
        f"Ongeldig story llm type: {llm_type!r}. Geldig: 'ollama_local' of 'ollama_cloud'."
    )


def _extract_recent_history(history: Optional[HistoryLike], max_history_turns: int) -> List[Dict[str, str]]:
    if not isinstance(history, list) or max_history_turns <= 0:
        return []
    filtered: List[Dict[str, str]] = []
    for item in history:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "").strip().lower()
        if role not in ("user", "assistant"):
            continue
        content = str(item.get("content") or "").strip()
        if not content:
            continue
        filtered.append({"role": role, "content": content})
    return filtered[-(max_history_turns * 2) :]


def _history_block(history: List[Dict[str, str]]) -> str:
    if not history:
        return "(geen recente geschiedenis)"
    lines = []
    for item in history:
        role = "USER" if item["role"] == "user" else "ASSISTANT"
        lines.append(f"- {role}: {item['content']}")
    return "\n".join(lines)


def _beat_constraints(phase: str) -> Dict[str, str]:
    return _BEAT_RULES.get(phase, {"allowed": "Blijf coherent met huidige fase.", "forbidden": "Sla geen beats over."})


def _norm_text(value: Any) -> str:
    return " ".join(str(value or "").strip().casefold().split())


def _debug_clip_text(value: Any, *, max_len: int = 16000) -> str:
    text = str(value or "")
    if len(text) <= max_len:
        return text
    omitted = len(text) - max_len
    return text[:max_len] + f"\n...[truncated {omitted} chars]"


def _debug_snapshot_messages(messages: List[Dict[str, str]]) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = str(msg.get("role") or "").strip().lower()
        if role not in ("system", "user", "assistant"):
            continue
        out.append({"role": role, "content": _debug_clip_text(msg.get("content", ""))})
    return out


def _debug_attempt_input(messages: List[Dict[str, str]], *, attempt: int) -> Dict[str, Any]:
    user_prompt = ""
    if messages:
        last = messages[-1]
        if isinstance(last, dict) and str(last.get("role") or "").strip().lower() == "user":
            user_prompt = _debug_clip_text(last.get("content", ""))
    return {
        "attempt": int(attempt),
        "user_prompt": user_prompt,
        "messages": _debug_snapshot_messages(messages),
    }


def _storyteller_state_projection(
    *,
    state: StoryState,
    patch: Optional[Dict[str, Any]],
    user_input: str,
) -> Dict[str, Any]:
    validated = validate_story_state(state)
    projected: Dict[str, Any] = {}
    for key in _STORYTELLER_CORE_KEYS:
        projected[key] = copy.deepcopy(validated.get(key))

    context_parts: List[str] = [
        str(user_input or ""),
        str(validated.get("last_scene_summary") or ""),
        str(validated.get("global_summary") or ""),
    ]
    for quest in validated.get("active_quests") or []:
        context_parts.append(str(quest or ""))
    for thread in validated.get("threads") or []:
        if not isinstance(thread, dict):
            continue
        context_parts.append(str(thread.get("id") or ""))
        context_parts.append(str(thread.get("notes") or ""))
    context_norm = _norm_text(" ".join(context_parts))

    def _patch_touched(field: str) -> bool:
        if not isinstance(patch, dict):
            return False
        block = patch.get(field)
        if not isinstance(block, dict):
            return False
        return bool((block.get("add") or []) or (block.get("remove") or []))

    for field in _STORYTELLER_OPTIONAL_KEYS:
        values = [str(v or "").strip() for v in (validated.get(field) or []) if str(v or "").strip()]
        if not values and not _patch_touched(field):
            continue

        relevant: List[str] = []
        for item in values:
            norm = _norm_text(item)
            if not norm:
                continue
            if len(norm) <= 2:
                relevant.append(item)
                continue
            if norm in context_norm:
                relevant.append(item)
        if _patch_touched(field):
            selected = values[:_STORYTELLER_OPTIONAL_MAX_ITEMS]
        elif relevant:
            selected = relevant[:_STORYTELLER_OPTIONAL_MAX_ITEMS]
        elif len(values) <= 2:
            selected = values[:]
        else:
            selected = []
        if selected:
            projected[field] = selected

    return projected


def build_storyteller_user_prompt(
    *,
    state: StoryState,
    user_input: str,
    history: Optional[HistoryLike],
    max_history_turns: int,
) -> str:
    phase = str(state.get("phase") or "setup")
    constraints = _beat_constraints(phase)
    recent = _extract_recent_history(history, max_history_turns)
    state_json = json.dumps(state, ensure_ascii=False, indent=2)
    return (
        "[MODE] storyteller\n"
        f"[BEAT] current={phase} idx={state.get('beat_idx', 0)}\n"
        f"[ALLOWED] {constraints['allowed']}\n"
        f"[FORBIDDEN] {constraints['forbidden']}\n"
        "[STATE_JSON]\n"
        f"{state_json}\n"
        "[RECENT_HISTORY]\n"
        f"{_history_block(recent)}\n"
        "[USER_INPUT]\n"
        f"{str(user_input or '').strip()}\n"
        "Geef ALLEEN strict JSON:\n"
        '{"scene":"...","choices":["...","..."]}'
    )


def build_state_updater_user_prompt(
    *,
    state: StoryState,
    scene_text: str,
    user_input: str,
    history: Optional[HistoryLike],
    max_history_turns: int,
) -> str:
    recent = _extract_recent_history(history, max_history_turns)
    state_json = json.dumps(state, ensure_ascii=False, indent=2)
    return (
        "[MODE] state_updater\n"
        "[STATE_JSON]\n"
        f"{state_json}\n"
        "[LAST_SCENE_TEXT]\n"
        f"{str(scene_text or '').strip()}\n"
        "[RECENT_HISTORY]\n"
        f"{_history_block(recent)}\n"
        "[USER_INPUT]\n"
        f"{str(user_input or '').strip()}\n"
        "Geef ALLEEN strict JSON patch volgens schema."
    )


def _generate_with_retry(
    *,
    llm_backend: Any,
    base_messages: List[Dict[str, str]],
    parser: Callable[[str], Dict[str, Any]],
    role_label: str,
    invalid_retry_count: int,
    should_abort: Optional[Callable[[], bool]] = None,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    attempts = 0
    errors: List[str] = []
    attempt_outputs: List[str] = []
    attempt_inputs: List[Dict[str, Any]] = []
    retry_contexts: List[Dict[str, Any]] = []
    messages = list(base_messages)

    while True:
        if callable(should_abort):
            try:
                if bool(should_abort()):
                    raise StoryTurnCancelledError(
                        f"{role_label} cancelled before LLM call.",
                        role_label=role_label,
                        stage="before_llm_call",
                        attempts=attempts,
                        attempt_outputs=attempt_outputs,
                        attempt_inputs=attempt_inputs,
                        retry_contexts=retry_contexts,
                    )
            except StoryTurnCancelledError:
                raise
            except Exception:
                pass
        attempts += 1
        attempt_inputs.append(_debug_attempt_input(messages, attempt=attempts))
        result = llm_backend.generate(messages)
        raw_output = str(result.reply or "")
        attempt_outputs.append(raw_output)
        if callable(should_abort):
            try:
                if bool(should_abort()):
                    raise StoryTurnCancelledError(
                        f"{role_label} cancelled after LLM call.",
                        role_label=role_label,
                        stage="after_llm_call",
                        attempts=attempts,
                        raw_output=raw_output,
                        attempt_outputs=attempt_outputs,
                        attempt_inputs=attempt_inputs,
                        retry_contexts=retry_contexts,
                    )
            except StoryTurnCancelledError:
                raise
            except Exception:
                pass
        try:
            parsed = parser(raw_output)
            return parsed, {
                "attempts": attempts,
                "errors": list(errors),
                "passed": True,
                "attempt_outputs": attempt_outputs,
                "raw_output": raw_output,
                "attempt_inputs": attempt_inputs,
                "retry_contexts": retry_contexts,
            }
        except Exception as exc:
            err = str(exc)
            errors.append(err)
            if attempts >= (invalid_retry_count + 1):
                raise StoryLLMOutputRejectedError(
                    f"{role_label} output ongeldig na {attempts} poging(en): {err}",
                    role_label=role_label,
                    attempts=attempts,
                    errors=errors,
                    raw_output=raw_output,
                    attempt_outputs=attempt_outputs,
                    attempt_inputs=attempt_inputs,
                    retry_contexts=retry_contexts,
                ) from exc
            repair_prompt = (
                "Vorige output was ongeldig.\n"
                f"Fout: {err}\n"
                "Corrigeer volledig. Geef ALLEEN strict JSON in exact het gevraagde schema."
            )
            messages = list(base_messages) + [
                {"role": "assistant", "content": raw_output},
                {"role": "user", "content": repair_prompt},
            ]
            retry_contexts.append(
                {
                    "failed_attempt": attempts,
                    "error": err,
                    "failed_output": _debug_clip_text(raw_output),
                    "repair_prompt": repair_prompt,
                    "next_attempt_input": _debug_attempt_input(messages, attempt=attempts + 1),
                }
            )


def run_story_turn(
    *,
    storyteller_llm,
    state_updater_llm,
    storyteller_system_prompt: str,
    state_updater_system_prompt: str,
    state: StoryState,
    last_scene_text: str,
    user_input: str,
    history: Optional[HistoryLike],
    strict_json: bool,
    max_history_turns: int,
    required_language: str = "nl",
    invalid_retry_count: int = 1,
    require_global_summary: bool = True,
    beat_gate_policy: str = DEFAULT_BEAT_GATE_POLICY,
    should_abort: Optional[Callable[[], bool]] = None,
) -> Dict[str, Any]:
    if callable(should_abort):
        try:
            if bool(should_abort()):
                raise StoryTurnCancelledError("Story turn cancelled before processing.")
        except StoryTurnCancelledError:
            raise
        except Exception:
            pass
    current_state = validate_story_state(state)
    logs: Dict[str, Any] = {
        "state_in": copy.deepcopy(current_state),
        "state_updater": None,
        "storyteller": None,
        "validation": {
            "storyteller": {"attempts": 0, "errors": [], "passed": False},
            "state_updater": {"attempts": 0, "errors": [], "passed": True},
        },
        "beat_gate": None,
    }

    patch = None
    beat_decision: Dict[str, Any] = {"requested_advance": "stay", "applied_advance": "stay"}
    if str(last_scene_text or "").strip():
        updater_prompt = build_state_updater_user_prompt(
            state=current_state,
            scene_text=last_scene_text,
            user_input=user_input,
            history=history,
            max_history_turns=max_history_turns,
        )
        updater_messages = [
            {"role": "system", "content": state_updater_system_prompt},
            {"role": "user", "content": updater_prompt},
        ]
        patch, updater_meta = _generate_with_retry(
            llm_backend=state_updater_llm,
            base_messages=updater_messages,
            parser=lambda raw: parse_state_updater_patch(
                raw,
                strict_json=strict_json,
                required_language=required_language,
                require_global_summary=require_global_summary,
            ),
            role_label="StateUpdater",
            invalid_retry_count=invalid_retry_count,
            should_abort=should_abort,
        )
        current_state, beat_decision = apply_story_patch(
            current_state,
            patch,
            user_input=user_input,
            beat_gate_policy=beat_gate_policy,
            summary_policy="updater_only",
        )
        logs["validation"]["state_updater"] = {
            "attempts": int(updater_meta.get("attempts") or 0),
            "errors": list(updater_meta.get("errors") or []),
            "passed": bool(updater_meta.get("passed")),
        }
        logs["beat_gate"] = beat_decision.get("beat_gate")
        logs["state_updater"] = {
            "system_prompt": state_updater_system_prompt,
            "user_prompt": updater_prompt,
            "raw_output": updater_meta.get("raw_output"),
            "attempt_outputs": list(updater_meta.get("attempt_outputs") or []),
            "attempt_inputs": list(updater_meta.get("attempt_inputs") or []),
            "retry_contexts": list(updater_meta.get("retry_contexts") or []),
            "parsed_patch": patch,
            "beat_decision": beat_decision,
        }

    storyteller_state = _storyteller_state_projection(
        state=current_state,
        patch=patch,
        user_input=user_input,
    )
    storyteller_prompt = build_storyteller_user_prompt(
        state=storyteller_state,
        user_input=user_input,
        history=history,
        max_history_turns=max_history_turns,
    )
    storyteller_messages = [
        {"role": "system", "content": storyteller_system_prompt},
        {"role": "user", "content": storyteller_prompt},
    ]
    scene_payload, storyteller_meta = _generate_with_retry(
        llm_backend=storyteller_llm,
        base_messages=storyteller_messages,
        parser=lambda raw: parse_storyteller_output(
            raw,
            strict_json=strict_json,
            required_language=required_language,
        ),
        role_label="Storyteller",
        invalid_retry_count=invalid_retry_count,
        should_abort=should_abort,
    )
    scene = str(scene_payload["scene"])
    choices = list(scene_payload["choices"])
    reply_text = format_story_reply(scene, choices)
    logs["validation"]["storyteller"] = {
        "attempts": int(storyteller_meta.get("attempts") or 0),
        "errors": list(storyteller_meta.get("errors") or []),
        "passed": bool(storyteller_meta.get("passed")),
    }

    logs["storyteller"] = {
        "system_prompt": storyteller_system_prompt,
        "user_prompt": storyteller_prompt,
        "state_projection": storyteller_state,
        "raw_output": storyteller_meta.get("raw_output"),
        "attempt_outputs": list(storyteller_meta.get("attempt_outputs") or []),
        "attempt_inputs": list(storyteller_meta.get("attempt_inputs") or []),
        "retry_contexts": list(storyteller_meta.get("retry_contexts") or []),
        "parsed_output": scene_payload,
    }
    logs["state_out"] = copy.deepcopy(current_state)

    return {
        "state": current_state,
        "scene": scene,
        "choices": choices,
        "reply": reply_text,
        "patch": patch,
        "beat_decision": beat_decision,
        "logs": logs,
    }
