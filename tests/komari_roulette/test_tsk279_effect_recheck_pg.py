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
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

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
    CountingProjector,
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


# ---------------------------------------------------------------------------
# Real "old API" GREEN probes (no lifecycle module dependency)
#
# These pin the shipped admission / binding / user_ban authority chain the C1
# installed gates must reuse: a real READY ``AdmissionRuntime`` (real
# ``adjudicate`` / ``recheck_qq_effect``), real ``BindingTransaction`` canonical
# group/member resolution against real PostgreSQL, and a real ``user_ban``
# service (its real repository, not a `return False` shim).  They never touch
# the still-missing ``lifecycle`` module, so they are the persistent green
# proof the C1 review asked for.
# ---------------------------------------------------------------------------


async def _real_binding_resolvers(harness: Tsk279Harness) -> tuple[Any, Any]:
    """Build the canonical resolvers from the real ``BindingTransaction``."""

    from komari_bot.plugins.character_binding import BindingTransaction

    async def resolve_group(app_id: str, group_openid: str) -> int | None:
        async with harness.session_factory() as session, session.begin():
            group = await BindingTransaction(session).resolve_group(
                app_id=app_id,
                group_openid=group_openid,
                lock=False,
            )
        if group is None:
            return None
        value = int(group.group_id)
        return value if value > 0 else None

    async def resolve_member(
        app_id: str,
        group_openid: str,
        member_openid: str,
    ) -> int | None:
        async with harness.session_factory() as session, session.begin():
            member = await BindingTransaction(session).resolve_member(
                app_id=app_id,
                group_openid=group_openid,
                member_openid=member_openid,
                lock=False,
            )
        if member is None:
            return None
        value = int(member.member_qq)
        return value if value > 0 else None

    return resolve_group, resolve_member


