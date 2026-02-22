from __future__ import annotations

import argparse
import copy
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from storyteller.story_engine import (
    StoryLLMOutputRejectedError,
    StoryTurnCancelledError,
    build_story_llm_backend,
    run_story_turn,
)
from storyteller.story_models import DEFAULT_BEAT_GATE_POLICY, StoryState, default_story_state, validate_story_state
from storyteller.story_storage import (
    build_story_runtime_dir,
    list_named_saves,
    load_autosave,
    load_named,
    save_autosave,
    save_named,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROMPTS_DIR = PROJECT_ROOT / "prompts"
DEFAULT_STORYTELLER_PROMPT = PROMPTS_DIR / "storyteller_v1.txt"
DEFAULT_UPDATER_PROMPT = PROMPTS_DIR / "state_updater_v1.txt"
INSTANCE_ID = "storyteller_cli_v1"

_DEFAULT_LOCAL_MODEL = "llama3.1:8b"
_DEFAULT_LOCAL_HOST = "http://localhost:11434"
_DEFAULT_CLOUD_MODEL = "gpt-oss:120b"
_DEFAULT_CLOUD_HOST = "https://ollama.com"

_VALID_LLM_TYPES = {"ollama_local", "ollama_cloud"}
_VALID_TOP_KEYS = {"storyteller", "state_updater", "runtime"}
_VALID_LLM_KEYS = {"type", "model", "host", "api_key", "temperature", "top_p", "top_k", "prompt_file"}
_VALID_RUNTIME_KEYS = {
    "strict_json",
    "required_language",
    "invalid_retry_count",
    "max_history_turns",
    "require_global_summary",
    "beat_gate_policy",
    "session_id",
    "debug",
    "log_file",
}


@dataclass
class SessionData:
    state: StoryState
    history: List[Dict[str, str]]
    last_scene_text: str
    last_choices: List[str]
    turn_idx: int


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_slug(raw: str, *, default: str = "session") -> str:
    value = re.sub(r"[^a-zA-Z0-9_.-]+", "_", str(raw or "").strip())
    value = value.strip("._-")
    return value or default


def _deep_merge(base: Dict[str, Any], patch: Dict[str, Any]) -> Dict[str, Any]:
    out = copy.deepcopy(base)
    for key, value in (patch or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _coerce_bool(raw: Any, *, key: str) -> bool:
    if isinstance(raw, bool):
        return raw
    raise ValueError(f"{key} moet boolean zijn.")


def _coerce_int(raw: Any, *, key: str, min_value: int = 0) -> int:
    if isinstance(raw, bool):
        raise ValueError(f"{key} moet integer zijn.")
    try:
        value = int(raw)
    except Exception as exc:  # pragma: no cover
        raise ValueError(f"{key} moet integer zijn.") from exc
    if value < min_value:
        raise ValueError(f"{key} moet >= {min_value} zijn.")
    return value


def _coerce_float_or_none(raw: Any, *, key: str) -> Optional[float]:
    if raw is None or raw == "":
        return None
    if isinstance(raw, bool):
        raise ValueError(f"{key} moet een getal zijn.")
    try:
        value = float(raw)
    except Exception as exc:  # pragma: no cover
        raise ValueError(f"{key} moet een getal zijn.") from exc
    return value


def _resolve_path(raw_path: str, *, base_dir: Path) -> Path:
    path = Path(str(raw_path).strip())
    if not path.is_absolute():
        path = (base_dir / path).resolve()
    return path


def _validate_sampling(cfg: Dict[str, Any], *, section: str) -> None:
    temperature = _coerce_float_or_none(cfg.get("temperature"), key=f"{section}.temperature")
    if temperature is not None and (temperature < 0.0 or temperature > 2.0):
        raise ValueError(f"{section}.temperature moet in [0.0, 2.0] liggen.")
    cfg["temperature"] = temperature

    top_p = _coerce_float_or_none(cfg.get("top_p"), key=f"{section}.top_p")
    if top_p is not None and (top_p <= 0.0 or top_p > 1.0):
        raise ValueError(f"{section}.top_p moet > 0.0 en <= 1.0 zijn.")
    cfg["top_p"] = top_p

    top_k_raw = cfg.get("top_k")
    if top_k_raw is None or top_k_raw == "":
        cfg["top_k"] = None
    else:
        cfg["top_k"] = _coerce_int(top_k_raw, key=f"{section}.top_k", min_value=1)


def _validate_llm_section(raw: Any, *, section: str, base_dir: Path, default_prompt: Path) -> Dict[str, Any]:
    cfg_in = copy.deepcopy(raw if isinstance(raw, dict) else {})
    unknown = sorted(set(cfg_in.keys()) - _VALID_LLM_KEYS)
    if unknown:
        raise ValueError(f"Onbekende keys in {section}: {', '.join(unknown)}")

    llm_type = str(cfg_in.get("type") or "ollama_local").strip().lower()
    if llm_type not in _VALID_LLM_TYPES:
        raise ValueError(f"{section}.type moet een van {sorted(_VALID_LLM_TYPES)} zijn.")

    default_model = _DEFAULT_LOCAL_MODEL if llm_type == "ollama_local" else _DEFAULT_CLOUD_MODEL
    default_host = _DEFAULT_LOCAL_HOST if llm_type == "ollama_local" else _DEFAULT_CLOUD_HOST

    model = str(cfg_in.get("model") or default_model).strip()
    if not model:
        raise ValueError(f"{section}.model mag niet leeg zijn.")

    host = str(cfg_in.get("host") or default_host).strip()
    if not host:
        raise ValueError(f"{section}.host mag niet leeg zijn.")

    api_key = cfg_in.get("api_key")
    if api_key is not None:
        api_key = str(api_key).strip() or None

    prompt_file_raw = cfg_in.get("prompt_file")
    if prompt_file_raw is None:
        prompt_path = default_prompt.resolve()
    else:
        prompt_path = _resolve_path(str(prompt_file_raw), base_dir=base_dir)

    cfg = {
        "type": llm_type,
        "model": model,
        "host": host,
        "api_key": api_key,
        "temperature": cfg_in.get("temperature"),
        "top_p": cfg_in.get("top_p"),
        "top_k": cfg_in.get("top_k"),
        "prompt_file": str(prompt_path),
    }
    _validate_sampling(cfg, section=section)
    return cfg


def _validate_runtime_section(raw: Any, *, base_dir: Path) -> Dict[str, Any]:
    cfg_in = copy.deepcopy(raw if isinstance(raw, dict) else {})
    unknown = sorted(set(cfg_in.keys()) - _VALID_RUNTIME_KEYS)
    if unknown:
        raise ValueError(f"Onbekende keys in runtime: {', '.join(unknown)}")

    required_language = str(cfg_in.get("required_language") or "nl").strip().lower()
    if not required_language:
        raise ValueError("runtime.required_language mag niet leeg zijn.")

    session_id = str(cfg_in.get("session_id") or "local_cli").strip()
    if not session_id:
        raise ValueError("runtime.session_id mag niet leeg zijn.")

    beat_gate_policy = str(cfg_in.get("beat_gate_policy") or DEFAULT_BEAT_GATE_POLICY).strip()
    if not beat_gate_policy:
        raise ValueError("runtime.beat_gate_policy mag niet leeg zijn.")

    log_file_raw = cfg_in.get("log_file", "auto")
    if log_file_raw is None:
        log_file = None
    else:
        log_file_text = str(log_file_raw).strip()
        if not log_file_text:
            log_file = "auto"
        elif log_file_text.lower() == "auto":
            log_file = "auto"
        else:
            log_file = str(_resolve_path(log_file_text, base_dir=base_dir))

    return {
        "strict_json": _coerce_bool(cfg_in.get("strict_json", True), key="runtime.strict_json"),
        "required_language": required_language,
        "invalid_retry_count": _coerce_int(cfg_in.get("invalid_retry_count", 1), key="runtime.invalid_retry_count", min_value=0),
        "max_history_turns": _coerce_int(cfg_in.get("max_history_turns", 3), key="runtime.max_history_turns", min_value=0),
        "require_global_summary": _coerce_bool(cfg_in.get("require_global_summary", True), key="runtime.require_global_summary"),
        "beat_gate_policy": beat_gate_policy,
        "session_id": session_id,
        "debug": _coerce_bool(cfg_in.get("debug", False), key="runtime.debug"),
        "log_file": log_file,
    }


def validate_config_dict(raw_cfg: Dict[str, Any], *, source_dir: Path) -> Dict[str, Any]:
    if not isinstance(raw_cfg, dict):
        raise ValueError("Config moet een JSON object zijn.")
    unknown = sorted(set(raw_cfg.keys()) - _VALID_TOP_KEYS)
    if unknown:
        raise ValueError(f"Onbekende top-level keys: {', '.join(unknown)}")

    storyteller_cfg = _validate_llm_section(
        raw_cfg.get("storyteller", {}),
        section="storyteller",
        base_dir=source_dir,
        default_prompt=DEFAULT_STORYTELLER_PROMPT,
    )
    updater_cfg = _validate_llm_section(
        raw_cfg.get("state_updater", {}),
        section="state_updater",
        base_dir=source_dir,
        default_prompt=DEFAULT_UPDATER_PROMPT,
    )
    runtime_cfg = _validate_runtime_section(raw_cfg.get("runtime", {}), base_dir=source_dir)

    return {
        "storyteller": storyteller_cfg,
        "state_updater": updater_cfg,
        "runtime": runtime_cfg,
    }


def _parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Standalone storyteller CLI")
    parser.add_argument("--config", type=str, default=None, help="Pad naar JSON config")

    parser.add_argument("--storyteller-type", choices=sorted(_VALID_LLM_TYPES), default=None)
    parser.add_argument("--storyteller-model", type=str, default=None)
    parser.add_argument("--storyteller-host", type=str, default=None)
    parser.add_argument("--storyteller-api-key", type=str, default=None)
    parser.add_argument("--storyteller-temperature", type=float, default=None)
    parser.add_argument("--storyteller-top-p", type=float, default=None)
    parser.add_argument("--storyteller-top-k", type=int, default=None)
    parser.add_argument("--storyteller-prompt-file", type=str, default=None)

    parser.add_argument("--updater-type", choices=sorted(_VALID_LLM_TYPES), default=None)
    parser.add_argument("--updater-model", type=str, default=None)
    parser.add_argument("--updater-host", type=str, default=None)
    parser.add_argument("--updater-api-key", type=str, default=None)
    parser.add_argument("--updater-temperature", type=float, default=None)
    parser.add_argument("--updater-top-p", type=float, default=None)
    parser.add_argument("--updater-top-k", type=int, default=None)
    parser.add_argument("--updater-prompt-file", type=str, default=None)

    parser.add_argument("--strict-json", dest="strict_json", action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument("--required-language", type=str, default=None)
    parser.add_argument("--invalid-retry-count", type=int, default=None)
    parser.add_argument("--max-history-turns", type=int, default=None)
    parser.add_argument("--require-global-summary", dest="require_global_summary", action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument("--beat-gate-policy", type=str, default=None)
    parser.add_argument("--session-id", type=str, default=None)
    parser.add_argument("--debug", dest="debug", action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument("--log-file", type=str, default=None)
    parser.add_argument("--no-log", action="store_true", help="Schakel JSONL turn-logging uit")

    return parser.parse_args(argv)


def _build_overrides(args: argparse.Namespace) -> Dict[str, Any]:
    overrides: Dict[str, Any] = {"storyteller": {}, "state_updater": {}, "runtime": {}}

    def set_if(section: str, key: str, value: Any) -> None:
        if value is not None:
            overrides[section][key] = value

    set_if("storyteller", "type", args.storyteller_type)
    set_if("storyteller", "model", args.storyteller_model)
    set_if("storyteller", "host", args.storyteller_host)
    set_if("storyteller", "api_key", args.storyteller_api_key)
    set_if("storyteller", "temperature", args.storyteller_temperature)
    set_if("storyteller", "top_p", args.storyteller_top_p)
    set_if("storyteller", "top_k", args.storyteller_top_k)
    set_if("storyteller", "prompt_file", args.storyteller_prompt_file)

    set_if("state_updater", "type", args.updater_type)
    set_if("state_updater", "model", args.updater_model)
    set_if("state_updater", "host", args.updater_host)
    set_if("state_updater", "api_key", args.updater_api_key)
    set_if("state_updater", "temperature", args.updater_temperature)
    set_if("state_updater", "top_p", args.updater_top_p)
    set_if("state_updater", "top_k", args.updater_top_k)
    set_if("state_updater", "prompt_file", args.updater_prompt_file)

    set_if("runtime", "strict_json", args.strict_json)
    set_if("runtime", "required_language", args.required_language)
    set_if("runtime", "invalid_retry_count", args.invalid_retry_count)
    set_if("runtime", "max_history_turns", args.max_history_turns)
    set_if("runtime", "require_global_summary", args.require_global_summary)
    set_if("runtime", "beat_gate_policy", args.beat_gate_policy)
    set_if("runtime", "session_id", args.session_id)
    set_if("runtime", "debug", args.debug)
    set_if("runtime", "log_file", args.log_file)

    if args.no_log:
        overrides["runtime"]["log_file"] = None

    out = {k: v for k, v in overrides.items() if v}
    return out


def load_and_resolve_config(args: argparse.Namespace) -> Dict[str, Any]:
    cfg_raw: Dict[str, Any] = {}
    source_dir = PROJECT_ROOT
    if args.config:
        cfg_path = Path(args.config).resolve()
        source_dir = cfg_path.parent
        try:
            cfg_raw = json.loads(cfg_path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ValueError(f"Config bestand niet gevonden: {cfg_path}") from exc
        except json.JSONDecodeError as exc:
            raise ValueError(f"Config JSON ongeldig in {cfg_path}: {exc}") from exc
    merged = _deep_merge(cfg_raw, _build_overrides(args))
    return validate_config_dict(merged, source_dir=source_dir)


def load_prompt_text(path_text: str, *, label: str) -> str:
    path = Path(path_text)
    try:
        text = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError as exc:
        raise ValueError(f"{label} prompt niet gevonden: {path}") from exc
    if not text:
        raise ValueError(f"{label} prompt is leeg: {path}")
    return text


def resolve_user_input(raw: str, last_choices: Optional[List[str]]) -> Tuple[str, Optional[str]]:
    text = str(raw or "").strip()
    if text in {"1", "2"}:
        idx = int(text) - 1
        if isinstance(last_choices, list) and idx < len(last_choices):
            mapped = str(last_choices[idx] or "").strip()
            if mapped:
                return mapped, f"Keuze {text} gemapt naar: {mapped}"
    return text, None


def _clip_for_console(value: Any, *, max_len: int = 3200) -> str:
    if isinstance(value, str):
        text = value
    else:
        text = json.dumps(value, ensure_ascii=False, indent=2)
    if len(text) <= max_len:
        return text
    omitted = len(text) - max_len
    return text[:max_len] + f"\n...[truncated {omitted} chars]"


def _state_delta(before: Dict[str, Any], after: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    delta: Dict[str, Dict[str, Any]] = {}
    keys = sorted(set(before.keys()) | set(after.keys()))
    for key in keys:
        a = before.get(key)
        b = after.get(key)
        if a != b:
            delta[key] = {"before": a, "after": b}
    return delta


def print_debug_turn(turn_idx: int, result: Dict[str, Any], state_before: Dict[str, Any], state_after: Dict[str, Any]) -> None:
    logs = result.get("logs") if isinstance(result.get("logs"), dict) else {}
    updater = logs.get("state_updater") if isinstance(logs.get("state_updater"), dict) else {}
    storyteller = logs.get("storyteller") if isinstance(logs.get("storyteller"), dict) else {}

    print(f"\n=== TURN {turn_idx} ===")
    print("--- STATE UPDATER INPUT ---")
    print(_clip_for_console((updater.get("attempt_inputs") or [])))
    print("--- STATE UPDATER RAW OUTPUT ---")
    print(_clip_for_console(updater.get("raw_output") or "(geen updater output)"))
    print("--- STATE PATCH + BEAT DECISION ---")
    print(_clip_for_console({"patch": result.get("patch"), "beat_decision": result.get("beat_decision")}))
    print("--- STORYTELLER INPUT ---")
    print(_clip_for_console((storyteller.get("attempt_inputs") or [])))
    print("--- STORYTELLER RAW OUTPUT ---")
    print(_clip_for_console(storyteller.get("raw_output") or ""))
    print("--- STATE DELTA ---")
    print(_clip_for_console(_state_delta(state_before, state_after)))


def _history_for_llm(history: List[Dict[str, str]], max_history_turns: int) -> List[Dict[str, str]]:
    if max_history_turns <= 0:
        return []
    return history[-(max_history_turns * 2) :]


def _session_payload(session: SessionData) -> Dict[str, Any]:
    return {
        "saved_at": _iso_now(),
        "state": copy.deepcopy(session.state),
        "history": copy.deepcopy(session.history),
        "last_scene_text": str(session.last_scene_text or ""),
        "last_choices": [str(v or "") for v in (session.last_choices or [])][:2],
        "turn_idx": int(session.turn_idx or 0),
    }


def _restore_session_payload(payload: Optional[Dict[str, Any]]) -> SessionData:
    if not isinstance(payload, dict):
        return SessionData(state=default_story_state(), history=[], last_scene_text="", last_choices=[], turn_idx=0)

    state = validate_story_state(payload.get("state") or default_story_state())

    history: List[Dict[str, str]] = []
    for item in payload.get("history") or []:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "").strip().lower()
        content = str(item.get("content") or "").strip()
        if role not in {"user", "assistant"} or not content:
            continue
        history.append({"role": role, "content": content})

    last_choices: List[str] = []
    for choice in payload.get("last_choices") or []:
        text = str(choice or "").strip()
        if text:
            last_choices.append(text)
        if len(last_choices) >= 2:
            break

    turn_idx_raw = payload.get("turn_idx", 0)
    try:
        turn_idx = max(0, int(turn_idx_raw))
    except Exception:
        turn_idx = 0

    return SessionData(
        state=state,
        history=history,
        last_scene_text=str(payload.get("last_scene_text") or ""),
        last_choices=last_choices,
        turn_idx=turn_idx,
    )


def _resolve_log_path(runtime_cfg: Dict[str, Any], runtime_dir: Path) -> Optional[Path]:
    log_file = runtime_cfg.get("log_file", "auto")
    if log_file is None:
        return None
    if str(log_file).strip().lower() == "auto":
        session_id = str(runtime_cfg.get("session_id") or "local_cli")
        return runtime_dir / f"turns_{_safe_slug(session_id)}.jsonl"
    return Path(str(log_file)).resolve()


def _append_jsonl(path: Path, record: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def _print_help() -> None:
    print(
        "\nBeschikbare commands:\n"
        "  /help            Toon deze help\n"
        "  /debug on|off    Zet debug-output aan of uit\n"
        "  /state           Toon compacte state\n"
        "  /save <name>     Sla huidige sessie op\n"
        "  /load <name>     Laad opgeslagen sessie\n"
        "  /saves           Toon beschikbare saves\n"
        "  /history [n]     Toon laatste n turns (default 5)\n"
        "  /quit            Stop de sessie\n"
    )


def _print_state_summary(state: Dict[str, Any]) -> None:
    print("\nState summary:")
    print(f"  beat_idx: {state.get('beat_idx')}")
    print(f"  phase: {state.get('phase')}")
    print(f"  active_quests: {len(state.get('active_quests') or [])}")
    print(f"  threads: {len(state.get('threads') or [])}")
    print(f"  pinned_facts: {len(state.get('pinned_facts') or [])}")
    summary = str(state.get("global_summary") or "").strip()
    if summary:
        print(f"  global_summary: {summary[:200]}")


def _print_history(history: List[Dict[str, str]], turns: int) -> None:
    if not history:
        print("\n(geen history)")
        return
    turns = max(1, turns)
    chunk = history[-(turns * 2) :]
    print("")
    for item in chunk:
        role = str(item.get("role") or "").upper()
        content = str(item.get("content") or "")
        print(f"[{role}] {content}")


def _create_llm_backend(cfg: Dict[str, Any]) -> Any:
    return build_story_llm_backend(
        llm_type=str(cfg.get("type") or "ollama_local"),
        llm_model=str(cfg.get("model") or "").strip(),
        base_params={
            "host": str(cfg.get("host") or "").strip(),
            "api_key": cfg.get("api_key"),
            "temperature": cfg.get("temperature"),
            "top_p": cfg.get("top_p"),
            "top_k": cfg.get("top_k"),
        },
    )


def _print_runtime_banner(cfg: Dict[str, Any], *, runtime_dir: Path, log_path: Optional[Path], debug_enabled: bool) -> None:
    print("Standalone Storyteller CLI")
    print(f"  session_id: {cfg['runtime']['session_id']}")
    print(f"  storyteller: {cfg['storyteller']['type']} | {cfg['storyteller']['model']}")
    print(f"  updater: {cfg['state_updater']['type']} | {cfg['state_updater']['model']}")
    print(f"  runtime_dir: {runtime_dir}")
    print(f"  debug: {'on' if debug_enabled else 'off'}")
    if log_path is None:
        print("  log_file: disabled")
    else:
        print(f"  log_file: {log_path}")
    print("Typ /help voor commands.\n")


def main(argv: Optional[List[str]] = None) -> int:
    args = _parse_args(argv)
    try:
        cfg = load_and_resolve_config(args)
    except Exception as exc:
        print(f"Config fout: {exc}")
        return 2

    runtime_dir = Path(build_story_runtime_dir(str(PROJECT_ROOT))).resolve()
    runtime_dir.mkdir(parents=True, exist_ok=True)

    try:
        storyteller_prompt = load_prompt_text(cfg["storyteller"]["prompt_file"], label="storyteller")
        updater_prompt = load_prompt_text(cfg["state_updater"]["prompt_file"], label="state_updater")
    except Exception as exc:
        print(f"Prompt fout: {exc}")
        return 2

    try:
        storyteller_llm = _create_llm_backend(cfg["storyteller"])
        updater_llm = _create_llm_backend(cfg["state_updater"])
    except Exception as exc:
        print(f"LLM init fout: {exc}")
        return 2

    runtime_cfg = cfg["runtime"]
    debug_enabled = bool(runtime_cfg.get("debug"))
    log_path = _resolve_log_path(runtime_cfg, runtime_dir)

    session_id = str(runtime_cfg.get("session_id") or "local_cli")
    autosave_payload = load_autosave(str(runtime_dir), instance_id=INSTANCE_ID, sid=session_id)
    session = _restore_session_payload(autosave_payload)
    if autosave_payload:
        print(f"Autosave geladen voor session '{session_id}'.")

    _print_runtime_banner(cfg, runtime_dir=runtime_dir, log_path=log_path, debug_enabled=debug_enabled)

    while True:
        try:
            raw_input_text = input("story> ").strip()
        except EOFError:
            print("\nEOF ontvangen, sessie wordt afgesloten.")
            break
        except KeyboardInterrupt:
            print("\nAfgebroken door gebruiker.")
            break

        if not raw_input_text:
            continue

        if raw_input_text.startswith("/"):
            parts = raw_input_text.split()
            cmd = parts[0].lower()

            if cmd in {"/quit", "/exit"}:
                print("Sessie gestopt.")
                break
            if cmd == "/help":
                _print_help()
                continue
            if cmd == "/debug":
                if len(parts) != 2 or parts[1].lower() not in {"on", "off"}:
                    print("Gebruik: /debug on|off")
                    continue
                debug_enabled = parts[1].lower() == "on"
                print(f"Debug is nu: {'ON' if debug_enabled else 'OFF'}")
                continue
            if cmd == "/state":
                _print_state_summary(session.state)
                continue
            if cmd == "/saves":
                saves = list_named_saves(str(runtime_dir), instance_id=INSTANCE_ID, sid=session_id)
                if not saves:
                    print("(geen saves)")
                else:
                    print("\nSaves:")
                    for name in saves:
                        print(f"  - {name}")
                continue
            if cmd == "/save":
                if len(parts) < 2:
                    print("Gebruik: /save <name>")
                    continue
                save_name = " ".join(parts[1:]).strip()
                try:
                    normalized, path = save_named(
                        str(runtime_dir),
                        instance_id=INSTANCE_ID,
                        sid=session_id,
                        save_name=save_name,
                        payload=_session_payload(session),
                    )
                    print(f"Save opgeslagen: {normalized} ({path})")
                except Exception as exc:
                    print(f"Save fout: {exc}")
                continue
            if cmd == "/load":
                if len(parts) < 2:
                    print("Gebruik: /load <name>")
                    continue
                save_name = " ".join(parts[1:]).strip()
                payload = load_named(str(runtime_dir), instance_id=INSTANCE_ID, sid=session_id, save_name=save_name)
                if not payload:
                    print(f"Save niet gevonden: {save_name}")
                    continue
                try:
                    session = _restore_session_payload(payload)
                except Exception as exc:
                    print(f"Load fout: {exc}")
                    continue
                print(f"Save geladen: {save_name}")
                continue
            if cmd == "/history":
                turns = 5
                if len(parts) >= 2:
                    try:
                        turns = max(1, int(parts[1]))
                    except Exception:
                        print("Gebruik: /history [n]")
                        continue
                _print_history(session.history, turns)
                continue

            print("Onbekend command. Gebruik /help")
            continue

        user_input_resolved, hint = resolve_user_input(raw_input_text, session.last_choices)
        if not user_input_resolved:
            continue

        state_before = copy.deepcopy(session.state)
        history_for_llm = _history_for_llm(session.history, int(runtime_cfg["max_history_turns"]))
        try:
            result = run_story_turn(
                storyteller_llm=storyteller_llm,
                state_updater_llm=updater_llm,
                storyteller_system_prompt=storyteller_prompt,
                state_updater_system_prompt=updater_prompt,
                state=session.state,
                last_scene_text=session.last_scene_text,
                user_input=user_input_resolved,
                history=history_for_llm,
                strict_json=bool(runtime_cfg["strict_json"]),
                max_history_turns=int(runtime_cfg["max_history_turns"]),
                required_language=str(runtime_cfg["required_language"]),
                invalid_retry_count=int(runtime_cfg["invalid_retry_count"]),
                require_global_summary=bool(runtime_cfg["require_global_summary"]),
                beat_gate_policy=str(runtime_cfg["beat_gate_policy"]),
            )
        except StoryTurnCancelledError as exc:
            print(f"Turn geannuleerd: {exc}")
            continue
        except StoryLLMOutputRejectedError as exc:
            print(f"LLM output afgewezen: {exc}")
            if debug_enabled:
                print(_clip_for_console({
                    "role": exc.role_label,
                    "attempts": exc.attempts,
                    "errors": exc.errors,
                    "attempt_outputs": exc.attempt_outputs,
                    "retry_contexts": exc.retry_contexts,
                }))
            continue
        except Exception as exc:
            print(f"Turn fout: {exc}")
            continue

        session.turn_idx += 1
        scene = str(result.get("scene") or "").strip()
        choices = [str(v or "").strip() for v in (result.get("choices") or []) if str(v or "").strip()]
        reply = str(result.get("reply") or "").strip()

        session.state = validate_story_state(result.get("state") or session.state)
        session.last_scene_text = scene
        session.last_choices = choices[:2]

        session.history.append({"role": "user", "content": user_input_resolved})
        session.history.append({"role": "assistant", "content": reply})

        print("\nScene:")
        print(scene)
        print("\nKeuzes:")
        for idx, choice in enumerate(choices[:2], start=1):
            print(f"{idx}. {choice}")
        if hint:
            print(f"\nHint: {hint}")

        state_after = copy.deepcopy(session.state)
        if debug_enabled:
            print_debug_turn(session.turn_idx, result, state_before, state_after)

        autosave_payload = _session_payload(session)
        save_autosave(str(runtime_dir), instance_id=INSTANCE_ID, sid=session_id, payload=autosave_payload)

        if log_path is not None:
            log_record = {
                "timestamp": _iso_now(),
                "turn_idx": session.turn_idx,
                "user_input_raw": raw_input_text,
                "user_input_resolved": user_input_resolved,
                "scene": scene,
                "choices": choices,
                "reply": reply,
                "state_before": state_before,
                "state_after": state_after,
                "patch": result.get("patch"),
                "beat_decision": result.get("beat_decision"),
                "diagnostics": result.get("logs"),
            }
            _append_jsonl(log_path, log_record)

    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
