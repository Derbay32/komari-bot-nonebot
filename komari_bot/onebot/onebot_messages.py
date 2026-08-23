"""OneBot 消息构造与事件侧便捷辅助函数。"""

from __future__ import annotations

from nonebot.adapters.onebot.v11 import Message, MessageEvent, MessageSegment


def plain_text_message(text: object) -> Message:
    """把动态内容构造成单一文本段，禁止再次解析 CQ 码。"""
    return Message(MessageSegment.text(str(text)))


def get_user_nickname(event: MessageEvent) -> str:
    """获取用户昵称。

    优先使用群昵称（card），其次使用用户昵称，最后使用「用户（ID）」占位。
    该逻辑收敛到 OneBot 层共享 utility，避免业务插件重复复制昵称辅助函数。

    Args:
        event: OneBot 消息事件

    Returns:
        用户昵称
    """
    sender = getattr(event, "sender", None)
    if sender is not None:
        card = getattr(sender, "card", None)
        if card:
            return card
        nickname = getattr(sender, "nickname", None)
        if nickname:
            return nickname

    user_id = getattr(event, "get_user_id", None)
    if callable(user_id):
        try:
            return f"用户（{user_id()}）"
        except TypeError:  # pragma: no cover - 防御性兜底
            return "用户"
    return "用户"
