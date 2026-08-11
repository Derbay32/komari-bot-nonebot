"""OneBot 回复发送能力的结果翻译验收测试。"""

from __future__ import annotations

import asyncio
from importlib import import_module, util
from types import SimpleNamespace
from typing import Any

import pytest
from nonebot.adapters.onebot.v11 import ActionFailed
from nonebot.exception import NetworkError


class _Bot:
    def __init__(self, outcomes: list[object]) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[dict[str, object]] = []

    async def call_api(self, api: str, **kwargs: object) -> object:
        self.calls.append({"api": api, **kwargs})
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def _onebot_module() -> Any:
    module_name = "komari_bot.plugins.komari_chat.services.reply_delivery_onebot"
    assert util.find_spec(module_name) is not None, "OneBot 发送边界尚未建立"
    return import_module(module_name)


def _request(*, reply_to_message_id: str | None = "message-1") -> SimpleNamespace:
    return SimpleNamespace(
        group_id="114514",
        reply="回复正文",
        reply_to_message_id=reply_to_message_id,
    )


def _action_failed(message: str = "平台明确拒绝") -> ActionFailed:
    return ActionFailed(
        status="failed",
        retcode=100,
        data=None,
        message=message,
    )


async def test_rich_reply_success_is_delivered_with_platform_message_id() -> None:
    module = _onebot_module()
    bot = _Bot([{"message_id": 7788}])

    result = await module.OneBotReplySender(bot)(_request())

    assert result == module.ReplyDeliveryResult.delivered("7788")
    assert bot.calls == [
        {
            "api": "send_group_msg",
            "group_id": 114514,
            "message": [
                {"type": "reply", "data": {"id": "message-1"}},
                {"type": "text", "data": {"text": "回复正文"}},
            ],
        }
    ]


async def test_explicit_rich_failure_falls_back_to_plain_text() -> None:
    module = _onebot_module()
    bot = _Bot([_action_failed(), {"message_id": "plain-1"}])

    result = await module.OneBotReplySender(bot)(_request())

    assert result == module.ReplyDeliveryResult.delivered("plain-1")
    assert len(bot.calls) == 2
    assert bot.calls[1]["api"] == "send_group_msg"
    assert bot.calls[1]["group_id"] == 114514
    plain_message = bot.calls[1]["message"]
    assert len(plain_message) == 1
    assert plain_message[0].type == "text"
    assert plain_message[0].data == {"text": "回复正文"}


async def test_two_explicit_failures_are_not_delivered() -> None:
    module = _onebot_module()
    bot = _Bot([_action_failed("富文本失败"), _action_failed("纯文本失败")])

    result = await module.OneBotReplySender(bot)(_request())

    assert result == module.ReplyDeliveryResult.not_delivered()
    assert len(bot.calls) == 2


@pytest.mark.parametrize(
    "error",
    [
        TimeoutError(),
        NetworkError("OneBot V11", "网络中断"),
    ],
)
async def test_uncertain_platform_errors_are_pending_confirmation(
    error: Exception,
) -> None:
    module = _onebot_module()
    bot = _Bot([error])

    result = await module.OneBotReplySender(bot)(_request())

    assert result == module.ReplyDeliveryResult.pending_confirmation()
    assert len(bot.calls) == 1


async def test_success_without_platform_message_id_is_still_delivered() -> None:
    module = _onebot_module()
    bot = _Bot([{}])

    result = await module.OneBotReplySender(bot)(
        _request(reply_to_message_id=None)
    )

    assert result == module.ReplyDeliveryResult.delivered()


async def test_cancelled_error_propagates() -> None:
    module = _onebot_module()
    bot = _Bot([asyncio.CancelledError()])

    with pytest.raises(asyncio.CancelledError):
        await module.OneBotReplySender(bot)(_request())
