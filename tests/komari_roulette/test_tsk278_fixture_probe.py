"""TSK-278 RED baseline: independent real-PG fixture probe.

This file is gated on ``KOMARI_TEST_POSTGRES_URL`` exactly like the existing
TSK-276 tests and imports **no TSK-278 symbol**.  It exists to prove that the
real TSK-276 PG API/SQL/helpers the TSK-278 tests rely on are healthy on their
own: receipt/fulfillment tables and columns, real claim/mark transitions,
real leaderboard rows with a frozen latest-win display name, and real
service-generated contexts with the mention priority (轮转→奖励→胜者→锁) and
原地不@.  With the gate disabled the whole file skips cleanly; with the gate
enabled any failure here is a 276/fixture regression, never a TSK-278
missing-seam false positive.
"""

from __future__ import annotations

from contextlib import suppress
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy import text

from komari_bot.plugins.character_binding.manager import CharacterBindingManager
from komari_bot.plugins.komari_roulette import (
    FulfillmentState,
    Observation,
    RouletteCommandService,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from .command_support import (
    PG_REQUIRED,
    command_factory,
    create_engine_and_factory,
    delete_scope,
    observation,
    request,
    reset_shared_orm_engine,
    scope,
    seed_binding,
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

pytestmark = [pytest.mark.asyncio, PG_REQUIRED]


@pytest.fixture
async def harness() -> AsyncIterator[
    tuple[AsyncEngine, async_sessionmaker[AsyncSession], CharacterBindingManager]
]:
    async for engine, session_factory in create_engine_and_factory():
        await reset_shared_orm_engine()
        manager = CharacterBindingManager()
        await manager.initialize()
        current = scope("tsk278-probe")
        try:
            yield engine, session_factory, manager
        finally:
            with suppress(Exception):
                await manager.close()
            await reset_shared_orm_engine()
            await delete_scope(engine, current)


# ---------------------------------------------------------------------------
# 1. Real tables / columns / helpers are present and functional
# ---------------------------------------------------------------------------


async def test_real_pg_helpers_and_tables_are_healthy(
    harness: tuple[
        AsyncEngine,
        async_sessionmaker[AsyncSession],
        CharacterBindingManager,
    ],
) -> None:
    _engine, session_factory, manager = harness
    current = scope("tsk278-probe-tables")
    await seed_binding(manager, current, 1)
    service = RouletteCommandService(
        session_factory=session_factory,
        reply_projector=CountingProjector(metadata={"keyboard": '{"rows": []}'}),
    )
    real_receipt = await create_waiting(service, current)

    # 收据/履约表与关键列真实存在。
    async with session_factory() as session:
        columns = (
            await session.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'komari_roulette_command_receipts'"
                )
            )
        ).scalars().all()
    for expected in (
        "receipt_id",
        "app_id",
        "group_openid",
        "inbound_msg_id",
        "fingerprint",
        "result_code",
        "game_id",
        "state_revision",
        "turn_seq",
        "reply_projection",
        "created_at",
    ):
        assert expected in columns, f"missing column {expected}"

    async with session_factory() as session:
        fulfillment_cols = (
            await session.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'komari_roulette_fulfillments'"
                )
            )
        ).scalars().all()
    for expected in ("receipt_id", "state", "platform_message_id", "updated_at"):
        assert expected in fulfillment_cols, f"missing column {expected}"

    # 每次领域命令都落一条履约行，初始 NOT_STARTED。
    async with session_factory() as session:
        state = await session.scalar(
            text(
                "SELECT state FROM komari_roulette_fulfillments "
                "WHERE receipt_id = :receipt_id"
            ),
            {"receipt_id": real_receipt.receipt_id},
        )
    assert state == FulfillmentState.NOT_STARTED.value


# ---------------------------------------------------------------------------
# 2. Real claim/mark transitions
# ---------------------------------------------------------------------------


async def test_real_claim_and_mark_transitions(
    harness: tuple[
        AsyncEngine,
        async_sessionmaker[AsyncSession],
        CharacterBindingManager,
    ],
) -> None:
    _engine, session_factory, manager = harness
    current = scope("tsk278-probe-claim")
    await seed_binding(manager, current, 1)
    service = RouletteCommandService(
        session_factory=session_factory,
        reply_projector=CountingProjector(metadata={"keyboard": '{"rows": []}'}),
    )
    receipt_a = await create_waiting(
        service, current, message_id="probe-a"
    )
    receipt_b = await create_waiting(
        service, current, message_id="probe-b"
    )

    claim_a = await service.claim_fulfillment(receipt_a.receipt_id)
    assert claim_a is not None
    assert claim_a.state == FulfillmentState.PENDING_CONFIRMATION
    # 已领取后再次领取 → None（重复事件幂等）。
    assert await service.claim_fulfillment(receipt_a.receipt_id) is None

    await service.mark_delivered(claim_a, platform_message_id="qq-probe-1")
    claim_b = await service.claim_fulfillment(receipt_b.receipt_id)
    assert claim_b is not None
    await service.mark_not_delivered(claim_b)

    async with session_factory() as session:
        rows = {
            row[0]: (row[1], row[2])
            for row in (
                await session.execute(
                    text(
                        "SELECT receipt_id, state, platform_message_id "
                        "FROM komari_roulette_fulfillments "
                        "WHERE receipt_id IN (:a, :b)"
                    ),
                    {"a": receipt_a.receipt_id, "b": receipt_b.receipt_id},
                )
            )
        }
    assert rows[receipt_a.receipt_id] == (
        FulfillmentState.DELIVERED.value,
        "qq-probe-1",
    )
    assert rows[receipt_b.receipt_id] == (
        FulfillmentState.NOT_DELIVERED.value,
        None,
    )


