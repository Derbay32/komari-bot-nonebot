"""One-shot QQ delivery seam for the Russian roulette plugin (TSK-278).

``RouletteDelivery`` consumes an already committed ``CommandReceipt``: it
rebuilds the frozen payload, atomically claims the single platform send
attempt, re-reads the live send authority, sends at most once, and records the
outcome.  It never re-reads game state, never re-renders, never retries, and
never rolls the domain back after a send has started (TSK-267).
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol, cast

from nonebot import logger
from nonebot.adapters.qq.message import Message, MessageSegment

from ..command_service import FulfillmentState
from .keyboard import keyboard_from_spec

if TYPE_CHECKING:
    from nonebot.adapters.qq import Bot as QQBot

    from ..command_service import CommandReceipt, FulfillmentClaim

type RuntimeCheck = Callable[[], bool | Awaitable[bool]]
type PayloadBuilder = Callable[["CommandReceipt"], Any]


class SendNotAcceptedError(RuntimeError):
    """The sender explicitly refused the message before any network call."""


class DeliveryOutcome(StrEnum):
    """Result of one delivery attempt."""

    DELIVERED = "delivered"
    NOT_DELIVERED = "not_delivered"
    UNKNOWN = "unknown"
    NO_CLAIM = "no_claim"


class _RouletteService(Protocol):
    """The narrow fulfillment surface the delivery consumes (TSK-276)."""

    async def claim_fulfillment(self, receipt_id: str) -> FulfillmentClaim | None: ...

    async def mark_delivered(
        self,
        claim: FulfillmentClaim,
        *,
        platform_message_id: str,
    ) -> None: ...

    async def mark_not_delivered(self, claim: FulfillmentClaim) -> None: ...


class QQMessageSender(Protocol):
    """Platform sender surface; real QQ ``Bot`` and test fakes both satisfy it."""

    async def send_to_group(
        self,
        group_openid: str,
        message: Message,
        *,
        msg_id: str | None = None,
        msg_seq: int | None = None,
    ) -> object: ...


def build_qq_message(receipt: CommandReceipt) -> Message:
    """Rebuild the frozen Markdown body and keyboard as a real QQ ``Message``.

    Pure projection of the committed receipt: no service read, no re-render.
    A malformed frozen keyboard spec is a build failure and never a retrofit.
    """

    spec = receipt.reply.metadata.get("keyboard")
    if not isinstance(spec, str):
        raise TypeError("frozen keyboard spec is missing")  # noqa: TRY003
    message = Message()
    message += MessageSegment.markdown(receipt.reply.body)
    keyboard = keyboard_from_spec(spec)
    # TSK-266 1F: no buttons means no keyboard field at all.  An empty ``rows``
    # list must not become an empty ``keyboard`` segment on the wire.
    if keyboard.content is not None and keyboard.content.rows:
        message += MessageSegment.keyboard(keyboard)
    return message


def _platform_message_id(response: object) -> str | None:
    """Extract the platform message id, or ``None`` when it is unusable.

    Real ``PostGroupMessagesReturn.id`` is ``str | None`` and test fakes may
    return a bare id string; anything else (``None``, a dict, an object with
    no ``id``) cannot confirm delivery and must never be stringified.
    """

    value = getattr(response, "id", response)
    if isinstance(value, str) and value.strip():
        return value
    return None


class RouletteDelivery:
    """Own the single allowed QQ send attempt for one committed receipt."""

    def __init__(
        self,
        service: _RouletteService,
        *,
        runtime_check: RuntimeCheck | None = None,
        payload_builder: PayloadBuilder | None = None,
    ) -> None:
        self._service = service
        self._runtime_check = runtime_check
        self._payload_builder: PayloadBuilder = payload_builder or build_qq_message

    async def deliver(  # noqa: PLR0911 — 每个结果分支都是一个真实结局
        self,
        receipt: CommandReceipt,
        sender: QQMessageSender | QQBot,
    ) -> DeliveryOutcome:
        """Build frozen payload → claim → recheck → send once → mark."""

        message, build_error = self._build(receipt)
        claim = await self._service.claim_fulfillment(receipt.receipt_id)
        if claim is None:
            return DeliveryOutcome.NO_CLAIM
        if claim.state is not FulfillmentState.PENDING_CONFIRMATION:
            # The claim is the only send authorization: a receipt whose
            # credential window expired was already converged atomically to
            # NOT_DELIVERED inside the claim and must never reach the network.
            return DeliveryOutcome.NOT_DELIVERED
        if build_error is not None:
            logger.warning(
                "[Roulette] 冻结载荷构建失败，本次不发送: error_type={}",
                type(build_error).__name__,
            )
            await self._service.mark_not_delivered(claim)
            return DeliveryOutcome.NOT_DELIVERED
        if not await self._send_allowed(claim):
            return DeliveryOutcome.NOT_DELIVERED
        try:
            response = await sender.send_to_group(
                receipt.group_openid,
                message,
                msg_id=receipt.inbound_msg_id,
                msg_seq=1,
            )
        except SendNotAcceptedError:
            await self._service.mark_not_delivered(claim)
            return DeliveryOutcome.NOT_DELIVERED
        except asyncio.CancelledError:
            # The send may or may not have happened: leave the claim pending
            # and let the platform idempotency key absorb a duplicate event.
            raise
        except Exception as error:
            logger.warning(
                "[Roulette] QQ 发送结果不确定，保持待确认: error_type={}",
                type(error).__name__,
            )
            return DeliveryOutcome.UNKNOWN
        try:
            platform_message_id = _platform_message_id(response)
            if platform_message_id is None:
                # TSK-278 6: the send happened but the platform gave no usable
                # id; keep PENDING_CONFIRMATION and never fake an id.
                logger.warning("[Roulette] 平台回执缺少可用消息 ID，保持待确认")
                return DeliveryOutcome.UNKNOWN
            await self._service.mark_delivered(
                claim,
                platform_message_id=platform_message_id,
            )
        except Exception as error:
            logger.warning(
                "[Roulette] 履约确认写入失败，发送已发生且不重发: error_type={}",
                type(error).__name__,
            )
            return DeliveryOutcome.UNKNOWN
        return DeliveryOutcome.DELIVERED

    def _build(self, receipt: CommandReceipt) -> tuple[Any, Exception | None]:
        try:
            return self._payload_builder(receipt), None
        except Exception as error:  # 预发送阶段的显式失败：绝不发送、绝不重试
            return None, error

    async def _send_allowed(self, claim: FulfillmentClaim) -> bool:
        """Live pre-send recheck; a failing check fails closed, never hangs.

        The recheck runs after the claim and before any network call.  A check
        that raises must not bubble out and leave the claim stuck in
        ``PENDING_CONFIRMATION``: it is recorded as ``NOT_DELIVERED`` instead.
        ``asyncio.CancelledError`` is a cancellation, not a rejected check.
        """

        check = self._runtime_check
        if check is None:
            return True
        try:
            result = check()
            if inspect.isawaitable(result):
                result = await cast("Awaitable[bool]", result)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.warning(
                "[Roulette] 发送前运行时重核失败，故障关闭: error_type={}",
                type(error).__name__,
            )
            await self._service.mark_not_delivered(claim)
            return False
        if not bool(result):
            await self._service.mark_not_delivered(claim)
            return False
        return True


__all__ = [
    "DeliveryOutcome",
    "QQMessageSender",
    "RouletteDelivery",
    "SendNotAcceptedError",
    "build_qq_message",
]
