"""Strict QQ group-@ command handler seam for the roulette plugin (TSK-278).

``RouletteQQHandler`` performs the ingress orchestration only: strict event
eligibility, admission-token handoff, pure parse, optional observation
pre-read for active writes, exactly one domain execution, the send gate, and
at most one delivery.  It never executes SQL, writes domain state, or touches
randomness; every collaborator is injected by the caller.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Mapping
from typing import TYPE_CHECKING, Any, Protocol, cast

from nonebot import logger
from nonebot.adapters.qq import Bot as QQBot  # noqa: TC002 - NoneBot DI 与发送器
from nonebot.adapters.qq.event import GroupAtMessageCreateEvent

from komari_bot.plugins.group_admission import get_qq_admission_token

from ..command_service import OBSERVED_ACTIVE_WRITES, CommandRequest
from .parser import parse_command

if TYPE_CHECKING:
    from ..command_service import (
        CommandReceipt,
        GroupRef,
        Observation,
    )
    from .delivery import QQMessageSender

type SendGate = Callable[[], bool | Awaitable[bool]]


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
        send_gate: SendGate | None = None,
    ) -> None:
        self._service = service
        self._delivery = delivery
        self._send_gate = send_gate

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
        if get_qq_admission_token(state) is None:
            return
        command = parse_command(event.get_message().extract_plain_text())
        if command is None:
            return
        request = CommandRequest(
            app_id=bot.self_id,
            group_openid=group_openid,
            inbound_msg_id=inbound_msg_id,
            member_openid=member_openid,
            command=command,
            target_mention_count=len(event.mentions or []),
        )
        observation: Observation | None = None
        if command.intent in OBSERVED_ACTIVE_WRITES:
            observation = await self._service.observe_current(request.group)
        receipt = await self._service.execute_group_command(
            request,
            observation=observation,
        )
        if not await self._send_allowed():
            return
        await self._delivery.deliver(receipt, bot)

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


__all__ = ["RouletteQQHandler"]
