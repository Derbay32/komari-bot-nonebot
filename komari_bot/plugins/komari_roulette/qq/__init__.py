"""QQ adapter seams for the Russian roulette plugin (TSK-278).

Importing this subpackage registers the single QQ group-@ entry matcher.  The
matcher owns no policy: it delegates to a runtime installed by the application
composition root (TSK-279, which owns the roulette configuration and
lifecycle), and does nothing at all while that runtime is absent.  Roulette
``plugin_enable`` defaults to ``false``, so failing closed is the contract
default rather than a degraded mode.

Installation requires explicit ``business_gate``, ``runtime_check`` and
``send_gate`` callables on purpose: this plugin never fabricates an always-true
authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from nonebot import on_message
from nonebot.adapters.qq import Bot as QQBot  # noqa: TC002 - NoneBot DI 注解
from nonebot.adapters.qq.event import (
    GroupAtMessageCreateEvent,  # noqa: TC002 - NoneBot DI 注解
)
from nonebot.typing import T_State  # noqa: TC002 - NoneBot DI 注解

from .delivery import DeliveryOutcome, RouletteDelivery, SendNotAcceptedError
from .handler import BusinessGate, RouletteQQHandler
from .keyboard import build_keyboard, keyboard_from_spec
from .parser import parse_command
from .renderer import render_reply

if TYPE_CHECKING:
    from ..command_service import RouletteCommandService
    from .delivery import PayloadBuilder, RuntimeCheck
    from .handler import SendGate


@dataclass(frozen=True, slots=True)
class RouletteQQRuntime:
    """The installed QQ adapter runtime the matcher delegates to."""

    handler: RouletteQQHandler


class _QQState:
    """Process-local runtime holder; a new runtime atomically replaces it."""

    __slots__ = ("runtime",)

    def __init__(self) -> None:
        self.runtime: RouletteQQRuntime | None = None


_state = _QQState()


def install_roulette_qq_runtime(
    *,
    service: RouletteCommandService,
    business_gate: BusinessGate,
    runtime_check: RuntimeCheck,
    send_gate: SendGate,
    payload_builder: PayloadBuilder | None = None,
) -> RouletteQQRuntime:
    """Install the QQ adapter runtime from explicit, non-optional authority.

    ``business_gate`` runs immediately before the single domain execution (the
    live plugin switch + group-admission recheck), ``send_gate`` runs before the
    atomic claim and receives *this call's* ``CommandRequest``, and
    ``runtime_check`` runs after the claim and immediately before the platform
    send and receives *this receipt's* ``CommandReceipt``.  All three are
    required keyword arguments so no caller can wire an always-true gate by
    omission, and none of them may read a process-global "current event".
    """

    delivery = RouletteDelivery(
        service,
        runtime_check=runtime_check,
        payload_builder=payload_builder,
    )
    runtime = RouletteQQRuntime(
        handler=RouletteQQHandler(
            service,
            delivery,
            business_gate=business_gate,
            send_gate=send_gate,
        )
    )
    _state.runtime = runtime
    return runtime


def clear_roulette_qq_runtime() -> None:
    """Stop distributing new roulette commands (shutdown or disable)."""

    _state.runtime = None


def get_roulette_qq_runtime() -> RouletteQQRuntime | None:
    """Return the installed runtime; ``None`` means the matcher fails closed."""

    return _state.runtime


roulette_qq = on_message(priority=2, block=False)


@roulette_qq.handle()
async def handle_roulette_qq(
    bot: QQBot,
    event: GroupAtMessageCreateEvent,
    state: T_State,
) -> None:
    """Delegate one admitted QQ group-@ command; otherwise do nothing."""

    runtime = _state.runtime
    if runtime is None:
        return
    await runtime.handler.handle(bot, event, state=state)


__all__ = [
    "BusinessGate",
    "DeliveryOutcome",
    "RouletteDelivery",
    "RouletteQQHandler",
    "RouletteQQRuntime",
    "SendNotAcceptedError",
    "build_keyboard",
    "clear_roulette_qq_runtime",
    "get_roulette_qq_runtime",
    "handle_roulette_qq",
    "install_roulette_qq_runtime",
    "keyboard_from_spec",
    "parse_command",
    "render_reply",
    "roulette_qq",
]
