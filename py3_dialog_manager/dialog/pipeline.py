# py3_nao_behavior_manager/dialog/pipeline.py
from __future__ import annotations

import json
from datetime import datetime, timezone
import threading
import re
from typing import Optional, Dict, Any, List

from dialog.interfaces import (
    DialogPipeline,
    DialogTurn,
    History,
    ChatMessage,
    UserInput,
    LLMResult,
)
from dialog.backends.llm_echo import EchoLLMBackend


_OUTPUT_STYLE_GUARD = (
    "## OUTPUT_STIJL (HARD)\n\n"
    "- Geef alleen spreekbare platte tekst terug (geen markdown).\n"
    "- Gebruik geen markdown-tekens zoals *, _, #, `, [ ], ( ) of bullets.\n"
    "- Herhaal geen systeemstatusregels zoals 'Uitgevoerd:', 'Geannuleerd:' of 'Verlopen:'.\n"
    "- Noem geen interne commandolabels letterlijk (zoals STAND_UP) als statusregel."
)

_EMOJI_RE = re.compile(
    "["
    "\U0001F1E6-\U0001F1FF"  # regional indicator symbols (flags)
    "\U0001F300-\U0001FAFF"  # misc symbols and pictographs + supplemental
    "\U00002600-\U000027BF"  # misc symbols + dingbats
    "]",
    flags=re.UNICODE,
)


def _role(msg: Any) -> Optional[str]:
    if isinstance(msg, dict):
        return msg.get("role")
    return getattr(msg, "role", None)


