"""OneBot 回复发送能力窄边界。

本模块只做三件事：组装富文本 / 纯文本消息、在明确失败后执行纯文本
降级、把平台结果翻译成领域送达事实（已送达 / 未送达 / 待确认）。
平台异常细节（``ActionFailed``、``NetworkError``、超时等）只在本
边界内翻译，绝不进入领域状态机；发送统一使用
``bot.call_api("send_group_msg", ...)``，不依赖 matcher 的隐式上下文。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol

from nonebot import logger
from nonebot.adapters.onebot.v11 import ActionFailed, MessageSegment
from nonebot.exception import NetworkError

if TYPE_CHECKING:
    from nonebot.internal.adapter import Bot


@dataclass(frozen=True)
class ReplyDeliveryResult:
    """平台发送结果翻译后的领域送达事实。

    三个互斥状态：``delivered``（已送达，平台消息 ID 可缺失）、
    ``not_delivered``（明确未送达）、``pending_confirmation``（结果
    未知，等待对账）。平台异常细节不进入本类型。
    """

    state: Literal["delivered", "not_delivered", "pending_confirmation"]
    platform_message_id: str | None = None

    @staticmethod
    def delivered(platform_message_id: object | None = None) -> "ReplyDeliveryResult":
        """已送达；平台消息 ID 缺失也允许。"""
        message_id = (
            str(platform_message_id) if platform_message_id is not None else None
        )
        return ReplyDeliveryResult(state="delivered", platform_message_id=message_id)

    @staticmethod
    def not_delivered() -> "ReplyDeliveryResult":
        """平台明确拒绝，未送达。"""
        return ReplyDeliveryResult(state="not_delivered")

    @staticmethod
    def pending_confirmation() -> "ReplyDeliveryResult":
        """结果未知（超时 / 网络错误 / 响应丢失），等待对账。"""
        return ReplyDeliveryResult(state="pending_confirmation")


class DeliveryRequest(Protocol):
    """发送边界消费的回复载荷最小接口。"""

    @property
    def group_id(self) -> str | int: ...

    @property
    def reply(self) -> str: ...

    @property
    def reply_to_message_id(self) -> str | None: ...


class OneBotReplySender:
    """OneBot 平台的回复发送能力边界。

    富文本（引用 + 文本）发送；明确失败（``ActionFailed``）后降级为
    纯文本重试；两次明确失败按未送达翻译。超时、``NetworkError`` 等
    不确定错误只进入待确认，不重试。``CancelledError`` 原样传播。
    """

    def __init__(self, bot: Bot) -> None:
        self.bot = bot

    @staticmethod
    def _rich_message(request: DeliveryRequest) -> list[dict[str, object]]:
        """富文本消息：OneBot 段数组（引用 + 文本）。"""
        segments: list[dict[str, object]] = []
        if request.reply_to_message_id:
            segments.append(
                {"type": "reply", "data": {"id": request.reply_to_message_id}}
            )
        segments.append({"type": "text", "data": {"text": request.reply}})
        return segments

    @staticmethod
    def _plain_message(request: DeliveryRequest) -> list[MessageSegment]:
        """纯文本消息：单一文本段，禁止再次解析 CQ 码。"""
        return [MessageSegment.text(request.reply)]

    @staticmethod
    def _platform_message_id(response: object) -> object | None:
        """从发送响应中提取平台消息 ID；缺失返回 None。"""
        if response is None:
            return None
        if isinstance(response, dict):
            message_id = response.get("message_id")
            if message_id is None and isinstance(response.get("data"), dict):
                message_id = response["data"].get("message_id")
            return message_id
        return getattr(response, "message_id", None)

    async def __call__(self, request: DeliveryRequest) -> ReplyDeliveryResult:
        """发送一次回复并翻译平台结果。

        消息组装在调用平台前完成，组装 / 转换等调用前编程错误直接
        向上传播，不被翻译为送达事实。平台边界只捕获可判定的
        明确失败（``ActionFailed``）与不确定结果（超时 /
        ``NetworkError``）；``call_api`` 抛出的其他未知异常发生在发送
        开始之后、无法判定是否已发出，保守翻译为待确认。
        """
        group_id = int(request.group_id)
        rich_message = self._rich_message(request)
        try:
            response = await self.bot.call_api(
                "send_group_msg",
                group_id=group_id,
                message=rich_message,
            )
        except asyncio.CancelledError:
            raise
        except ActionFailed as error:
            logger.warning(
                "[KomariChat] 富文本发送被平台明确拒绝: {}，降级纯文本",
                error,
            )
            plain_message = self._plain_message(request)
            try:
                response = await self.bot.call_api(
                    "send_group_msg",
                    group_id=group_id,
                    message=plain_message,
                )
            except asyncio.CancelledError:
                raise
            except ActionFailed:
                # 两次明确失败：未送达
                return ReplyDeliveryResult.not_delivered()
            except (TimeoutError, NetworkError):
                return ReplyDeliveryResult.pending_confirmation()
            except Exception:
                # 降级发送抛出未知异常：无法判定是否已发出，保守待确认
                return ReplyDeliveryResult.pending_confirmation()
        except (TimeoutError, NetworkError):
            return ReplyDeliveryResult.pending_confirmation()
        except Exception:
            # call_api 抛出未知异常：发送开始后无法判定是否已发出，
            # 保守待确认，交由对账决定送达事实
            return ReplyDeliveryResult.pending_confirmation()
        return ReplyDeliveryResult.delivered(self._platform_message_id(response))


__all__ = ["DeliveryRequest", "OneBotReplySender", "ReplyDeliveryResult"]
