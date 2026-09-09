"""TSK-278 RED baseline: delivery against the real fulfillment store.

Gated on ``KOMARI_TEST_POSTGRES_URL`` exactly like the existing TSK-276
tests.  It exercises ``RouletteDelivery`` over a real ``RouletteCommandService``
(real claim/mark rows) with a replaceable ``FakeSender``.

RED: ``RouletteDelivery`` / ``DeliveryOutcome`` are imported inside each test
so this file still skips cleanly without PostgreSQL and reports the same
missing-seam error when the gate is enabled.
"""

from __future__ import annotations

from contextlib import suppress
from typing import TYPE_CHECKING

import pytest

from komari_bot.plugins.character_binding.manager import CharacterBindingManager
from komari_bot.plugins.komari_roulette import RouletteCommandService

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from .command_support import (
    PG_REQUIRED,
    create_engine_and_factory,
    delete_scope,
    reset_shared_orm_engine,
    scope,
    seed_binding,
)
from .test_command_service import CountingProjector, create_waiting
from .tsk278_support import FakeSender

pytestmark = [pytest.mark.asyncio, PG_REQUIRED]


@pytest.fixture
async def harness() -> AsyncIterator[
    tuple[AsyncEngine, async_sessionmaker[AsyncSession], CharacterBindingManager]
]:
    async for engine, session_factory in create_engine_and_factory():
        await reset_shared_orm_engine()
        manager = CharacterBindingManager()
        await manager.initialize()
        current = scope("tsk278-delivery")
        try:
            yield engine, session_factory, manager
        finally:
            with suppress(Exception):
                await manager.close()
            await reset_shared_orm_engine()
            await delete_scope(engine, current)


async def test_real_delivery_success_marks_delivered(
    harness: tuple[
        AsyncEngine,
        async_sessionmaker[AsyncSession],
        CharacterBindingManager,
    ],
) -> None:
    from komari_bot.plugins.komari_roulette import (
        DeliveryOutcome,
        RouletteDelivery,
    )

    _engine, session_factory, manager = harness
    current = scope("tsk278-delivery-ok")
    await seed_binding(manager, current, 1)
    service = RouletteCommandService(
        session_factory=session_factory,
        reply_projector=CountingProjector(),
    )
    real_receipt = await create_waiting(service, current)

    sender = FakeSender(result="qq-platform-real-1")
    outcome = await RouletteDelivery(service=service).deliver(real_receipt, sender)

    assert outcome is DeliveryOutcome.DELIVERED
    assert len(sender.network_calls) == 1
    payload = sender.network_calls[0]
    assert payload["group_openid"] == current.group_openid
    assert payload["msg_id"] == real_receipt.inbound_msg_id
    assert payload["msg_seq"] == 1
    assert "message_reference" not in payload

    # 真实履约行已标记 DELIVERED 并记录平台消息 ID。
    from sqlalchemy import text

    async with session_factory() as session:
        row = (
            await session.execute(
                text(
                    "SELECT state, platform_message_id "
                    "FROM komari_roulette_fulfillments "
                    "WHERE receipt_id = :receipt_id"
                ),
                {"receipt_id": real_receipt.receipt_id},
            )
        ).mappings().first()
    assert row is not None
    assert row["state"] == "DELIVERED"
    assert row["platform_message_id"] == "qq-platform-real-1"


async def test_real_delivery_duplicate_event_single_network_call(
    harness: tuple[
        AsyncEngine,
        async_sessionmaker[AsyncSession],
        CharacterBindingManager,
    ],
) -> None:
    from komari_bot.plugins.komari_roulette import (
        DeliveryOutcome,
        RouletteDelivery,
    )

    _engine, session_factory, manager = harness
    current = scope("tsk278-delivery-dup")
    await seed_binding(manager, current, 1)
    service = RouletteCommandService(
        session_factory=session_factory,
        reply_projector=CountingProjector(),
    )
    real_receipt = await create_waiting(service, current)

    sender = FakeSender(result="qq-platform-real-2")
    delivery = RouletteDelivery(service=service)

    first = await delivery.deliver(real_receipt, sender)
    assert first is DeliveryOutcome.DELIVERED
    second = await delivery.deliver(real_receipt, sender)
    assert second is DeliveryOutcome.NO_CLAIM

    assert len(sender.network_calls) == 1
    assert len(sender.calls) == 1


async def test_real_delivery_explicit_failure_marks_not_delivered(
    harness: tuple[
        AsyncEngine,
        async_sessionmaker[AsyncSession],
        CharacterBindingManager,
    ],
) -> None:
    from komari_bot.plugins.komari_roulette import (
        DeliveryOutcome,
        RouletteDelivery,
        SendNotAcceptedError,
    )

    _engine, session_factory, manager = harness
    current = scope("tsk278-delivery-fail")
    await seed_binding(manager, current, 1)
    service = RouletteCommandService(
        session_factory=session_factory,
        reply_projector=CountingProjector(),
    )
    real_receipt = await create_waiting(service, current)

    sender = FakeSender(mode="fail_before_send", exc=SendNotAcceptedError("blocked"))
    outcome = await RouletteDelivery(service=service).deliver(real_receipt, sender)

    assert outcome is DeliveryOutcome.NOT_DELIVERED
    assert sender.network_calls == []

    from sqlalchemy import text

    async with session_factory() as session:
        state = await session.scalar(
            text(
                "SELECT state FROM komari_roulette_fulfillments "
                "WHERE receipt_id = :receipt_id"
            ),
            {"receipt_id": real_receipt.receipt_id},
        )
    assert state == "NOT_DELIVERED"
