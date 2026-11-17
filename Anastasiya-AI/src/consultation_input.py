from dataclasses import dataclass
from typing import Callable, Awaitable, Any

@dataclass(frozen=True)
class ConsultationInput:
    sender_id: int
    chat_id: int
    id: int
    raw_text: str
    sender: Any
    reply: Callable[..., Awaitable[Any]]
