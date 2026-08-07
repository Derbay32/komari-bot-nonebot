"""OneBot 消息构造辅助函数。"""

from __future__ import annotations

from nonebot.adapters.onebot.v11 import Message, MessageSegment


def plain_text_message(text: object) -> Message:
    """把动态内容构造成单一文本段，禁止再次解析 CQ 码。"""
    return Message(MessageSegment.text(str(text)))
