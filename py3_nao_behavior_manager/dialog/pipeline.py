# py3_nao_behavior_manager/dialog/pipeline.py
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Optional, Dict, Any

from dialog.interfaces import (
    DialogPipeline,
    DialogTurn,
    History,
    ChatMessage,
    UserInput,
    LLMResult,
)


class InputLLMOutputPipeline(DialogPipeline):
    """
    Input -> LLM -> Output

    - Optioneel system_prompt: wordt als eerste system-message meegestuurd.
    - Optioneel per turn logging van exacte 'messages' naar JSONL.
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
    ) -> None:
        self.input = input_backend
        self.llm = llm
        self.output = output_backend

        self.status_to_console = status_to_console
        self.system_prompt = system_prompt.strip() if system_prompt else None

        self.log_messages_path = log_messages_path
        self.log_meta = log_meta or {}
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

        if messages and getattr(messages[0], "role", None) == "system":
            return messages

        sys_msg = ChatMessage(role="system", content=self.system_prompt)  # type: ignore[arg-type]
        return [sys_msg] + list(messages)

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
        messages.append(ChatMessage(role="user", content=user_in.text))  # type: ignore[arg-type]

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