# ---------------------------------------------------------------------------
# 3. Real leaderboard: frozen latest-win display name survives rename
# ---------------------------------------------------------------------------


async def test_real_leaderboard_frozen_latest_win_name(
    harness: tuple[
        AsyncEngine,
        async_sessionmaker[AsyncSession],
        CharacterBindingManager,
    ],
) -> None:
    _engine, session_factory, manager = harness
    current = scope("tsk278-probe-leaderboard")
    members = await seed_players(manager, current, 2)
    projector = CountingProjector(metadata={"keyboard": '{"rows": []}'})
    service = RouletteCommandService(
        session_factory=session_factory,
        reply_projector=projector,
    )
    await create_waiting(service, current)
    await join_player(service, current, members[1], "probe-join-2")
    started = await start_game(service, current, members[0])
    assert started.result_code == "started"

    # 局主弃权 → 另一名玩家获胜；排行榜写入冻结显示名。
    forfeit_row = await current_game_row(session_factory, current)
    assert forfeit_row is not None
    forfeited = await service.execute_group_command(
        request(
            current,
            "probe-forfeit",
            command_factory("forfeit"),
            member_openid=members[0],
        ),
        observation=observation(
            game_id=str(forfeit_row["game_id"]),
            state_revision=int(forfeit_row["state_revision"]),
            turn_seq=int(forfeit_row["turn_seq"]),
        ),
    )
    assert forfeited.result_code in {"forfeited", "completed"}
    assert projector.context_objects[-1].winner_group_wins == 1

    async with session_factory() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT display_name, wins, member_openid "
                    "FROM komari_roulette_leaderboard "
                    "WHERE app_id = :app_id AND group_openid = :group_openid"
                ),
                {
                    "app_id": current.app_id,
                    "group_openid": current.group_openid,
                },
            )
        ).mappings().all()
    assert len(rows) == 1
    frozen_name = rows[0]["display_name"]
    assert rows[0]["member_openid"] == members[1]

    # 改名后排行榜仍显示冻结名。
    await seed_binding(manager, current, 2, name="新昵称")
    async with session_factory() as session:
        after = (
            await session.execute(
                text(
                    "SELECT display_name, wins "
                    "FROM komari_roulette_leaderboard "
                    "WHERE app_id = :app_id AND group_openid = :group_openid"
                ),
                {
                    "app_id": current.app_id,
                    "group_openid": current.group_openid,
                },
            )
        ).mappings().all()
    assert len(after) == 1
    assert after[0]["display_name"] == frozen_name
    assert after[0]["wins"] == 1


async def test_real_leaderboard_details_encoding_with_colon_name(
    harness: tuple[
        AsyncEngine,
        async_sessionmaker[AsyncSession],
        CharacterBindingManager,
    ],
) -> None:
    """真实 276 details 的 leaderboard 条目编码恒为 ``{冻结名}:{N}``：
    冻结名含冒号时仍保留完整名字，只有最后一个冒号后是胜场数。"""
    _engine, session_factory, manager = harness
    current = scope("tsk278-probe-leaderboard-colon")
    members = await seed_players(manager, current, 2)
    await seed_binding(manager, current, 2, name="小红:小明")
    projector = CountingProjector(metadata={"keyboard": '{"rows": []}'})
    service = RouletteCommandService(
        session_factory=session_factory,
        reply_projector=projector,
    )
    await create_waiting(service, current)
    await join_player(service, current, members[1], "probe-colon-join-2")
    await start_game(service, current, members[0])
    forfeit_row = await current_game_row(session_factory, current)
    assert forfeit_row is not None
    forfeited = await service.execute_group_command(
        request(
            current,
            "probe-colon-forfeit",
            command_factory("forfeit"),
            member_openid=members[0],
        ),
        observation=observation(
            game_id=str(forfeit_row["game_id"]),
            state_revision=int(forfeit_row["state_revision"]),
            turn_seq=int(forfeit_row["turn_seq"]),
        ),
    )
    assert forfeited.result_code in {"forfeited", "completed"}

    listed = await service.execute_group_command(
        request(
            current,
            "probe-colon-leaderboard",
            command_factory("leaderboard"),
            member_openid=members[1],
        )
    )
    assert listed.result_code == "leaderboard"
    ctx = projector.context_objects[-1]
    assert ctx.details.get("leaderboard") == ("小红:小明:1",)


# ---------------------------------------------------------------------------
# 4. Real service contexts: mention priority 轮转→奖励→胜者→锁 and 原地不@
# ---------------------------------------------------------------------------


