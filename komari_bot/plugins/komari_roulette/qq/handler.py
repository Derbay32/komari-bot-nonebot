"""Strict QQ group-@ command handler seam for the roulette plugin (TSK-278).

``RouletteQQHandler`` performs the ingress orchestration only: strict event
eligibility, real admission-token scope + identity binding, pure parse, optional
observation pre-read for active writes, a mandatory business re-authorization
gate immediately before the single domain execution, the send gate, and at most
one delivery.  It never executes SQL, writes domain state, or touches
randomness; every collaborator is injected by the caller.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Mapping
from typing import TYPE_CHECKING, Any, Protocol, cast

from nonebot import logger
from nonebot.adapters.qq import Bot as QQBot
from nonebot.adapters.qq.event import GroupAtMessageCreateEvent

from komari_bot.plugins import group_admission

from ..command_service import OBSERVED_ACTIVE_WRITES, CommandRequest
from .parser import parse_command

if TYPE_CHECKING:
    from komari_bot.plugins.group_admission import QQAdmissionToken

    from ..command_service import (
        CommandReceipt,
        GroupRef,
        Observation,
    )
    from .delivery import QQMessageSender

type SendGate = Callable[[], bool | Awaitable[bool]]
type BusinessGate = Callable[
    [QQBot, GroupAtMessageCreateEvent, "QQAdmissionToken"],
    bool | Awaitable[bool],
]

_MENTION_SEGMENT_TYPES = frozenset({"mention_user", "mention_everyone"})


class _CommandService(Protocol):
    """The narrow TSK-276 command surface the handler orchestrates."""

    async def observe_current(self, group: GroupRef) -> Observation | None: ...

    async def execute_group_command(
        self,
        request: CommandRequest,
        *,
        observation: Observation | None = None,
    ) -> CommandReceipt: ...


class _Delivery(Protocol):
    """The narrow delivery surface the handler invokes at most once."""

    async def deliver(
        self,
        receipt: CommandReceipt,
        sender: QQMessageSender,
    ) -> object: ...


class RouletteQQHandler:
    """Orchestrate one QQ group-@ command without owning any side effect."""

    def __init__(
        self,
        service: _CommandService,
        delivery: _Delivery,
        *,
        business_gate: BusinessGate,
        send_gate: SendGate | None = None,
    ) -> None:
        self._service = service
        self._delivery = delivery
        self._business_gate = business_gate
        self._send_gate = send_gate

    @staticmethod
    def _resolve_token(state: Mapping[str, Any] | None) -> QQAdmissionToken | None:
        """Read the handoff token through the live authoritative helper.

        ``get_qq_admission_token`` is the single authority for the handoff
        token: when it reports no token the event is rejected, with no fallback
        to an import-time binding.
        """

        return group_admission.get_qq_admission_token(state)

    async def handle(
        self,
        bot: QQBot,
        event: object,
        *,
        state: Mapping[str, Any] | None = None,
    ) -> None:
        """Silently ignore anything that is not an admitted roulette command."""

        if type(event) is not GroupAtMessageCreateEvent:
            return
        group_openid = event.group_openid
        inbound_msg_id = event.id
        author = event.author
        member_openid = getattr(author, "member_openid", None)
        if not (
            isinstance(group_openid, str)
            and group_openid.strip()
            and isinstance(inbound_msg_id, str)
            and inbound_msg_id.strip()
            and isinstance(member_openid, str)
            and member_openid.strip()
        ):
            return
        # Resolve the real handoff token (see ``_resolve_token``); the live
        # helper is the single authority and ``None`` means reject.
        token = self._resolve_token(state)
        if token is None or not self._token_binds_to_event(
            token,
            bot=bot,
            group_openid=group_openid,
            member_openid=member_openid,
            inbound_msg_id=inbound_msg_id,
        ):
            return
        message = event.get_message()
        command = parse_command(message.extract_plain_text())
        if command is None:
            return
        request = CommandRequest(
            app_id=bot.self_id,
            group_openid=group_openid,
            inbound_msg_id=inbound_msg_id,
            member_openid=member_openid,
            command=command,
            target_mention_count=sum(
                1 for segment in message if segment.type in _MENTION_SEGMENT_TYPES
            ),
        )
        observation: Observation | None = None
        if command.intent in OBSERVED_ACTIVE_WRITES:
            observation = await self._service.observe_current(request.group)
        if not await self._business_allowed(bot, event, token):
            return
        receipt = await self._service.execute_group_command(
            request,
            observation=observation,
        )
        if not await self._send_allowed():
            return
        await self._delivery.deliver(receipt, bot)

    @staticmethod
    def _token_binds_to_event(
        token: QQAdmissionToken,
        *,
        bot: QQBot,
        group_openid: str,
        member_openid: str,
        inbound_msg_id: str,
    ) -> bool:
        """Require a business-scoped token whose identity matches the event.

        A binding / binding-challenge token (or any token minted for another
        app, group, member or message) must never authorize a business command.
        """

        return (
            token.scope == "business"
            and token.app_id == bot.self_id
            and token.group_openid == group_openid
            and token.member_openid == member_openid
            and token.qq_message_id == inbound_msg_id
        )

    async def _business_allowed(
        self,
        bot: QQBot,
        event: GroupAtMessageCreateEvent,
        token: QQAdmissionToken,
    ) -> bool:
        """Re-adjudicate business authority right before the domain write."""

        try:
            result = self._business_gate(bot, event, token)
            if inspect.isawaitable(result):
                result = await cast("Awaitable[bool]", result)
        except Exception as error:  # 准入查询失败即故障关闭：绝不写入、绝不发送
            logger.warning(
                "[Roulette] 业务重授权门失败，本次不执行: error_type={}",
                type(error).__name__,
            )
            return False
        return bool(result)

    async def _send_allowed(self) -> bool:
        gate = self._send_gate
        if gate is None:
            return True
        try:
            result = gate()
            if inspect.isawaitable(result):
                return bool(await cast("Awaitable[bool]", result))
            return bool(result)
        except Exception as error:  # 实时核查失败即故障关闭：绝不发送
            logger.warning(
                "[Roulette] 发送前实时核查失败，本次不发送: error_type={}",
                type(error).__name__,
            )
            return False


__all__ = ["BusinessGate", "RouletteQQHandler", "SendGate"]
