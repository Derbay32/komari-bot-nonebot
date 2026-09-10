"""TSK-276 one-to-one post-commit fulfillment claim tests."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import text

from komari_bot.plugins.character_binding.manager import CharacterBindingManager
from komari_bot.plugins.komari_roulette import (
    CommandReceipt,
    FulfillmentConflictError,
    FulfillmentState,
    RouletteCommandService,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from .command_support import (
    PG_REQUIRED,
    Scope,
    backend_pid,
    create_engine_and_factory,
    delete_scope,
    reset_shared_orm_engine,
    scope,
    seed_binding,
    wait_for_blocked,
)
from .test_command_service import CountingProjector, create_waiting

pytestmark = [pytest.mark.asyncio, PG_REQUIRED]


@dataclass(frozen=True, slots=True)
class Harness:
    engine: AsyncEngine
    session_factory: async_sessionmaker[AsyncSession]
    binding_manager: CharacterBindingManager


@pytest.fixture
async def harness() -> AsyncIterator[Harness]:
    async for engine, session_factory in create_engine_and_factory():
        await reset_shared_orm_engine()
        manager = CharacterBindingManager()
        await manager.initialize()
        current = scope("fulfillment-fixture")
        try:
            yield Harness(engine, session_factory, manager)
        finally:
            with suppress(Exception):
                await manager.close()
            await reset_shared_orm_engine()
            await delete_scope(engine, current)


async def receipt_for(
    harness: Harness,
    current: Scope,
) -> CommandReceipt:
    await seed_binding(harness.binding_manager, current, 1)
    service = RouletteCommandService(
        session_factory=harness.session_factory,
        reply_projector=CountingProjector(),
    )
    return await create_waiting(service, current)


async def fulfillment_row(
    session_factory: async_sessionmaker[AsyncSession],
    receipt_id: str,
) -> dict[str, object]:
    async with session_factory() as session:
        row = (
            await session.execute(
                text(
                    "SELECT state, platform_message_id "
                    "FROM komari_roulette_fulfillments "
                    "WHERE receipt_id = :receipt_id"
                ),
                {"receipt_id": receipt_id},
            )
        ).mappings().one()
        return dict(row)


async def test_only_one_worker_claims_one_receipt(
    harness: Harness,
) -> None:
    current = scope("claim-race")
    receipt = await receipt_for(harness, current)
    service = RouletteCommandService(
        session_factory=harness.session_factory,
        reply_projector=CountingProjector(),
    )
    receipt_id = receipt.receipt_id
    async with harness.session_factory() as blocker:
        await blocker.begin()
        blocker_pid = await backend_pid(blocker)
        await blocker.execute(
            text(
                "SELECT receipt_id FROM komari_roulette_fulfillments "
                "WHERE receipt_id = :receipt_id FOR UPDATE"
            ),
            {"receipt_id": receipt_id},
        )
        started = [asyncio.Event(), asyncio.Event()]

        async def run_claim(index: int):
            started[index].set()
            return await service.claim_fulfillment(receipt_id)

        tasks = [asyncio.create_task(run_claim(index)) for index in range(2)]
        for event in started:
            await event.wait()
        await wait_for_blocked(harness.session_factory, blocker_pid)
        await blocker.commit()
        claims = await asyncio.gather(*tasks)
    assert sum(claim is not None for claim in claims) == 1
    claim = next(claim for claim in claims if claim is not None)
    assert getattr(claim, "state", None) == FulfillmentState.PENDING_CONFIRMATION
    row = await fulfillment_row(harness.session_factory, receipt_id)
    assert row["state"] == "PENDING_CONFIRMATION"
    await delete_scope(harness.engine, current)


async def test_delivery_updates_only_fulfillment_and_cannot_be_reclaimed(
    harness: Harness,
) -> None:
    current = scope("claim-delivery")
    receipt = await receipt_for(harness, current)
    service = RouletteCommandService(
        session_factory=harness.session_factory,
        reply_projector=CountingProjector(),
    )
    receipt_id = receipt.receipt_id
    before = await service.observe_current(current.group)
    claim = await service.claim_fulfillment(receipt_id)
    assert claim is not None
    await service.mark_delivered(claim, platform_message_id="qq-msg-1")
    await service.mark_delivered(claim, platform_message_id="qq-msg-1")
    with pytest.raises(FulfillmentConflictError):
        await service.mark_delivered(claim, platform_message_id="qq-msg-2")
    replay_claim = await service.claim_fulfillment(receipt_id)
    assert replay_claim is None
    after = await service.observe_current(current.group)
    assert getattr(after, "state_revision", None) == getattr(
        before, "state_revision", None
    )
    assert getattr(after, "turn_seq", None) == getattr(before, "turn_seq", None)
    row = await fulfillment_row(harness.session_factory, receipt_id)
    assert row == {
        "state": "DELIVERED",
        "platform_message_id": "qq-msg-1",
    }
    await delete_scope(harness.engine, current)


async def test_pre_send_known_failure_is_not_delivered_and_unknown_stays_pending(
    harness: Harness,
) -> None:
    current = scope("claim-outcomes")
    receipt = await receipt_for(harness, current)
    service = RouletteCommandService(
        session_factory=harness.session_factory,
        reply_projector=CountingProjector(),
    )
    receipt_id = receipt.receipt_id
    known_failure = await service.claim_fulfillment(receipt_id)
    assert known_failure is not None
    await service.mark_not_delivered(known_failure)
    assert (await fulfillment_row(harness.session_factory, receipt_id))["state"] == (
        "NOT_DELIVERED"
    )
    assert await service.claim_fulfillment(receipt_id) is None

    second_current = scope("claim-unknown")
    second = await receipt_for(harness, second_current)
    second_id = second.receipt_id
    uncertain = await service.claim_fulfillment(second_id)
    assert uncertain is not None
    # No mark call represents a sender that started and lost its response.  The
    # durable claim remains PENDING_CONFIRMATION and cannot be reclaimed.
    assert (await fulfillment_row(harness.session_factory, second_id))["state"] == (
        "PENDING_CONFIRMATION"
    )
    assert await service.claim_fulfillment(second_id) is None
    await delete_scope(harness.engine, current)
    await delete_scope(harness.engine, second_current)


async def test_invalid_credential_age_converges_to_not_delivered(
    harness: Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """不可用 age 不得被误判为窗口内：claim 原子收敛 NOT_DELIVERED（fail-closed）。

    这是 claim 边界的行为断言，不锁定 ``_age_seconds`` 的具体 isinstance 编码。
    """
    current = scope("claim-invalid-age")
    receipt = await receipt_for(harness, current)
    service = RouletteCommandService(
        session_factory=harness.session_factory,
        reply_projector=CountingProjector(),
    )
    receipt_id = receipt.receipt_id

    async def invalid_window_row(
        _session: AsyncSession, receipt_id_arg: str
    ) -> dict[str, object]:
        return {
            "receipt_id": receipt_id_arg,
            "state": FulfillmentState.NOT_STARTED.value,
            "age_seconds": "not-a-number",
        }

    monkeypatch.setattr(
        RouletteCommandService,
        "_fulfillment_window_row",
        staticmethod(invalid_window_row),
    )

    claim = await service.claim_fulfillment(receipt_id)
    assert claim is not None
    assert claim.state is FulfillmentState.NOT_DELIVERED
    row = await fulfillment_row(harness.session_factory, receipt_id)
    assert row["state"] == "NOT_DELIVERED"
    await delete_scope(harness.engine, current)
