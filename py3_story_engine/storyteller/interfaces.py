from __future__ import annotations

from dataclasses import dataclass
from typing import List, Literal, Optional, Protocol, TypedDict, runtime_checkable

ChatRole = Literal["system", "user", "assistant"]


class ChatMessage(TypedDict):
    role: ChatRole
    content: str


History = List[ChatMessage]


@dataclass
class LLMResult:
    reply: str
    messages: History
    tokens_in: Optional[int] = None
    tokens_out: Optional[int] = None


@runtime_checkable
class LLMBackend(Protocol):
    def generate(self, messages: History) -> LLMResult:
        ...
