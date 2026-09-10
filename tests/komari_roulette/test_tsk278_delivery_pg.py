"""TSK-278 RED baseline: delivery against the real fulfillment store.

Gated on ``KOMARI_TEST_POSTGRES_URL`` exactly like the existing TSK-276
tests.  It exercises ``RouletteDelivery`` over a real ``RouletteCommandService``
(real claim/mark rows) with a replaceable ``FakeSender``; the payload is a
real QQ ``Message`` rebuilt from the frozen receipt projection.

RED: ``RouletteDelivery`` / ``DeliveryOutcome`` / ``SendNotAcceptedError`` /
``keyboard_from_spec`` are imported inside each test so this file still skips
cleanly without PostgreSQL and reports the same missing-seam error when the
gate is enabled.
"""

from __future__ import annotations

import asyncio
import time
from contextlib import suppress
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy import text

from komari_bot.plugins.character_binding.manager import CharacterBindingManager
from komari_bot.plugins.komari_roulette import (
    FulfillmentState,
    ReplyProjection,
    RouletteCommandService,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from .command_support import (
    PG_REQUIRED,
    backend_pid,
    command_factory,
    create_engine_and_factory,
    delete_scope,
    observation,
    request,
    reset_shared_orm_engine,
    scope,
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
    start_game,
)
from .tsk278_support import (
    FakeSender,
    assert_no_keyboard_segment,
    assert_single_mention_tag,
    message_markdown_content,
)

pytestmark = [pytest.mark.asyncio, PG_REQUIRED]


class MentionProjector(CountingProjector):
    """Projector that freezes the real mention into the body.

    Stands in for the missing TSK-278 ``render_reply`` so the real
    service→receipt→delivery chain can be exercised against a body that
    actually contains the native mention tag at the canonical position.
    """

    def __call__(self, context: Any) -> ReplyProjection:
        projection = super().__call__(context)
        body = projection.body
        if context.mention_target is not None:
            tag = (
                f'<qqbot-at-user id="{context.mention_target.member_openid}" />'
            )
            if context.mention_reason in {"turn", "reward"}:
                name = context.current_player.display_name
                body = f"**当前：{name}** {tag}"
            elif context.mention_reason == "winner":
                wins = context.winner_group_wins or 1
                name = context.mention_target.display_name
                body = f"{name} {tag} 获胜，累计胜场 {wins}。"
            elif context.mention_reason == "lock_target":
                actor = context.current_player.display_name
                name = context.mention_target.display_name
                body = f"> {actor}对{name}（{tag}）使用了锁。"
        return ReplyProjection(body=body, metadata=projection.metadata)


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
    from komari_bot.plugins.komari_roulette.qq.delivery import (
        DeliveryOutcome,
        RouletteDelivery,
    )

    _engine, session_factory, manager = harness
    current = scope("tsk278-delivery-ok")
    await seed_binding(manager, current, 1)
    service = RouletteCommandService(
        session_factory=session_factory,
        reply_projector=CountingProjector(metadata={"keyboard": '{"rows": []}'}),
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


async def test_real_delivery_payload_is_real_qq_message(
    harness: tuple[
        AsyncEngine,
        async_sessionmaker[AsyncSession],
        CharacterBindingManager,
    ],
) -> None:
    from komari_bot.plugins.komari_roulette.qq.delivery import (
        DeliveryOutcome,
        RouletteDelivery,
    )

    _engine, session_factory, manager = harness
    current = scope("tsk278-delivery-payload")
    await seed_binding(manager, current, 1)
    service = RouletteCommandService(
        session_factory=session_factory,
        reply_projector=CountingProjector(metadata={"keyboard": '{"rows": []}'}),
    )
    real_receipt = await create_waiting(service, current)

    sender = FakeSender(result="qq-platform-real-payload")
    outcome = await RouletteDelivery(service=service).deliver(real_receipt, sender)

    assert outcome is DeliveryOutcome.DELIVERED
    message = sender.calls[0]["message"]
    # 真实 QQ MessageSegment.markdown 正文来自冻结收据投影。
    assert message_markdown_content(message) == real_receipt.reply.body
    # 冻结 keyboard spec '{"rows": []}' 无按钮 → 载荷不得携带空 keyboard 字段。
    assert_no_keyboard_segment(message)


async def test_real_delivery_preserves_frozen_mention_body(
    harness: tuple[
        AsyncEngine,
        async_sessionmaker[AsyncSession],
        CharacterBindingManager,
    ],
) -> None:
    """真实服务上下文产生轮转提及 → 冻结投影 → 交付真实 Message 且 tag 位置不变。"""
    from komari_bot.plugins.komari_roulette.qq.delivery import (
        DeliveryOutcome,
        RouletteDelivery,
    )

    _engine, session_factory, manager = harness
    current = scope("tsk278-delivery-mention")
    members = await seed_players(manager, current, 2)
    projector = MentionProjector(metadata={"keyboard": '{"rows": []}'})
    service = RouletteCommandService(
        session_factory=session_factory,
        reply_projector=projector,
        random_source=CountingRandom(),
    )
    await create_waiting(service, current)
    await join_player(service, current, members[1], "join-2")
    await start_game(service, current, members[0])
    shot_row = await current_game_row(session_factory, current)
    assert shot_row is not None
    await service.execute_group_command(
        request(
            current,
            "shot-1",
            command_factory("shoot"),
            member_openid=members[0],
        ),
        observation=observation(
            game_id=str(shot_row["game_id"]),
            state_revision=int(shot_row["state_revision"]),
            turn_seq=int(shot_row["turn_seq"]),
        ),
    )
    end_row = await current_game_row(session_factory, current)
    assert end_row is not None
    ended = await service.execute_group_command(
        request(
            current,
            "end-turn-1",
            command_factory("end_turn"),
            member_openid=members[0],
        ),
        observation=observation(
            game_id=str(end_row["game_id"]),
            state_revision=int(end_row["state_revision"]),
            turn_seq=int(end_row["turn_seq"]),
        ),
    )
    assert ended.result_code == "turn_ended"
    context_obj = projector.context_objects[-1]
    assert context_obj.mention_reason == "turn"
    assert context_obj.mention_target is not None
    assert context_obj.current_player is not None
    mention_openid = context_obj.mention_target.member_openid
    assert mention_openid == members[1]

    sender = FakeSender(result="qq-platform-real-mention")
    outcome = await RouletteDelivery(service=service).deliver(ended, sender)

    assert outcome is DeliveryOutcome.DELIVERED
    message = sender.calls[0]["message"]
    markdown = message_markdown_content(message)
    # 交付载荷只消费已提交收据：正文与冻结投影逐字一致，tag 位置不变。
    assert markdown == ended.reply.body
    assert_single_mention_tag(markdown, members[1])
    expected = (
        f"**当前：{context_obj.current_player.display_name}** "
        f'<qqbot-at-user id="{members[1]}" />'
    )
    assert markdown == expected


async def test_real_claim_happens_before_send(
    harness: tuple[
        AsyncEngine,
        async_sessionmaker[AsyncSession],
        CharacterBindingManager,
    ],
) -> None:
    """真实顺序断言：sender 调用时履约行必须已是 PENDING_CONFIRMATION。"""
    from komari_bot.plugins.komari_roulette.qq.delivery import (
        DeliveryOutcome,
        RouletteDelivery,
    )

    _engine, session_factory, manager = harness
    current = scope("tsk278-delivery-order")
    await seed_binding(manager, current, 1)
    service = RouletteCommandService(
        session_factory=session_factory,
        reply_projector=CountingProjector(metadata={"keyboard": '{"rows": []}'}),
    )
    real_receipt = await create_waiting(service, current)
    states_at_send: list[str] = []

    class SnapshotSender(FakeSender):
        async def send_to_group(
            self,
            group_openid: str,
            message: Any,
            *,
            msg_id: str | None = None,
            msg_seq: int | None = None,
            **kwargs: Any,
        ) -> Any:
            async with session_factory() as session:
                state = await session.scalar(
                    text(
                        "SELECT state FROM komari_roulette_fulfillments "
                        "WHERE receipt_id = :receipt_id"
                    ),
                    {"receipt_id": real_receipt.receipt_id},
                )
            states_at_send.append(str(state))
            return await super().send_to_group(
                group_openid,
                message,
                msg_id=msg_id,
                msg_seq=msg_seq,
                **kwargs,
            )

    sender = SnapshotSender(result="qq-platform-real-order")
    outcome = await RouletteDelivery(service=service).deliver(real_receipt, sender)

    assert outcome is DeliveryOutcome.DELIVERED
    # claim 先于 send 生效：发送瞬间行已领取（PENDING_CONFIRMATION），
    # mark_delivered 发生在 send 之后（发送瞬间尚未 DELIVERED）。
    assert states_at_send == ["PENDING_CONFIRMATION"]
    async with session_factory() as session:
        final_state = await session.scalar(
            text(
                "SELECT state FROM komari_roulette_fulfillments "
                "WHERE receipt_id = :receipt_id"
            ),
            {"receipt_id": real_receipt.receipt_id},
        )
    assert final_state == "DELIVERED"


async def test_real_delivery_duplicate_event_single_network_call(
    harness: tuple[
        AsyncEngine,
        async_sessionmaker[AsyncSession],
        CharacterBindingManager,
    ],
) -> None:
    from komari_bot.plugins.komari_roulette.qq.delivery import (
        DeliveryOutcome,
        RouletteDelivery,
    )

    _engine, session_factory, manager = harness
    current = scope("tsk278-delivery-dup")
    await seed_binding(manager, current, 1)
    service = RouletteCommandService(
        session_factory=session_factory,
        reply_projector=CountingProjector(metadata={"keyboard": '{"rows": []}'}),
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


async def test_real_delivery_missing_platform_id_stays_pending_confirmation(
    harness: tuple[
        AsyncEngine,
        async_sessionmaker[AsyncSession],
        CharacterBindingManager,
    ],
) -> None:
    """平台回执无可用 id（真实 ``PostGroupMessagesReturn.id`` 可为 ``None``）→
    UNKNOWN，真实履约行保持 PENDING_CONFIRMATION，绝不写假 id。"""
    from komari_bot.plugins.komari_roulette.qq.delivery import (
        DeliveryOutcome,
        RouletteDelivery,
    )

    _engine, session_factory, manager = harness
    current = scope("tsk278-delivery-noid")
    await seed_binding(manager, current, 1)
    service = RouletteCommandService(
        session_factory=session_factory,
        reply_projector=CountingProjector(metadata={"keyboard": '{"rows": []}'}),
    )
    real_receipt = await create_waiting(service, current)

    class _NoIdResponse:
        id = None

    sender = FakeSender(result=_NoIdResponse())
    outcome = await RouletteDelivery(service=service).deliver(
        real_receipt, sender
    )

    assert outcome is DeliveryOutcome.UNKNOWN
    assert len(sender.network_calls) == 1
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
    assert row["state"] == "PENDING_CONFIRMATION"
    assert row["platform_message_id"] is None


async def test_real_delivery_explicit_failure_marks_not_delivered(
    harness: tuple[
        AsyncEngine,
        async_sessionmaker[AsyncSession],
        CharacterBindingManager,
    ],
) -> None:
    from komari_bot.plugins.komari_roulette.qq.delivery import (
        DeliveryOutcome,
        RouletteDelivery,
        SendNotAcceptedError,
    )

    _engine, session_factory, manager = harness
    current = scope("tsk278-delivery-fail")
    await seed_binding(manager, current, 1)
    service = RouletteCommandService(
        session_factory=session_factory,
        reply_projector=CountingProjector(metadata={"keyboard": '{"rows": []}'}),
    )
    real_receipt = await create_waiting(service, current)

    sender = FakeSender(mode="fail_before_send", exc=SendNotAcceptedError("blocked"))
    outcome = await RouletteDelivery(service=service).deliver(real_receipt, sender)

    assert outcome is DeliveryOutcome.NOT_DELIVERED
    assert sender.network_calls == []

    async with session_factory() as session:
        state = await session.scalar(
            text(
                "SELECT state FROM komari_roulette_fulfillments "
                "WHERE receipt_id = :receipt_id"
            ),
            {"receipt_id": real_receipt.receipt_id},
        )
    assert state == "NOT_DELIVERED"


async def test_real_runtime_recheck_failure_is_zero_network(
    harness: tuple[
        AsyncEngine,
        async_sessionmaker[AsyncSession],
        CharacterBindingManager,
    ],
) -> None:
    """发送前 runtime 重核（5 分钟凭证过期/轮盘开关关闭/群准入受限）→ NOT_DELIVERED。

    276 无 ``mark_not_started_failed``（已核查实际接口）；预发送失败经真实
    claim_fulfillment（NOT_STARTED→PENDING）+ mark_not_delivered
    （PENDING→NOT_DELIVERED），0 网络。
    """
    from komari_bot.plugins.komari_roulette.qq.delivery import (
        DeliveryOutcome,
        RouletteDelivery,
    )

    _engine, session_factory, manager = harness
    current = scope("tsk278-delivery-runtime")
    await seed_binding(manager, current, 1)
    service = RouletteCommandService(
        session_factory=session_factory,
        reply_projector=CountingProjector(metadata={"keyboard": '{"rows": []}'}),
    )
    real_receipt = await create_waiting(service, current)

    sender = FakeSender()
    outcome = await RouletteDelivery(
        service=service,
        runtime_check=lambda: False,
    ).deliver(real_receipt, sender)

    assert outcome is DeliveryOutcome.NOT_DELIVERED
    assert sender.calls == []
    assert sender.network_calls == []
    async with session_factory() as session:
        state = await session.scalar(
            text(
                "SELECT state FROM komari_roulette_fulfillments "
                "WHERE receipt_id = :receipt_id"
            ),
            {"receipt_id": real_receipt.receipt_id},
        )
    assert state == "NOT_DELIVERED"


async def test_real_async_runtime_recheck_is_awaited(
    harness: tuple[
        AsyncEngine,
        async_sessionmaker[AsyncSession],
        CharacterBindingManager,
    ],
) -> None:
    """真实准入重核为异步可调用时同样被 await 并拦截发送（0 网络）。"""
    from komari_bot.plugins.komari_roulette.qq.delivery import (
        DeliveryOutcome,
        RouletteDelivery,
    )

    _engine, session_factory, manager = harness
    current = scope("tsk278-delivery-async-runtime")
    await seed_binding(manager, current, 1)
    service = RouletteCommandService(
        session_factory=session_factory,
        reply_projector=CountingProjector(metadata={"keyboard": '{"rows": []}'}),
    )
    real_receipt = await create_waiting(service, current)
    seen: list[str] = []

    async def async_runtime() -> bool:
        seen.append("runtime")
        return False

    sender = FakeSender()
    outcome = await RouletteDelivery(
        service=service,
        runtime_check=async_runtime,
    ).deliver(real_receipt, sender)

    assert outcome is DeliveryOutcome.NOT_DELIVERED
    assert seen == ["runtime"]
    assert sender.calls == []
    assert sender.network_calls == []
    async with session_factory() as session:
        state = await session.scalar(
            text(
                "SELECT state FROM komari_roulette_fulfillments "
                "WHERE receipt_id = :receipt_id"
            ),
            {"receipt_id": real_receipt.receipt_id},
        )
    assert state == "NOT_DELIVERED"


async def test_real_build_failure_is_zero_network(
    harness: tuple[
        AsyncEngine,
        async_sessionmaker[AsyncSession],
        CharacterBindingManager,
    ],
) -> None:
    """冻结投影 keyboard spec 损坏 → 构建失败 → claim 后 mark_not_delivered。"""
    from komari_bot.plugins.komari_roulette.qq.delivery import (
        DeliveryOutcome,
        RouletteDelivery,
    )

    _engine, session_factory, manager = harness
    current = scope("tsk278-delivery-buildfail")
    await seed_binding(manager, current, 1)
    service = RouletteCommandService(
        session_factory=session_factory,
        reply_projector=CountingProjector(metadata={"keyboard": "{broken"}),
    )
    real_receipt = await create_waiting(service, current)

    sender = FakeSender()
    outcome = await RouletteDelivery(service=service).deliver(real_receipt, sender)

    assert outcome is DeliveryOutcome.NOT_DELIVERED
    assert sender.calls == []
    assert sender.network_calls == []
    async with session_factory() as session:
        state = await session.scalar(
            text(
                "SELECT state FROM komari_roulette_fulfillments "
                "WHERE receipt_id = :receipt_id"
            ),
            {"receipt_id": real_receipt.receipt_id},
        )
    assert state == "NOT_DELIVERED"


async def test_leaderboard_after_self_expiry_is_not_leaderboard(
    harness: tuple[
        AsyncEngine,
        async_sessionmaker[AsyncSession],
        CharacterBindingManager,
    ],
) -> None:
    """TSK-266 10.2：排行榜查询惰性推进到期；若查询者正是被推进超时的原当前玩家，
    本票必须返回超时结果（turn_expired + 轮转/终局），不得追加排行榜。"""
    _engine, session_factory, manager = harness
    current = scope("tsk278-leaderboard-suppress")
    members = await seed_players(manager, current, 2)
    projector = CountingProjector(metadata={"keyboard": '{"rows": []}'})
    service = RouletteCommandService(
        session_factory=session_factory,
        reply_projector=projector,
        random_source=CountingRandom(),
    )
    await create_waiting(service, current)
    await join_player(service, current, members[1], "join-2")
    await start_game(service, current, members[0])

    # 到期：真实回填 turn_deadline_at 到过去。
    async with session_factory() as session:
        await session.execute(
            text(
                "UPDATE komari_roulette_games "
                "SET turn_deadline_at = NOW() - INTERVAL '1 minute' "
                "WHERE app_id = :app_id AND group_openid = :group_openid"
            ),
            {
                "app_id": current.app_id,
                "group_openid": current.group_openid,
            },
        )
        await session.commit()

    # 原当前玩家（局主）查询排行榜 → 返回超时结果，不是排行榜。
    suppressed = await service.execute_group_command(
        request(
            current,
            "leaderboard-self",
            command_factory("leaderboard"),
            member_openid=members[0],
        )
    )
    assert suppressed.result_code == "turn_expired"
    context = projector.context_objects[-1]
    assert context.result_code == "turn_expired"
    assert context.details.get("eliminated_reason") == "timeout"
    assert context.winner_group_wins == 1

    # 其他玩家查询排行榜 → 正常排行榜。
    shown = await service.execute_group_command(
        request(
            current,
            "leaderboard-other",
            command_factory("leaderboard"),
            member_openid=members[1],
        )
    )
    assert shown.result_code == "leaderboard"
    assert projector.context_objects[-1].result_code == "leaderboard"


# ---------------------------------------------------------------------------
# 5 分钟凭证窗口：收据 created_at 对照 DB 时钟，在 claim 内原子判定
# ---------------------------------------------------------------------------


async def _receipt_age_seconds(
    session_factory: async_sessionmaker[AsyncSession],
    receipt_id: str,
) -> float:
    async with session_factory() as session:
        age = await session.scalar(
            text(
                "SELECT EXTRACT(EPOCH FROM (clock_timestamp() - created_at)) "
                "FROM komari_roulette_command_receipts "
                "WHERE receipt_id = :receipt_id"
            ),
            {"receipt_id": receipt_id},
        )
    assert age is not None
    return float(age)


async def test_real_receipt_aged_past_window_never_sends(
    harness: tuple[
        AsyncEngine,
        async_sessionmaker[AsyncSession],
        CharacterBindingManager,
    ],
) -> None:
    """收据 created_at 超过 300s → claim 原子收敛为 NOT_DELIVERED，0 网络。

    TSK-267 §8 / TSK-278 评论 6a9ed97d：NOT_STARTED 且未调用过 QQ API 时，
    凭证>5min 与构建失败/插件关闭/准入受限一样，原子转 NOT_DELIVERED，
    绝不调用平台。
    """
    from komari_bot.plugins.komari_roulette.qq.delivery import (
        DeliveryOutcome,
        RouletteDelivery,
    )

    _engine, session_factory, manager = harness
    current = scope("tsk278-delivery-expired")
    await seed_binding(manager, current, 1)
    service = RouletteCommandService(
        session_factory=session_factory,
        reply_projector=CountingProjector(metadata={"keyboard": '{"rows": []}'}),
    )
    real_receipt = await create_waiting(service, current)

    async with session_factory() as session:
        await session.execute(
            text(
                "UPDATE komari_roulette_command_receipts "
                "SET created_at = NOW() - make_interval(secs => 301) "
                "WHERE receipt_id = :receipt_id"
            ),
            {"receipt_id": real_receipt.receipt_id},
        )
        await session.commit()
    assert await _receipt_age_seconds(session_factory, real_receipt.receipt_id) >= 300

    sender = FakeSender(result="qq-platform-expired")
    outcome = await RouletteDelivery(service=service).deliver(real_receipt, sender)

    assert outcome is DeliveryOutcome.NOT_DELIVERED
    assert sender.calls == []
    assert sender.network_calls == []
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
    assert row["state"] == "NOT_DELIVERED"
    assert row["platform_message_id"] is None


async def test_real_receipt_in_window_sends_once(
    harness: tuple[
        AsyncEngine,
        async_sessionmaker[AsyncSession],
        CharacterBindingManager,
    ],
) -> None:
    """窗口内（< 300s）仍恰好一次正常发送；边界不得误杀。"""
    from komari_bot.plugins.komari_roulette.qq.delivery import (
        DeliveryOutcome,
        RouletteDelivery,
    )

    _engine, session_factory, manager = harness
    current = scope("tsk278-delivery-window-ok")
    await seed_binding(manager, current, 1)
    service = RouletteCommandService(
        session_factory=session_factory,
        reply_projector=CountingProjector(metadata={"keyboard": '{"rows": []}'}),
    )
    real_receipt = await create_waiting(service, current)
    assert await _receipt_age_seconds(session_factory, real_receipt.receipt_id) < 300

    sender = FakeSender(result="qq-platform-window-ok")
    outcome = await RouletteDelivery(service=service).deliver(real_receipt, sender)

    assert outcome is DeliveryOutcome.DELIVERED
    assert len(sender.network_calls) == 1


async def test_real_slow_build_crossing_window_never_sends(
    harness: tuple[
        AsyncEngine,
        async_sessionmaker[AsyncSession],
        CharacterBindingManager,
    ],
) -> None:
    """构建先于 claim；若构建期间跨过 300s 边界，claim 仍须拒发。

    构建完成的时刻不预授权发送：窗口必须在 claim 时刻用 DB 时钟重新判定。
    """
    from komari_bot.plugins.komari_roulette.qq.delivery import (
        DeliveryOutcome,
        RouletteDelivery,
        build_qq_message,
    )

    _engine, session_factory, manager = harness
    current = scope("tsk278-delivery-crossing")
    await seed_binding(manager, current, 1)
    service = RouletteCommandService(
        session_factory=session_factory,
        reply_projector=CountingProjector(metadata={"keyboard": '{"rows": []}'}),
    )
    real_receipt = await create_waiting(service, current)

    # 构建开始时仍在窗口内（~299s）。
    async with session_factory() as session:
        await session.execute(
            text(
                "UPDATE komari_roulette_command_receipts "
                "SET created_at = NOW() - make_interval(secs => 299) "
                "WHERE receipt_id = :receipt_id"
            ),
            {"receipt_id": real_receipt.receipt_id},
        )
        await session.commit()

    def slow_builder(receipt: Any) -> Any:
        time.sleep(2.0)  # 阻塞事件循环：模拟构建耗时，claim 已跨过边界
        return build_qq_message(receipt)

    sender = FakeSender(result="qq-platform-crossing")
    outcome = await RouletteDelivery(
        service=service,
        payload_builder=slow_builder,
    ).deliver(real_receipt, sender)

    assert outcome is DeliveryOutcome.NOT_DELIVERED
    assert sender.calls == []
    assert sender.network_calls == []
    async with session_factory() as session:
        state = await session.scalar(
            text(
                "SELECT state FROM komari_roulette_fulfillments "
                "WHERE receipt_id = :receipt_id"
            ),
            {"receipt_id": real_receipt.receipt_id},
        )
    assert state == "NOT_DELIVERED"


# ---------------------------------------------------------------------------
# 发送前权威凭证窗口重核：service.check_fulfillment_window(claim)，DB 时钟
# ---------------------------------------------------------------------------


async def test_real_window_recheck_reads_db_clock(
    harness: tuple[
        AsyncEngine,
        async_sessionmaker[AsyncSession],
        CharacterBindingManager,
    ],
) -> None:
    """真实 seam：pending claim 窗口内 True；收据 DB 老化 >300s 后 False。

    判定完全使用 PostgreSQL 时钟（``clock_timestamp()`` 对
    ``komari_roulette_command_receipts.created_at``），不依赖本地 ``now``，
    因此不需要 sleep。
    """
    _engine, session_factory, manager = harness
    current = scope("tsk278-window-recheck-dbclock")
    await seed_binding(manager, current, 1)
    service = RouletteCommandService(
        session_factory=session_factory,
        reply_projector=CountingProjector(metadata={"keyboard": '{"rows": []}'}),
    )
    real_receipt = await create_waiting(service, current)

    claim_obj = await service.claim_fulfillment(real_receipt.receipt_id)
    assert claim_obj is not None
    assert claim_obj.state is FulfillmentState.PENDING_CONFIRMATION
    assert await service.check_fulfillment_window(claim_obj) is True

    # 仅在 DB 内让收据老化；本地时钟不动，避免 sleep/flaky。
    async with session_factory() as session:
        await session.execute(
            text(
                "UPDATE komari_roulette_command_receipts "
                "SET created_at = clock_timestamp() - make_interval(secs => 301) "
                "WHERE receipt_id = :receipt_id"
            ),
            {"receipt_id": real_receipt.receipt_id},
        )
        await session.commit()

    assert await service.check_fulfillment_window(claim_obj) is False


async def _age_receipt_seconds(
    session_factory: async_sessionmaker[AsyncSession],
    receipt_id: str,
    seconds: float,
) -> None:
    """Backdate the receipt inside the DB so the real clock sees the new age."""

    async with session_factory() as session:
        await session.execute(
            text(
                "UPDATE komari_roulette_command_receipts "
                "SET created_at = clock_timestamp() - make_interval(secs => :secs) "
                "WHERE receipt_id = :receipt_id"
            ),
            {"receipt_id": receipt_id, "secs": seconds},
        )
        await session.commit()


async def test_real_window_recheck_takes_db_clock_after_lock_wait(
    harness: tuple[
        AsyncEngine,
        async_sessionmaker[AsyncSession],
        CharacterBindingManager,
    ],
) -> None:
    """锁等待跨过 300s 时，发送前重核必须在拿到行锁后取 DB 时钟。

    另一连接只用 ``FOR UPDATE`` 持有履约行锁、不改数据（不触发 EPQ 重评），
    被阻塞的 ``check_fulfillment_window`` 若在等锁前就把
    ``clock_timestamp() - created_at`` 算成 ~299s，就会在真实年龄 ~301s 时
    仍返回 True 放行发送。断言以锁释放时刻的真实年龄为准：必须 False。
    """
    _engine, session_factory, manager = harness
    current = scope("tsk278-window-lock-wait")
    await seed_binding(manager, current, 1)
    service = RouletteCommandService(
        session_factory=session_factory,
        reply_projector=CountingProjector(metadata={"keyboard": '{"rows": []}'}),
    )
    real_receipt = await create_waiting(service, current)
    await _age_receipt_seconds(session_factory, real_receipt.receipt_id, 299)

    # claim 时刻仍在窗口内（precondition：否则下面无法构造 PENDING）。
    claim = await service.claim_fulfillment(real_receipt.receipt_id)
    assert claim is not None
    assert claim.state is FulfillmentState.PENDING_CONFIRMATION

    async with session_factory() as blocker:
        blocker_pid = await backend_pid(blocker)
        await blocker.execute(
            text(
                "SELECT receipt_id FROM komari_roulette_fulfillments "
                "WHERE receipt_id = :receipt_id FOR UPDATE"
            ),
            {"receipt_id": real_receipt.receipt_id},
        )
        task = asyncio.create_task(service.check_fulfillment_window(claim))
        try:
            await wait_for_blocked(session_factory, blocker_pid)
            await asyncio.sleep(2.0)  # 真实锁等待：收据年龄 299s → ~301s
            await blocker.rollback()  # 释放行锁，被阻塞的重核查询继续
            allowed = await asyncio.wait_for(task, timeout=5)
        finally:
            if not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await task
            with suppress(Exception):
                await blocker.rollback()

    # 锁释放时真实年龄 > 300s → 必须拒绝发送。
    assert allowed is False


async def test_real_claim_takes_db_clock_after_lock_wait(
    harness: tuple[
        AsyncEngine,
        async_sessionmaker[AsyncSession],
        CharacterBindingManager,
    ],
) -> None:
    """claim 自身在行锁上等待跨过 300s 时必须收敛为 NOT_DELIVERED。

    与发送前重核同一 SQL：另一连接只用 ``FOR UPDATE`` 持锁不改数据，claim
    若在等锁前求值 age 会以 ~299s 放行成 PENDING_CONFIRMATION；正确行为是
    拿到锁后用 DB 时钟得到 ~301s 并原子收敛为 NOT_DELIVERED。
    """
    _engine, session_factory, manager = harness
    current = scope("tsk278-claim-lock-wait")
    await seed_binding(manager, current, 1)
    service = RouletteCommandService(
        session_factory=session_factory,
        reply_projector=CountingProjector(metadata={"keyboard": '{"rows": []}'}),
    )
    real_receipt = await create_waiting(service, current)
    await _age_receipt_seconds(session_factory, real_receipt.receipt_id, 299)

    async with session_factory() as blocker:
        blocker_pid = await backend_pid(blocker)
        await blocker.execute(
            text(
                "SELECT receipt_id FROM komari_roulette_fulfillments "
                "WHERE receipt_id = :receipt_id FOR UPDATE"
            ),
            {"receipt_id": real_receipt.receipt_id},
        )
        task = asyncio.create_task(
            service.claim_fulfillment(real_receipt.receipt_id)
        )
        try:
            await wait_for_blocked(session_factory, blocker_pid)
            await asyncio.sleep(2.0)  # 真实锁等待
            await blocker.rollback()
            claim = await asyncio.wait_for(task, timeout=5)
        finally:
            if not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await task
            with suppress(Exception):
                await blocker.rollback()

    assert claim is not None
    assert claim.state is FulfillmentState.NOT_DELIVERED
    async with session_factory() as session:
        state = await session.scalar(
            text(
                "SELECT state FROM komari_roulette_fulfillments "
                "WHERE receipt_id = :receipt_id"
            ),
            {"receipt_id": real_receipt.receipt_id},
        )
    assert state == "NOT_DELIVERED"


async def test_real_slow_runtime_crossing_window_never_sends(
    harness: tuple[
        AsyncEngine,
        async_sessionmaker[AsyncSession],
        CharacterBindingManager,
    ],
) -> None:
    """claim 时刻在窗口内，runtime 重核期间收据跨过 300s → 发送前重核拒发。

    交付必须先 claim 得到正的 PENDING（窗口内），随后 runtime 重核把 DB
    ``created_at`` 改到 -301s；delivery 发送前调用真实
    ``check_fulfillment_window(claim)``（DB 时钟）→ False → 0 网络且
    NOT_DELIVERED。全程不 sleep。
    """
    from komari_bot.plugins.komari_roulette.qq.delivery import (
        DeliveryOutcome,
        RouletteDelivery,
    )

    _engine, session_factory, manager = harness
    current = scope("tsk278-delivery-window-recheck")
    await seed_binding(manager, current, 1)
    service = RouletteCommandService(
        session_factory=session_factory,
        reply_projector=CountingProjector(metadata={"keyboard": '{"rows": []}'}),
    )
    real_receipt = await create_waiting(service, current)
    assert await _receipt_age_seconds(session_factory, real_receipt.receipt_id) < 300

    async def slow_runtime() -> bool:
        async with session_factory() as session:
            await session.execute(
                text(
                    "UPDATE komari_roulette_command_receipts "
                    "SET created_at = clock_timestamp() - make_interval(secs => 301) "
                    "WHERE receipt_id = :receipt_id"
                ),
                {"receipt_id": real_receipt.receipt_id},
            )
            await session.commit()
        return True

    sender = FakeSender(result="qq-platform-window-recheck")
    outcome = await RouletteDelivery(
        service=service,
        runtime_check=slow_runtime,
    ).deliver(real_receipt, sender)

    assert outcome is DeliveryOutcome.NOT_DELIVERED
    assert sender.calls == []
    assert sender.network_calls == []
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
    assert row["state"] == "NOT_DELIVERED"
    assert row["platform_message_id"] is None
