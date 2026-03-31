"""OneBot 事件规则辅助函数。"""

from __future__ import annotations

from nonebot.adapters.onebot.v11 import GroupMessageEvent
from nonebot.rule import Rule, is_type, to_me


def group_message_rule() -> Rule:
    """仅匹配群消息事件。"""
    return is_type(GroupMessageEvent)


def group_message_to_me_rule() -> Rule:
    """仅匹配与机器人相关的群消息事件。"""
    return to_me() & group_message_rule()
