# py3_nao_behavior_manager/dialog/pipeline.py
from __future__ import annotations

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
    """

    def __init__(self, input_backend, llm, output_backend, *, status_to_console: bool = True) -> None:
        self.input = input_backend
        self.llm = llm
        self.output = output_backend
        self.status_to_console = status_to_console

    def _status(self, msg: str) -> None:
        if self.status_to_console:
            print(msg)

    def run_once(self, history: History | None = None) -> DialogTurn:
        user_in: UserInput = self.input.get_input()

        # Cancel/empty input => geen LLM call, geen output
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

        self._status("🤖 THINKING...")
        llm_res = self.llm.generate(messages)

        self._status("📣 OUTPUT...")
        self.output.emit(llm_res.reply)

        return DialogTurn(
            user_input=user_in,
            llm=llm_res,
            user_audio=user_in.audio,
            stt=user_in.stt,
        )


def build_pipeline(profile_name: str = "nao_whisper_ollama_cloud"):
    raise RuntimeError(
        "Gebruik scripts/run_from_json.py met configs/*.json "
        "in plaats van build_pipeline(profile)."
    )
