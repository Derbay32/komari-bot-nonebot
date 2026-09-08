"""QQ 群 ``/bind`` 绑定向导的唯一 QQ 入口。

只处理精确 ``GroupAtMessageCreateEvent`` 且正文以 ``/bind`` 开头的入站消息；
准入 token 由 274 事件门禁写入共享 ``state``，本模块不重复
``qualify_qq_event`` 或初始 claim。发送使用真实 QQ Markdown 载荷
（``markdown.content`` + ``msg_type=2`` + ``action.type=2`` 按钮），挑战消息
携带原生引用；发送结果不确定时不补发任何 fallback。
"""

from __future__ import annotations

from nonebot import logger, on_message
from nonebot.adapters.qq.event import GroupAtMessageCreateEvent
from nonebot.adapters.qq.message import Message, MessageSegment
from nonebot.adapters.qq.models import (
    Action,
    Button,
    InlineKeyboard,
    InlineKeyboardRow,
    MessageKeyboard,
    MessageReference,
    RenderData,
)
from nonebot.typing import T_State  # noqa: TC002 - NoneBot 运行时解析 DI 注解

from komari_bot.plugins.group_admission import get_qq_admission_token

from .wizard import WizardReply, get_binding_wizard

bind_qq = on_message(priority=2, block=False)


def _qq_message(reply: WizardReply) -> Message:
    """把向导回复组装为真实 QQ Markdown + 键盘载荷。"""
    message = Message()
    if reply.reply_to_message_id:
        message += MessageSegment.reference(
            MessageReference(message_id=reply.reply_to_message_id)
        )
    message += MessageSegment.markdown(reply.body)
    rows = [
        InlineKeyboardRow(
            buttons=[
                Button(
                    render_data=RenderData(label=button.label),
                    action=Action(type=2, data=button.command),
                )
                for button in row
            ]
        )
        for row in reply.keyboard
        if row
    ]
    if rows:
        message += MessageSegment.keyboard(
            MessageKeyboard(content=InlineKeyboard(rows=rows))
        )
    return message


@bind_qq.handle()
async def handle_bind_qq(
    event: GroupAtMessageCreateEvent,
    state: T_State,
) -> None:
    """消费 state 中的 274 token，经向导渲染后只发送一次。"""
    if type(event) is not GroupAtMessageCreateEvent:
        return
    content = event.content if isinstance(event.content, str) else ""
    if not content.startswith("/bind"):
        return
    token = get_qq_admission_token(state)
    if token is None:
        return
    wizard = get_binding_wizard()
    if wizard is None:
        return
    reply = await wizard.handle_event(event, token)
    if reply is None:
        return
    if not await wizard.authorize_send(token, reply):
        return
    try:
        await bind_qq.send(_qq_message(reply))
    except Exception as error:
        logger.warning(
            "[CharacterBinding] QQ 绑定回复发送结果不确定，不补发: error_type={}",
            type(error).__name__,
        )


__all__ = ["bind_qq"]