class InputLLMOutputPipeline(DialogPipeline):
    """
    Input -> LLM -> Output

    - system_prompt: als eerste system-message meegestuurd
    - log_messages_path: JSONL met exacte 'messages' die naar LLM gaan
    - max_history_turns: bewaart laatste N user-turns (inclusief huidige user turn)
    """

    def __init__(
        self,
        input_backend,
        llm,
        output_backend,
        *,
        status_to_console: bool = True,
        system_prompt: Optional[str] = None,
        log_messages_path: Optional[str] = None,
        log_meta: Optional[Dict[str, Any]] = None,
        max_history_turns: Optional[int] = None,
        cmdrec_recognizer=None,
        behavior_executor=None,
        debug_cmdrec: bool = False,
        behavior_reset_timeout_s: float = 10.0,
        runtime_context_enabled: bool = False,
        runtime_context_static: Optional[Dict[str, str]] = None,
        runtime_last_action: Optional[str] = None,
    ) -> None:
        self.input = input_backend
        self.llm = llm
        self.output = output_backend

        self.status_to_console = status_to_console
        self.system_prompt_base = system_prompt.strip() if system_prompt else None
        self.system_prompt = self.system_prompt_base

        self.log_messages_path = log_messages_path
        self.log_meta = log_meta or {}
        self.max_history_turns = max_history_turns

        self._runtime_context_enabled = runtime_context_enabled
        self._runtime_context_static = runtime_context_static or {}
        self._runtime_last_action = runtime_last_action
        self.runtime_context_enabled = self._runtime_context_enabled
        self.runtime_context_static = dict(self._runtime_context_static)

        self._cmdrec = cmdrec_recognizer
        self._behavior_executor = behavior_executor
        self._behavior_reset_timeout_s = behavior_reset_timeout_s
        self._behavior_reset_timer: Optional[threading.Timer] = None
        self._behavior_finish_callback_enabled = self._register_behavior_finish_callback()
        self._cmdrec_mode = "DIALOG"
        self._active_behavior: Optional[str] = None
        self._debug_cmdrec = debug_cmdrec
        self._last_cmdrec_debug: Optional[Dict[str, Any]] = None

        self._turn_idx = 0

    def _status(self, msg: str) -> None:
        if self.status_to_console:
            print(msg)

    def get_system_prompt(self, *, last_action: Optional[str] = None) -> Optional[str]:
        return self._build_system_prompt(last_action=last_action)

    def set_last_action(self, summary: Optional[str]) -> None:
        self._runtime_last_action = summary

    def _build_runtime_context_text(self, *, last_action: Optional[str]) -> str:
        if not self._runtime_context_enabled:
            return ""
        available = self._runtime_context_static.get("available_commands", "")
        dances = self._runtime_context_static.get("dance_catalog", "")
        last_action_text = last_action or ""

        blocks = [
            "## CONTEXT_GEBRUIK (runtime)\n\n"
            "Gebruik onderstaande blokken alleen als context. "
            "Herhaal geen statusregels of commandolabels letterlijk in je antwoord.",
            "## BESCHIKBARE_COMMANDOS (runtime)\n\n" + (available or ""),
            "## DANS_CATALOGUS (runtime)\n\n" + (dances or ""),
            "## LAATSTE_ACTIE (runtime, may be empty)\n\n" + (last_action_text or ""),
        ]
        return "\n\n".join(blocks)

    def _build_system_prompt(self, *, last_action: Optional[str] = None) -> Optional[str]:
        base = self.system_prompt_base or ""
        context = self._build_runtime_context_text(
            last_action=self._runtime_last_action if last_action is None else last_action
        )
        guard = _OUTPUT_STYLE_GUARD
        if not base and not context and not guard:
            return None
        parts = [part for part in (base, context, guard) if part]
        return "\n\n---\n\n".join(parts)

    def normalize_output_text(self, text: str) -> str:
        out = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
        if not out:
            return ""

        # Guardrail: suppress leading execution status lines in spoken output.
        status_re = re.compile(r"^\s*(?:ok\.?\s*)?(uitgevoerd|geannuleerd|verlopen)\s*:\s*.+$", re.IGNORECASE)
        kept_lines: List[str] = []
        for line in out.split("\n"):
            if status_re.match(line or ""):
                continue
            kept_lines.append(line)
        out = "\n".join(kept_lines)

        # Strip common markdown constructs that sound awkward in TTS.
        out = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1", out)
        out = re.sub(r"(?<!\\)\*\*(.+?)(?<!\\)\*\*", r"\1", out)
        out = re.sub(r"(?<!\\)\*(.+?)(?<!\\)\*", r"\1", out)
        out = re.sub(r"(?<!\\)__(.+?)(?<!\\)__", r"\1", out)
        out = re.sub(r"(?<!\\)_(.+?)(?<!\\)_", r"\1", out)
        out = out.replace("`", "")
        out = out.replace("\\*", "*").replace("\\_", "_").replace("\\`", "`")
        out = out.replace("*", "")
        out = re.sub(r"(?<=\w)_(?=\w)", " ", out)
        out = out.replace("\ufe0f", "").replace("\u200d", "").replace("\u20e3", "")
        out = _EMOJI_RE.sub("", out)
        out = re.sub(r"[ \t]{2,}", " ", out)
        out = re.sub(r"\s+([,.;:!?])", r"\1", out)
        out = re.sub(r"[ \t]+\n", "\n", out)
        out = re.sub(r"\n{3,}", "\n\n", out)
        return out.strip()

    def _log_messages(self, messages: History) -> None:
        if not self.log_messages_path:
            return

        rec = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "turn_idx": self._turn_idx,
            "meta": self.log_meta,
            "messages": messages,
        }
        line = json.dumps(rec, ensure_ascii=False)
        with open(self.log_messages_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
            f.flush()

    def _prepend_system_prompt(self, messages: History) -> History:
        system_prompt = self._build_system_prompt()
        if not system_prompt:
            return messages

        if messages and _role(messages[0]) == "system":
            return [ChatMessage(role="system", content=system_prompt)] + list(messages[1:])

        sys_msg = ChatMessage(role="system", content=system_prompt)  # type: ignore[arg-type]
        return [sys_msg] + list(messages)

    def _trim_history(self, history: History) -> History:
        """
        Bewaar laatste N user-turns. Turn = een user-message; we knippen history
        vanaf de N-de laatste user-message tot het eind.

        Als history begint met een system-message, behouden we die.
        """
        n = self.max_history_turns
        if n is None:
            return history
        if n <= 0:
            if history and _role(history[0]) == "system":
                return [history[0]]
            return []

        sys_prefix: List[Any] = []
        rest: List[Any] = list(history)

        if rest and _role(rest[0]) == "system":
            sys_prefix = [rest[0]]
            rest = rest[1:]

        user_idxs = [i for i, m in enumerate(rest) if _role(m) == "user"]
        if len(user_idxs) <= n:
            return sys_prefix + rest

        cut = user_idxs[-n]
        return sys_prefix + rest[cut:]

    def _log_cmdrec(self, decision, bundle_path: Optional[str]) -> None:
        if not self._debug_cmdrec:
            return
        top3 = decision.top3 or []
        top3_str = ", ".join([f"{label}:{score:.3f}" for label, score in top3])
        if decision.is_command and decision.command:
            print(
                f"CMD: {decision.command.label} "
                f"({decision.command.confidence:.3f}) top3=[{top3_str}]"
            )
        else:
            print(f"DIALOG: reason={decision.reason or 'none'} top3=[{top3_str}]")

    def _reset_behavior_state(self, *args, **kwargs) -> None:
        if self._behavior_reset_timer is not None:
            self._behavior_reset_timer.cancel()
            self._behavior_reset_timer = None
        self._cmdrec_mode = "DIALOG"
        self._active_behavior = None

    def _schedule_behavior_reset(self) -> None:
        if self._behavior_reset_timer is not None:
            self._behavior_reset_timer.cancel()
        self._behavior_reset_timer = threading.Timer(
            self._behavior_reset_timeout_s,
            self._reset_behavior_state,
        )
        self._behavior_reset_timer.daemon = True
        self._behavior_reset_timer.start()

    def _register_behavior_finish_callback(self) -> bool:
        if self._behavior_executor is None:
            return False
        setter = getattr(self._behavior_executor, "set_on_finish", None)
        if callable(setter):
            setter(self._reset_behavior_state)
            return True
        return False

    def _format_action_summary(self, label: str, resolved: Optional[Dict[str, Any]]) -> str:
        if resolved:
            items = ", ".join([f"{k}={v}" for k, v in sorted(resolved.items())])
            return f"Uitgevoerd: {label}; resolved: {items}"
        return f"Uitgevoerd: {label}"

    def _set_last_action_from_command(self, command: Optional[Any]) -> None:
        if not self._runtime_context_enabled:
            return
        if not command:
            return
        label = getattr(command, "label", None)
        if not label:
            return
        resolved = getattr(command, "resolved", None)
        self._runtime_last_action = self._format_action_summary(label, resolved)

    def run_once(self, history: History | None = None) -> DialogTurn:
        user_in: UserInput = self.input.get_input()

        # Lege input => geen LLM call, geen output (en dus ook niets te loggen)
        if not (user_in.text or "").strip():
            llm_res = LLMResult(reply="", messages=list(history or []))
            return DialogTurn(
                user_input=user_in,
                llm=llm_res,
                user_audio=user_in.audio,
                stt=user_in.stt,
            )

        if self._cmdrec is not None:
            decision = self._cmdrec.route(user_in.text, self._cmdrec_mode, self._active_behavior)
            bundle_path = getattr(self._cmdrec, "bundle_path", None)
            bundle_value = str(bundle_path) if bundle_path else None
            self._log_cmdrec(decision, bundle_value)
            if self._debug_cmdrec:
                self._last_cmdrec_debug = {
                    "routed_as": "command" if decision.is_command else "dialog",
                    "reason": decision.reason or None,
                    "label": decision.command.label if decision.command else None,
                    "confidence": decision.command.confidence if decision.command else None,
                    "top3": decision.top3,
                }
            else:
                self._last_cmdrec_debug = None

            if decision.is_command and decision.command:
                if self._behavior_executor:
                    if decision.command.label == "STOP":
                        self._behavior_executor.execute(decision.command)
                        self._reset_behavior_state()
                    else:
                        self._cmdrec_mode = "PERFORMING"
                        self._active_behavior = decision.command.label
                        self._behavior_executor.execute(decision.command)
                        if not self._behavior_finish_callback_enabled:
                            self._schedule_behavior_reset()
                else:
                    self._reset_behavior_state()

                self._set_last_action_from_command(decision.command)

                reply_text = (
                    user_in.text
                    if isinstance(self.llm, EchoLLMBackend)
                    else f"[CMD] {decision.command.label}"
                )
                llm_res = LLMResult(
                    reply=reply_text,
                    messages=list(history or []),
                )
                return DialogTurn(
                    user_input=user_in,
                    llm=llm_res,
                    user_audio=user_in.audio,
                    stt=user_in.stt,
                )
        elif self._debug_cmdrec:
            self._last_cmdrec_debug = {
                "routed_as": "dialog",
                "reason": "disabled",
                "label": None,
                "confidence": None,
                "top3": None,
            }
            print("DIALOG: reason=disabled top3=[]")
        else:
            self._last_cmdrec_debug = None

        messages: History = list(history or [])

        # Voeg huidige user toe
        messages.append(ChatMessage(role="user", content=user_in.text))  # type: ignore[arg-type]

        # Trim nu (dus inclusief de huidige user turn)
        messages = self._trim_history(messages)

        # System prompt toevoegen (als eerste)
        messages_to_send = self._prepend_system_prompt(messages)

        # LOG: exact wat we gaan sturen
        self._log_messages(messages_to_send)

        self._status("🤖 THINKING...")
        llm_res = self.llm.generate(messages_to_send)

        self._status("📣 OUTPUT...")
        spoken_text = self.normalize_output_text(llm_res.reply)
        if spoken_text:
            self.output.emit(spoken_text)

        turn = DialogTurn(
            user_input=user_in,
            llm=llm_res,
            user_audio=user_in.audio,
            stt=user_in.stt,
        )

        self._turn_idx += 1
        return turn


def build_pipeline(profile_name: str = "nao_whisper_ollama_cloud"):
    raise RuntimeError(
        "Gebruik scripts/run_from_json.py met configs/*.json "
        "in plaats van build_pipeline(profile)."
    )
