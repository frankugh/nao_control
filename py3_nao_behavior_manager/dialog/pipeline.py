# py3_nao_behavior_manager/dialog/pipeline.py
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List

from dialog.interfaces import (
    DialogPipeline,
    DialogTurn,
    History,
    ChatMessage,
    UserInput,
    LLMResult,
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
    ) -> None:
        self.input = input_backend
        self.llm = llm
        self.output = output_backend

        self.status_to_console = status_to_console
        self.system_prompt = system_prompt.strip() if system_prompt else None

        self.log_messages_path = log_messages_path
        self.log_meta = log_meta or {}
        self.max_history_turns = max_history_turns

        self._turn_idx = 0

    def _status(self, msg: str) -> None:
        if self.status_to_console:
            print(msg)

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
        if not self.system_prompt:
            return messages

        if messages and _role(messages[0]) == "system":
            return messages

        sys_msg = ChatMessage(role="system", content=self.system_prompt)  # type: ignore[arg-type]
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
        self.output.emit(llm_res.reply)

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
