"""TSK-279 Stage-C1 RED: post-lock domain effect recheck + post-claim final authority.

AC4: ``execute_group_command`` gains a per-call ``effect_check`` (mirroring the
existing ``advance_expired`` seam).  A ``False`` answer settles as a dedicated
safe-rejection control flow that the handler silently absorbs - **no receipt,
no state mutation, no fulfillment** - and the gate is evaluated *after* the
group advisory lock is held.  Other business / storage errors must still
propagate.

AC5: the QQ handler builds one per-call closure capturing the *original*
``bot`` / ``event`` / ``token`` and passes it to both the service post-lock
check and the delivery's extra post-claim check.  A ``False`` answer blocks the
network call (``NOT_DELIVERED``); authority is never re-minted from the receipt
and never shared across concurrent groups.

Production does not accept the new keyword yet, so the cases fail with
``TypeError`` / failed business assertions (the expected RED buckets), never a
collection error.
"""

from __future__ import annotations

import asyncio
import inspect
from contextlib import suppress
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy import text

from .command_support import (
    PG_REQUIRED,
    backend_pid,
    command_factory,
    hold_group_lock,
    request,
    seed_binding,
    wait_for_blocked,
)
from .test_command_service import (
    CountingRandom,
    create_waiting,
    current_game_row,
    join_player,
    seed_players,
    service_for,
)
from .tsk278_support import (
    FakeQQBot,
    admission_state,
    business_token,
    make_group_at_event,
    projection,
    receipt,
)
from .tsk279_support import (
    Tsk279Harness,
    harness_fixture_body,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

pytestmark = [pytest.mark.asyncio]

EFFECT_REJECTION_NAME = "EffectCheckRejectedError"


async def _maybe_await(value: object) -> Any:
    if inspect.isawaitable(value):
        return await value  # type: ignore[misc]
    return value


@pytest.fixture
async def harness() -> AsyncIterator[Tsk279Harness]:
    async for current in harness_fixture_body():
        yield current


async def _receipt_count(
    harness: Tsk279Harness,
    current: Any,
    message_id: str,
) -> int:
    async with harness.session_factory() as session:
        return int(
            await session.scalar(
                text(
                    "SELECT count(*) FROM komari_roulette_command_receipts "
                    "WHERE app_id = :app_id AND group_openid = :group_openid "
                    "AND inbound_msg_id = :msg"
                ),
                {
                    "app_id": current.app_id,
                    "group_openid": current.group_openid,
                    "msg": message_id,
                },
            )
            or 0
        )


async def _game_state(harness: Tsk279Harness, current: Any) -> dict[str, Any]:
    async with harness.session_factory() as session:
        row = (
            (
                await session.execute(
                    text(
                        "SELECT game_id, lifecycle, state_revision, turn_seq, "
                        "chamber_revision, current_player_seq, "
                        "waiting_expires_at, turn_deadline_at "
                        "FROM komari_roulette_games "
                        "WHERE app_id = :app_id AND group_openid = :group_openid "
                        "ORDER BY created_at DESC LIMIT 1"
                    ),
                    {
                        "app_id": current.app_id,
                        "group_openid": current.group_openid,
                    },
                )
            )
            .mappings()
            .first()
        )
        receipts = int(
            await session.scalar(
                text(
                    "SELECT count(*) FROM komari_roulette_command_receipts "
                    "WHERE app_id = :app_id AND group_openid = :group_openid"
                ),
                {
                    "app_id": current.app_id,
                    "group_openid": current.group_openid,
                },
            )
            or 0
        )
        fulfillments = int(
            await session.scalar(
                text(
                    "SELECT count(*) FROM komari_roulette_fulfillments AS f "
                    "JOIN komari_roulette_command_receipts AS r "
                    "ON r.receipt_id = f.receipt_id "
                    "WHERE r.app_id = :app_id AND r.group_openid = :group_openid"
                ),
                {
                    "app_id": current.app_id,
                    "group_openid": current.group_openid,
                },
            )
            or 0
        )
    assert row is not None
    return {"game": dict(row), "receipts": receipts, "fulfillments": fulfillments}


# ---------------------------------------------------------------------------
# AC4: execute_group_command effect_check
# ---------------------------------------------------------------------------


def test_execute_group_command_exposes_effect_check_keyword() -> None:
    from komari_bot.plugins.komari_roulette.command_service import (
        RouletteCommandService,
    )

    signature = inspect.signature(RouletteCommandService.execute_group_command)
    assert "effect_check" in signature.parameters, (
        "execute_group_command must accept the per-call effect_check keyword "
        "(mirroring advance_expired)"
    )


@PG_REQUIRED
async def test_effect_check_rejection_leaves_zero_effects(
    harness: Tsk279Harness,
) -> None:
    service = service_for(harness, random_source=CountingRandom())
    async with harness.scope("effect-reject") as current:
        members = await seed_players(harness.binding_manager, current, 2)
        await create_waiting(service, current)
        await join_player(service, current, members[1], "effect-reject-join")
        before = await _game_state(harness, current)

        gate_calls: list[int] = []

        def gate() -> bool:
            gate_calls.append(1)
            return False

        with pytest.raises(Exception) as excinfo:
            await service.execute_group_command(
                request(
                    current,
                    "effect-reject-start",
                    command_factory("start"),
                    member_openid=members[0],
                ),
                effect_check=gate,
            )
        error = excinfo.value
        assert type(error).__name__ == EFFECT_REJECTION_NAME, (
            "a rejected effect_check must raise the dedicated safe-rejection "
            f"control flow, got {type(error).__name__}: {error}"
        )
        assert isinstance(error, RuntimeError)
        # The rejection is not a storage / state error masquerading as a guard.
        assert type(error).__name__ not in {
            "StorageUnavailableError",
            "StateConflictError",
            "CommitOutcomeUnknownError",
        }

        assert gate_calls == [1], "the gate must be called exactly once"
        # Zero receipt: the rejected command never became an error receipt.
        assert await _receipt_count(harness, current, "effect-reject-start") == 0
        after = await _game_state(harness, current)
        assert after == before, (
            "a rejected effect_check must not mutate the game, create a receipt "
            "or create a fulfillment"
        )


@PG_REQUIRED
async def test_effect_check_is_evaluated_only_after_the_group_lock(
    harness: Tsk279Harness,
) -> None:
    gate_calls = 0

    async def gate() -> bool:
        nonlocal gate_calls
        gate_calls += 1
        return True

    service = service_for(harness, random_source=CountingRandom())
    async with harness.scope("effect-lock") as current:
        members = await seed_players(harness.binding_manager, current, 2)
        await create_waiting(service, current)
        async with harness.session_factory() as blocker:
            await blocker.begin()
            blocker_pid = await backend_pid(blocker)
            await hold_group_lock(blocker, current)
            task = asyncio.create_task(
                service.execute_group_command(
                    request(
                        current,
                        "effect-lock-join",
                        command_factory("join"),
                        member_openid=members[1],
                    ),
                    effect_check=gate,
                )
            )
            try:
                await wait_for_blocked(harness.session_factory, blocker_pid)
                assert gate_calls == 0, (
                    "the effect_check must not run before the group lock is held"
                )
            finally:
                await blocker.commit()
            async with asyncio.timeout(10):
                with suppress(Exception):
                    await task
    assert gate_calls == 1, "the gate must run once the lock is released"


@PG_REQUIRED
async def test_effect_check_true_still_propagates_storage_errors(
    harness: Tsk279Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A true gate must not swallow the real storage failure that follows it."""

    from komari_bot.plugins.komari_roulette import (
        command_service as command_service_module,
    )
    from komari_bot.plugins.komari_roulette.command_service import (
        StorageUnavailableError,
    )

    gate_calls: list[int] = []

    def gate() -> bool:
        gate_calls.append(1)
        return True

    async def boom(self: object, group: object, *, for_update: bool = False) -> None:
        del self, group, for_update
        raise StorageUnavailableError("forced storage failure (C1 test)")  # noqa: TRY003

    monkeypatch.setattr(
        command_service_module.PostgresRouletteStorage, "load_current", boom
    )
    service = service_for(harness, random_source=CountingRandom())
    async with harness.scope("effect-db-error") as current:
        await seed_binding(harness.binding_manager, current, 1)
        with pytest.raises(StorageUnavailableError):
            await service.execute_group_command(
                request(
                    current,
                    "effect-db-error-join",
                    command_factory("join"),
                ),
                effect_check=gate,
            )
    assert gate_calls == [1]


# ---------------------------------------------------------------------------
# AC5: handler per-call closure across service + delivery
# ---------------------------------------------------------------------------


class _RecordingService:
    """Records the per-call effect_check the handler passes to the service."""

    def __init__(self, *, barrier: asyncio.Barrier | None = None) -> None:
        self.execute_effect_checks: list[Any] = []
        self.execute_requests: list[Any] = []
        self.observation_calls = 0
        self._barrier = barrier

    async def observe_current(self, group: Any) -> Any:
        del group
        self.observation_calls += 1
        return None

    async def execute_group_command(
        self,
        request: Any,
        *,
        observation: Any = None,
        effect_check: Any = None,
    ) -> Any:
        del observation
        self.execute_requests.append(request)
        self.execute_effect_checks.append(effect_check)
        if self._barrier is not None:
            await self._barrier.wait()
        if effect_check is not None and not await _maybe_await(effect_check()):
            from komari_bot.plugins.komari_roulette.command_service import (
                EffectCheckRejectedError,
            )

            raise EffectCheckRejectedError
        return receipt(
            receipt_id=f"r-{request.inbound_msg_id}",
            app_id=request.app_id,
            group_openid=request.group_openid,
            inbound_msg_id=request.inbound_msg_id,
            reply=projection("> 测试正文。"),
        )


class _RecordingDelivery:
    """Records the per-call effect_check the handler passes to the delivery."""

    def __init__(self) -> None:
        self.deliver_effect_checks: list[Any] = []
        self.deliver_calls: list[Any] = []

    async def deliver(
        self,
        receipt: Any,
        sender: Any,
        *,
        effect_check: Any = None,
    ) -> Any:
        self.deliver_calls.append((receipt, sender))
        self.deliver_effect_checks.append(effect_check)
        return None


async def test_handler_passes_original_token_closure_to_service_and_delivery() -> None:
    from komari_bot.plugins.komari_roulette.qq.handler import RouletteQQHandler

    gate_calls: list[tuple[Any, Any, Any]] = []

    def business_gate(bot: Any, event: Any, token: Any) -> bool:
        gate_calls.append((bot, event, token))
        return True

    service = _RecordingService()
    delivery = _RecordingDelivery()
    handler = RouletteQQHandler(
        service=service,
        delivery=delivery,
        business_gate=business_gate,
    )
    bot = FakeQQBot()
    event = make_group_at_event("/轮盘 开枪")
    token = business_token()
    await handler.handle(bot, event, state=admission_state(token=token))

    assert len(gate_calls) == 1, "the front-door business gate runs once"
    assert len(service.execute_effect_checks) == 1
    service_check = service.execute_effect_checks[0]
    assert service_check is not None, (
        "the handler must pass the per-call effect_check to the service"
    )
    await _maybe_await(service_check())
    assert gate_calls[-1][0] is bot
    assert gate_calls[-1][1] is event
    assert gate_calls[-1][2] is token, (
        "the post-lock closure must re-check the ORIGINAL token, not a "
        "receipt-derived re-mint"
    )

    assert len(delivery.deliver_effect_checks) == 1
    delivery_check = delivery.deliver_effect_checks[0]
    assert delivery_check is not None, (
        "the handler must pass the per-call effect_check to the delivery"
    )
    await _maybe_await(delivery_check())
    assert gate_calls[-1][2] is token
    assert gate_calls[-1][0] is bot
    assert gate_calls[-1][1] is event


async def test_handler_silently_absorbs_effect_rejection() -> None:
    """A rejected effect_check is a silent control flow, not an error reply."""

    from komari_bot.plugins.komari_roulette.qq.handler import RouletteQQHandler

    class _RejectingService(_RecordingService):
        async def execute_group_command(
            self,
            request: Any,
            *,
            observation: Any = None,
            effect_check: Any = None,
        ) -> Any:
            del request, observation, effect_check
            from komari_bot.plugins.komari_roulette.command_service import (
                EffectCheckRejectedError,
            )

            raise EffectCheckRejectedError

    service = _RejectingService()
    delivery = _RecordingDelivery()
    handler = RouletteQQHandler(
        service=service,
        delivery=delivery,
        business_gate=lambda _bot, _event, _token: True,
    )
    # No exception escapes and nothing is delivered.
    await handler.handle(
        FakeQQBot(),
        make_group_at_event("/轮盘 开枪"),
        state=admission_state(),
    )
    assert delivery.deliver_calls == []


async def test_concurrent_handlers_keep_their_own_token_in_the_closure() -> None:
    """Two interleaved handler calls must never borrow each other's token."""

    from komari_bot.plugins.komari_roulette.qq.handler import RouletteQQHandler

    seen: list[Any] = []

    def business_gate(bot: Any, event: Any, token: Any) -> bool:
        del bot, event
        seen.append(token)
        return True

    service = _RecordingService(barrier=asyncio.Barrier(2))
    delivery = _RecordingDelivery()
    handler = RouletteQQHandler(
        service=service,
        delivery=delivery,
        business_gate=business_gate,
    )

    async def run(group: str, member: str, message_id: str) -> None:
        event = make_group_at_event(
            "/轮盘 开枪",
            message_id=message_id,
            group_openid=group,
            member_openid=member,
        )
        token = business_token(
            group_openid=group,
            member_openid=member,
            qq_message_id=message_id,
        )
        with suppress(Exception):
            await handler.handle(FakeQQBot(), event, state=admission_state(token=token))

    await asyncio.gather(
        run("tsk279-effect-allowed", "tsk279-effect-a", "effect-msg-1"),
        run("tsk279-effect-blocked", "tsk279-effect-b", "effect-msg-2"),
    )
    assert len(service.execute_effect_checks) == 2
    assert len(service.execute_requests) == 2
    for command_request, check in zip(
        service.execute_requests,
        service.execute_effect_checks,
        strict=True,
    ):
        assert check is not None, (
            "each handler call must pass its own post-lock closure"
        )
        await _maybe_await(check())
        token = seen[-1]
        # The closure really re-checks THIS call's original identity.
        assert token.group_openid == command_request.group_openid
        assert token.member_openid == command_request.member_openid
        assert token.qq_message_id == command_request.inbound_msg_id


# ---------------------------------------------------------------------------
# AC5 (real service + real PG): the delivery post-claim check blocks the send
# ---------------------------------------------------------------------------


class _NetworkRecordingSender:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def send_to_group(
        self,
        group_openid: str,
        message: Any,
        *,
        msg_id: str | None = None,
        msg_seq: int | None = None,
    ) -> Any:
        self.calls.append(
            {
                "group_openid": group_openid,
                "message": message,
                "msg_id": msg_id,
                "msg_seq": msg_seq,
            }
        )
        return None


def test_delivery_exposes_per_call_effect_check_keyword() -> None:
    from komari_bot.plugins.komari_roulette.qq.delivery import RouletteDelivery

    signature = inspect.signature(RouletteDelivery.deliver)
    assert "effect_check" in signature.parameters, (
        "deliver must accept the per-call post-claim effect_check keyword"
    )


@PG_REQUIRED
async def test_delivery_post_claim_check_blocks_network_as_not_delivered(
    harness: Tsk279Harness,
) -> None:
    from komari_bot.plugins.komari_roulette.qq.delivery import (
        DeliveryOutcome,
        RouletteDelivery,
    )

    service = service_for(harness, random_source=CountingRandom())
    async with harness.scope("delivery-effect") as current:
        await seed_binding(harness.binding_manager, current, 1)
        pending = await create_waiting(
            service, current, message_id="delivery-effect-1"
        )
        sender = _NetworkRecordingSender()
        delivery = RouletteDelivery(
            service,
            runtime_check=lambda _receipt: True,
            payload_builder=lambda command_receipt: command_receipt.reply.body,
        )
        gate_calls: list[int] = []

        def gate() -> bool:
            gate_calls.append(1)
            return False

        outcome = await delivery.deliver(pending, sender, effect_check=gate)
        assert outcome is DeliveryOutcome.NOT_DELIVERED
        assert gate_calls == [1]
        assert sender.calls == [], "a rejected post-claim check must not send"
        async with harness.session_factory() as session:
            state = await session.scalar(
                text(
                    "SELECT state FROM komari_roulette_fulfillments "
                    "WHERE receipt_id = :receipt_id"
                ),
                {"receipt_id": pending.receipt_id},
            )
        assert state == "NOT_DELIVERED"


@PG_REQUIRED
async def test_delivery_variant_effect_rejected_after_lock_wait(
    harness: Tsk279Harness,
) -> None:
    """The real command path rejects an effect that turned false while locked."""


    service = service_for(harness, random_source=CountingRandom())
    async with harness.scope("effect-lock-revoke") as current:
        members = await seed_players(harness.binding_manager, current, 2)
        await create_waiting(service, current)
        await join_player(service, current, members[1], "revoke-join")
        state = {"allowed": True}

        async def gate() -> bool:
            return state["allowed"]

        async with harness.session_factory() as blocker:
            await blocker.begin()
            blocker_pid = await backend_pid(blocker)
            await hold_group_lock(blocker, current)
            task = asyncio.create_task(
                service.execute_group_command(
                    request(
                        current,
                        "revoke-start",
                        command_factory("start"),
                        member_openid=members[0],
                    ),
                    effect_check=gate,
                )
            )
            try:
                await wait_for_blocked(harness.session_factory, blocker_pid)
                # Authority revoked while the command waits on the group lock.
                state["allowed"] = False
            finally:
                await blocker.commit()
            with pytest.raises(Exception) as excinfo:
                async with asyncio.timeout(10):
                    await task
        assert type(excinfo.value).__name__ == EFFECT_REJECTION_NAME
        assert await _receipt_count(harness, current, "revoke-start") == 0
        row = await current_game_row(harness.session_factory, current)
        assert row is not None
        assert row["lifecycle"] == "waiting", (
            "a revoked group must not advance the game past waiting"
        )
