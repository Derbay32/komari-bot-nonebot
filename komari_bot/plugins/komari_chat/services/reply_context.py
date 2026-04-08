"""引用消息上下文结构。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

ReplySourceSide = Literal["assistant", "user"]


@dataclass(frozen=True)
class ReplyContext:
    """当前回复链路中可见的被引用消息上下文。"""

    source_side: ReplySourceSide
    message_id: str
    user_id: str | None
    user_nickname: str | None
    text: str
    image_sources: tuple[str, ...]
    image_count: int
    has_visible_image: bool