async def test_real_context_mention_priority_and_continue_in_place(
    harness: tuple[
        AsyncEngine,
        async_sessionmaker[AsyncSession],
        CharacterBindingManager,
    ],
) -> None:
    _engine, session_factory, manager = harness
    current = scope("tsk278-probe-mention")
    members = await seed_players(manager, current, 2)
    entropy = CountingRandom()
    projector = CountingProjector(metadata={"keyboard": '{"rows": []}'})
    service = RouletteCommandService(
        session_factory=session_factory,
        reply_projector=projector,
        random_source=entropy,
    )
    await create_waiting(service, current)
    await join_player(service, current, members[1], "probe-join-2")
    await start_game(service, current, members[0])

    def last_context() -> Any:
        return projector.context_objects[-1]

    async def game_observation() -> Observation:
        row = await current_game_row(session_factory, current)
        assert row is not None
        return observation(
            game_id=str(row["game_id"]),
            state_revision=int(row["state_revision"]),
            turn_seq=int(row["turn_seq"]),
        )

    # 原地不@：actor == current，空弹继续行动 → 无提及。
    shot = await service.execute_group_command(
        request(
            current,
            "probe-shot-1",
            command_factory("shoot"),
            member_openid=members[0],
        ),
        observation=await game_observation(),
    )
    assert shot.result_code == "shot"
    ctx = last_context()
    assert ctx.mention_target is None
    assert ctx.mention_reason is None
    assert ctx.current_player is not None
    assert ctx.current_player.member_openid == members[0]

    # 轮转：结束回合 → 下一名玩家成为当前玩家，@ 新当前玩家。
    ended = await service.execute_group_command(
        request(
            current,
            "probe-end-turn",
            command_factory("end_turn"),
            member_openid=members[0],
        ),
        observation=await game_observation(),
    )
    assert ended.result_code == "turn_ended"
    ctx = last_context()
    assert ctx.mention_reason == "turn"
    assert ctx.mention_target is not None
    assert ctx.mention_target.member_openid == members[1]
    assert ctx.current_player is not None
    assert ctx.current_player.member_openid == members[1]

    # 锁：对目标使用锁 → @ 锁目标（当前玩家对非当前玩家使用）。
    before = await current_game_row(session_factory, current)
    assert before is not None
    async with session_factory() as session:
        await session.execute(
            text(
                "UPDATE komari_roulette_players SET lock_count = 1 "
                "WHERE game_id = :game_id AND join_seq = 2"
            ),
            {"game_id": before["game_id"]},
        )
        await session.commit()
    locked = await service.execute_group_command(
        request(
            current,
            "probe-lock",
            command_factory("use_item", item="lock", target_player_seq=1),
            member_openid=members[1],
        ),
        observation=observation(
            game_id=str(before["game_id"]),
            state_revision=int(before["state_revision"]),
            turn_seq=int(before["turn_seq"]),
        ),
    )
    assert locked.result_code == "item_used"
    ctx = last_context()
    assert ctx.mention_reason == "lock_target"
    assert ctx.mention_target is not None
    assert ctx.mention_target.member_openid == members[0]

    # 奖励：待处理奖励选择 → @ 奖励玩家（当前玩家）。
    before = await current_game_row(session_factory, current)
    assert before is not None
    item_observation = observation(
        game_id=str(before["game_id"]),
        state_revision=int(before["state_revision"]),
        turn_seq=int(before["turn_seq"]),
    )
    async with session_factory() as session:
        await session.execute(
            text(
                "UPDATE komari_roulette_games "
                "SET phase = 'item_choice', "
                "pending_rewards = ARRAY['beer', 'lock']::text[] "
                "WHERE game_id = :game_id"
            ),
            {"game_id": before["game_id"]},
        )
        await session.execute(
            text(
                "UPDATE komari_roulette_players "
                "SET magnifier_count = 1, beer_count = 1, "
                "burst_count = 1, lock_count = 1 "
                "WHERE game_id = :game_id AND join_seq = 2"
            ),
            {"game_id": before["game_id"]},
        )
        await session.commit()
    chosen = await service.execute_group_command(
        request(
            current,
            "probe-choose",
            command_factory("choose_item", decision="discard"),
            member_openid=members[1],
        ),
        observation=item_observation,
    )
    assert chosen.result_code == "item_choice_updated"
    ctx = last_context()
    assert ctx.mention_reason == "reward"
    assert ctx.mention_target is not None
    assert ctx.mention_target.member_openid == members[1]

    # 胜者：弃权终结 → @ 唯一胜者（当前玩家为空，胜者优先于锁目标）。
    won = await service.execute_group_command(
        request(
            current,
            "probe-forfeit",
            command_factory("forfeit"),
            member_openid=members[1],
        ),
        observation=await game_observation(),
    )
    assert won.result_code in {"forfeited", "completed"}
    ctx = last_context()
    assert ctx.mention_reason == "winner"
    assert ctx.mention_target is not None
    assert ctx.mention_target.member_openid == members[0]
    assert ctx.current_player is None