@PG_REQUIRED
async def test_old_api_authority_chain_allows_then_rejects_the_same_token(
    harness: Tsk279Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real admission + binding + user_ban: mint, verify and then revoke one token."""

    from komari_bot.plugins import group_admission
    from komari_bot.plugins.user_ban.service import UserBanService
    from tests.group_admission.management_support import prepare_control_plane
    from tests.group_admission.qq_admission_support import QQProbeBot, make_group_at
    from tests.group_admission.registry_isolation_support import (
        registry_isolation_context,
    )
    from tests.group_admission.runtime_support import (
        AdmissionStorageFake,
        stored_policy,
    )

    storage = AdmissionStorageFake(
        stored_policy(1, {"mode": "blacklist", "group_ids": []})
    )
    await prepare_control_plane(monkeypatch, storage)

    async with harness.scope("old-api-green") as current:
        numeric_group = 279001
        member_qq = 1_000_000_000 + int(uuid4().int % 1_000_000_000)
        await harness.binding_manager.bind_group_member(
            app_id=current.app_id,
            group_id=str(numeric_group),
            group_openid=current.group_openid,
            member_qq=str(member_qq),
            member_openid=current.member_openid,
            character_name="Seat 1",
            bot_self_id="tsk279-test-bot",
        )
        resolve_group, resolve_member = await _real_binding_resolvers(harness)
        ban_service = UserBanService()

        async def ban_checker(member_qq_value: int, scope: object) -> bool:
            assert str(scope) == "command"
            return await ban_service.is_user_banned(
                str(member_qq_value), cast("Any", scope)
            )

        bot = QQProbeBot(self_id=current.app_id)
        event = make_group_at(
            content="/轮盘 开局",
            group_openid=current.group_openid,
            member_openid=current.member_openid,
            message_id="old-api-msg-1",
        )
        try:
            with registry_isolation_context():
                group_admission.register_qq_group_resolver(
                    resolve_group, member_resolver=resolve_member
                )
                group_admission.register_qq_ban_checker(ban_checker)
                try:
                    token = await group_admission.qualify_qq_event(bot, event)
                    assert token is not None, (
                        "the real admission chain must mint a business token"
                    )
                    assert token.scope == "business"
                    assert token.group_id == numeric_group
                    assert token.member_qq == member_qq

                    decision = await group_admission.recheck_qq_effect(
                        token, effect="business"
                    )
                    assert decision.allowed is True
                    assert decision.reason_code == "policy_admitted"

                    await ban_service.ban_user(
                        user_id=str(member_qq),
                        target_scope="command",
                        operator_id="tsk279-test",
                    )
                    banned = await group_admission.recheck_qq_effect(
                        token, effect="business"
                    )
                    assert banned.allowed is False
                    assert banned.reason_code == "user_banned"

                    await ban_service.unban_user(
                        user_id=str(member_qq), target_scope="command"
                    )
                    restored = await group_admission.recheck_qq_effect(
                        token, effect="business"
                    )
                    assert restored.allowed is True

                    storage.deliver(
                        stored_policy(
                            2,
                            {
                                "mode": "blacklist",
                                "group_ids": [numeric_group],
                            },
                        )
                    )
                    restricted = await group_admission.recheck_qq_effect(
                        token, effect="business"
                    )
                    assert restricted.allowed is False
                    assert restricted.reason_code == "policy_restricted"

                    storage.deliver(
                        stored_policy(3, {"mode": "blacklist", "group_ids": []})
                    )
                    readmitted = await group_admission.recheck_qq_effect(
                        token, effect="business"
                    )
                    assert readmitted.allowed is True

                    async with harness.engine.begin() as connection:
                        await connection.execute(
                            text(
                                "UPDATE komari_character_binding_groups "
                                "SET group_id = :moved "
                                "WHERE app_id = :app_id AND group_openid = :group"
                            ),
                            {
                                "moved": str(numeric_group + 1),
                                "app_id": current.app_id,
                                "group": current.group_openid,
                            },
                        )
                    remapped = await group_admission.recheck_qq_effect(
                        token, effect="business"
                    )
                    assert remapped.allowed is False
                    assert remapped.reason_code == "scope_mismatch"
                finally:
                    group_admission.register_qq_group_resolver(None)
                    group_admission.register_qq_ban_checker(None)
        finally:
            with suppress(Exception):
                await ban_service.unban_user(
                    user_id=str(member_qq), target_scope="command"
                )
            with suppress(Exception):
                await ban_service.close()


# ---------------------------------------------------------------------------
# Second green probe: the shipped PG credential window still blocks a send even
# when the runtime authority says yes, and (RED) an accepted post-claim
# ``effect_check`` must not bypass that window.
# ---------------------------------------------------------------------------


@PG_REQUIRED
async def test_real_delivery_true_runtime_check_with_expired_pg_window_never_sends(
    harness: Tsk279Harness,
) -> None:
    """``runtime_check`` accepts, the PG window expired → NOT_DELIVERED, no network."""

    from komari_bot.plugins.komari_roulette.qq.delivery import (
        DeliveryOutcome,
        RouletteDelivery,
    )

    service = service_for(harness, random_source=CountingRandom())
    async with harness.scope("window-green") as current:
        await seed_binding(harness.binding_manager, current, 1)
        pending = await create_waiting(
            service, current, message_id="window-green-1"
        )
        sender = _NetworkRecordingSender()

        async def runtime_accepts(receipt: Any) -> bool:
            assert receipt is pending
            async with harness.session_factory() as session:
                await session.execute(
                    text(
                        "UPDATE komari_roulette_command_receipts "
                        "SET created_at = clock_timestamp() "
                        "- make_interval(secs => 301) "
                        "WHERE receipt_id = :receipt_id"
                    ),
                    {"receipt_id": pending.receipt_id},
                )
                await session.commit()
            return True

        outcome = await RouletteDelivery(
            service,
            runtime_check=runtime_accepts,
            payload_builder=lambda command_receipt: command_receipt.reply.body,
        ).deliver(pending, sender)

        assert outcome is DeliveryOutcome.NOT_DELIVERED
        assert sender.calls == []
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
async def test_real_delivery_accepted_effect_check_still_honours_expired_window(
    harness: Tsk279Harness,
) -> None:
    """An accepted post-claim ``effect_check`` must not bypass the PG window."""

    from komari_bot.plugins.komari_roulette.qq.delivery import (
        DeliveryOutcome,
        RouletteDelivery,
    )

    service = service_for(harness, random_source=CountingRandom())
    async with harness.scope("window-effect-green") as current:
        await seed_binding(harness.binding_manager, current, 1)
        pending = await create_waiting(
            service, current, message_id="window-effect-1"
        )
        sender = _NetworkRecordingSender()

        async def runtime_accepts(receipt: Any) -> bool:
            assert receipt is pending
            async with harness.session_factory() as session:
                await session.execute(
                    text(
                        "UPDATE komari_roulette_command_receipts "
                        "SET created_at = clock_timestamp() "
                        "- make_interval(secs => 301) "
                        "WHERE receipt_id = :receipt_id"
                    ),
                    {"receipt_id": pending.receipt_id},
                )
                await session.commit()
            return True

        outcome = await RouletteDelivery(
            service,
            runtime_check=runtime_accepts,
            payload_builder=lambda command_receipt: command_receipt.reply.body,
        ).deliver(pending, sender, effect_check=lambda: True)

        assert outcome is DeliveryOutcome.NOT_DELIVERED
        assert sender.calls == []


# ---------------------------------------------------------------------------
# Installed real handler / service / delivery GREEN proofs
#
# The C1 review found that "QQ runtime != None" plus always-true fakes let the
# RED matrix pass without ever reading real authority.  These GREEN cases wire
# the *real* installed matcher (via ``install_roulette_qq_runtime`` /
# ``handle_roulette_qq``), a real ``RouletteCommandService`` on PostgreSQL and
# the real QQ adapter transport (recorded, never sent), and require the installed
# business gate to answer from the real ``recheck_qq_effect`` authority chain.
# ---------------------------------------------------------------------------


async def _mint_business_token(
    current: Any,
    message_id: str,
) -> tuple[Any, Any, Any]:
    from komari_bot.plugins import group_admission
    from tests.group_admission.qq_admission_support import QQProbeBot, make_group_at

    bot = QQProbeBot(self_id=current.app_id)
    event = make_group_at(
        content="/轮盘 开局",
        group_openid=current.group_openid,
        member_openid=current.member_openid,
        message_id=message_id,
    )
    token = await group_admission.qualify_qq_event(bot, event)
    assert token is not None, "the real admission chain must mint the token"
    return token, bot, event


@PG_REQUIRED
async def test_installed_matcher_commits_valid_token_and_captures_real_sdk_payload(
    harness: Tsk279Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A valid original token through the installed handler commits and sends once."""

    from komari_bot.plugins import group_admission
    from komari_bot.plugins.komari_roulette.qq import (
        clear_roulette_qq_runtime,
        handle_roulette_qq,
        install_roulette_qq_runtime,
    )
    from komari_bot.plugins.user_ban.service import UserBanService
    from tests.group_admission.management_support import prepare_control_plane
    from tests.group_admission.runtime_support import (
        AdmissionStorageFake,
        stored_policy,
    )

    from .tsk279_lifecycle_support import RecordingQQBot

    storage = AdmissionStorageFake(
        stored_policy(1, {"mode": "blacklist", "group_ids": []})
    )
    await prepare_control_plane(monkeypatch, storage)
    # Real projector metadata: the frozen keyboard spec is part of the wire
    # contract, so the default ``build_qq_message`` path is exercised end to end.
    service = service_for(
        harness,
        projector=CountingProjector(metadata={"keyboard": '{"rows": []}'}),
        random_source=CountingRandom(),
    )

    async with harness.scope("installed-green") as current:
        numeric_group = 279101
        member_qq = 2_000_000_000 + int(uuid4().int % 1_000_000_000)
        await harness.binding_manager.bind_group_member(
            app_id=current.app_id,
            group_id=str(numeric_group),
            group_openid=current.group_openid,
            member_qq=str(member_qq),
            member_openid=current.member_openid,
            character_name="Seat 1",
            bot_self_id="tsk279-test-bot",
        )
        resolve_group, resolve_member = await _real_binding_resolvers(harness)
        ban_service = UserBanService()

        async def ban_checker(member_qq_value: int, scope: object) -> bool:
            return await ban_service.is_user_banned(
                str(member_qq_value), cast("Any", scope)
            )

        group_admission.register_qq_group_resolver(
            resolve_group, member_resolver=resolve_member
        )
        group_admission.register_qq_ban_checker(ban_checker)
        try:
            token, _mint_bot, event = await _mint_business_token(
                current, "installed-green-1"
            )

            async def business_gate(_bot: Any, _event: Any, checked: Any) -> bool:
                decision = await group_admission.recheck_qq_effect(
                    checked, effect="business"
                )
                return decision.allowed

            install_roulette_qq_runtime(
                service=service,
                business_gate=business_gate,
                runtime_check=lambda _receipt: True,
                send_gate=lambda _request: True,
            )
            try:
                bot = RecordingQQBot(current.app_id)
                await handle_roulette_qq(
                    bot, event, admission_state(token=token)
                )
                assert await _receipt_count(
                    harness, current, "installed-green-1"
                ) == 1, "the installed handler must commit the domain receipt"
                apis = [api for api, _data in bot.calls]
                assert apis == ["post_group_messages"], (
                    f"the installed delivery must send exactly one real SDK "
                    f"payload, got {apis}"
                )
                payload = bot.calls[0][1]
                assert payload["msg_id"] == "installed-green-1"
                assert payload["msg_seq"] == 1
                assert payload["markdown"].content == "冻结安全回复"
            finally:
                clear_roulette_qq_runtime()
        finally:
            group_admission.register_qq_group_resolver(None)
            group_admission.register_qq_ban_checker(None)
            with suppress(Exception):
                await ban_service.unban_user(
                    user_id=str(member_qq), target_scope="command"
                )
            with suppress(Exception):
                await ban_service.close()


@PG_REQUIRED
async def test_installed_matcher_business_gate_reads_live_authority_for_same_token(
    harness: Tsk279Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ban / canonical remap / policy change must each deny the minted token."""

    from komari_bot.plugins import group_admission
    from komari_bot.plugins.komari_roulette.qq import (
        clear_roulette_qq_runtime,
        handle_roulette_qq,
        install_roulette_qq_runtime,
    )
    from komari_bot.plugins.user_ban.service import UserBanService
    from tests.group_admission.management_support import prepare_control_plane
    from tests.group_admission.runtime_support import (
        AdmissionStorageFake,
        stored_policy,
    )

    from .tsk279_lifecycle_support import RecordingQQBot

    storage = AdmissionStorageFake(
        stored_policy(1, {"mode": "blacklist", "group_ids": []})
    )
    await prepare_control_plane(monkeypatch, storage)
    service = service_for(harness, random_source=CountingRandom())

    async with harness.scope("installed-authority") as current:
        numeric_group = 279201
        member_qq = 3_000_000_000 + int(uuid4().int % 1_000_000_000)
        await harness.binding_manager.bind_group_member(
            app_id=current.app_id,
            group_id=str(numeric_group),
            group_openid=current.group_openid,
            member_qq=str(member_qq),
            member_openid=current.member_openid,
            character_name="Seat 1",
            bot_self_id="tsk279-test-bot",
        )
        resolve_group, resolve_member = await _real_binding_resolvers(harness)
        ban_service = UserBanService()

        async def ban_checker(member_qq_value: int, scope: object) -> bool:
            return await ban_service.is_user_banned(
                str(member_qq_value), cast("Any", scope)
            )

        async def business_gate(_bot: Any, _event: Any, checked: Any) -> bool:
            decision = await group_admission.recheck_qq_effect(
                checked, effect="business"
            )
            return decision.allowed

        group_admission.register_qq_group_resolver(
            resolve_group, member_resolver=resolve_member
        )
        group_admission.register_qq_ban_checker(ban_checker)
        install_roulette_qq_runtime(
            service=service,
            business_gate=business_gate,
            runtime_check=lambda _receipt: True,
            send_gate=lambda _request: True,
        )
        bot = RecordingQQBot(current.app_id)
        try:
            # 1. An applicable user ban rejects the already-minted token.
            token_ban, _mint_bot, event_ban = await _mint_business_token(
                current, "installed-ban-1"
            )
            await ban_service.ban_user(
                user_id=str(member_qq),
                target_scope="command",
                operator_id="tsk279-test",
            )
            await handle_roulette_qq(
                bot, event_ban, admission_state(token=token_ban)
            )
            await ban_service.unban_user(
                user_id=str(member_qq), target_scope="command"
            )

            # 2. A canonical group remap rejects the same (already-minted) token.
            token_map, _mint_bot, event_map = await _mint_business_token(
                current, "installed-map-1"
            )
            async with harness.engine.begin() as connection:
                await connection.execute(
                    text(
                        "UPDATE komari_character_binding_groups "
                        "SET group_id = :moved "
                        "WHERE app_id = :app_id AND group_openid = :group"
                    ),
                    {
                        "moved": str(numeric_group + 1),
                        "app_id": current.app_id,
                        "group": current.group_openid,
                    },
                )
            await handle_roulette_qq(
                bot, event_map, admission_state(token=token_map)
            )
            async with harness.engine.begin() as connection:
                await connection.execute(
                    text(
                        "UPDATE komari_character_binding_groups "
                        "SET group_id = :original "
                        "WHERE app_id = :app_id AND group_openid = :group"
                    ),
                    {
                        "original": str(numeric_group),
                        "app_id": current.app_id,
                        "group": current.group_openid,
                    },
                )

            # 3. A live policy revocation rejects the same (already-minted) token.
            token_policy, _mint_bot, event_policy = await _mint_business_token(
                current, "installed-policy-1"
            )
            storage.deliver(
                stored_policy(
                    2, {"mode": "blacklist", "group_ids": [numeric_group]}
                )
            )
            await handle_roulette_qq(
                bot, event_policy, admission_state(token=token_policy)
            )
            storage.deliver(
                stored_policy(3, {"mode": "blacklist", "group_ids": []})
            )

            assert bot.calls == [], "a denied authority must never send"
            assert await _receipt_count(harness, current, "installed-ban-1") == 0
            assert await _receipt_count(harness, current, "installed-map-1") == 0
            assert await _receipt_count(
                harness, current, "installed-policy-1"
            ) == 0
        finally:
            clear_roulette_qq_runtime()
            group_admission.register_qq_group_resolver(None)
            group_admission.register_qq_ban_checker(None)
            with suppress(Exception):
                await ban_service.unban_user(
                    user_id=str(member_qq), target_scope="command"
                )
            with suppress(Exception):
                await ban_service.close()


# ---------------------------------------------------------------------------
# Installed-path RED: the post-lock authority window through the real handler
# ---------------------------------------------------------------------------


@PG_REQUIRED
async def test_installed_handler_rechecks_authority_inside_the_group_lock(
    harness: Tsk279Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Authority revoked while the installed handler waits on the group lock.

    The full production path is used here (real matcher, real service, real
    admission/binding/ban).  The handler mints a valid token, then the member is
    banned while the domain command is queued on the group advisory lock; the
    installed path must re-read the authority *after* the lock and produce no
    receipt and no send.  A handler that only checks the front door fails this.
    """

    from komari_bot.plugins import group_admission
    from komari_bot.plugins.komari_roulette.qq import (
        clear_roulette_qq_runtime,
        handle_roulette_qq,
        install_roulette_qq_runtime,
    )
    from komari_bot.plugins.user_ban.service import UserBanService
    from tests.group_admission.management_support import prepare_control_plane
    from tests.group_admission.runtime_support import (
        AdmissionStorageFake,
        stored_policy,
    )

    from .tsk279_lifecycle_support import RecordingQQBot

    storage = AdmissionStorageFake(
        stored_policy(1, {"mode": "blacklist", "group_ids": []})
    )
    await prepare_control_plane(monkeypatch, storage)
    service = service_for(
        harness,
        projector=CountingProjector(metadata={"keyboard": '{"rows": []}'}),
        random_source=CountingRandom(),
    )

    async with harness.scope("installed-postlock") as current:
        numeric_group = 279301
        member_qq = 4_000_000_000 + int(uuid4().int % 1_000_000_000)
        await harness.binding_manager.bind_group_member(
            app_id=current.app_id,
            group_id=str(numeric_group),
            group_openid=current.group_openid,
            member_qq=str(member_qq),
            member_openid=current.member_openid,
            character_name="Seat 1",
            bot_self_id="tsk279-test-bot",
        )
        resolve_group, resolve_member = await _real_binding_resolvers(harness)
        ban_service = UserBanService()

        async def ban_checker(member_qq_value: int, scope: object) -> bool:
            return await ban_service.is_user_banned(
                str(member_qq_value), cast("Any", scope)
            )

        async def business_gate(_bot: Any, _event: Any, checked: Any) -> bool:
            decision = await group_admission.recheck_qq_effect(
                checked, effect="business"
            )
            return decision.allowed

        group_admission.register_qq_group_resolver(
            resolve_group, member_resolver=resolve_member
        )
        group_admission.register_qq_ban_checker(ban_checker)
        install_roulette_qq_runtime(
            service=service,
            business_gate=business_gate,
            runtime_check=lambda _receipt: True,
            send_gate=lambda _request: True,
        )
        bot = RecordingQQBot(current.app_id)
        try:
            token, _mint_bot, event = await _mint_business_token(
                current, "postlock-create-1"
            )
            async with harness.session_factory() as blocker:
                await blocker.begin()
                blocker_pid = await backend_pid(blocker)
                await hold_group_lock(blocker, current)

                async def _run_handler() -> None:
                    await handle_roulette_qq(
                        bot, event, admission_state(token=token)
                    )

                task = asyncio.create_task(_run_handler())
                try:
                    await wait_for_blocked(harness.session_factory, blocker_pid)
                    # The authority is revoked while the command is queued.
                    await ban_service.ban_user(
                        user_id=str(member_qq),
                        target_scope="command",
                        operator_id="tsk279-test",
                    )
                finally:
                    await blocker.commit()
                async with asyncio.timeout(10):
                    with suppress(Exception):
                        await task

            assert await _receipt_count(
                harness, current, "postlock-create-1"
            ) == 0, (
                "an authority revoked while the command queued must leave no "
                "domain receipt"
            )
            assert bot.calls == [], (
                "an authority revoked while the command queued must never send"
            )
        finally:
            clear_roulette_qq_runtime()
            group_admission.register_qq_group_resolver(None)
            group_admission.register_qq_ban_checker(None)
            with suppress(Exception):
                await ban_service.unban_user(
                    user_id=str(member_qq), target_scope="command"
                )
            with suppress(Exception):
                await ban_service.close()
