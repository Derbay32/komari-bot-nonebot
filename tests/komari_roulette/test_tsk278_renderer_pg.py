# ruff: noqa: RUF001
"""TSK-278: real service → real renderer end-to-end projections.

Gated on ``KOMARI_TEST_POSTGRES_URL``.  Unlike ``test_tsk278_delivery_pg.py``
(which substitutes a stand-in projector), this file wires the REAL
``komari_roulette.qq.renderer.render_reply`` as the service projector, so the
full chain domain → persisted snapshot → ``ReplyProjectionContext`` →
TSK-266 copy → frozen receipt body runs against PostgreSQL.

The cases are the confirmed TSK-266 decisions that only show up when the real
domain state is projected: shoot follow-up, active timeout rotation, lock
result, reward choice, and the three waiting-end copies (host cancel, last
player leave, leave after start).
"""

from __future__ import annotations

from contextlib import suppress
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy import text

from komari_bot.plugins.character_binding.manager import CharacterBindingManager
from komari_bot.plugins.komari_roulette import RouletteCommandService
from komari_bot.plugins.komari_roulette.domain import ItemType
from komari_bot.plugins.komari_roulette.qq.renderer import render_reply

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping, Sequence

from .command_support import (
    PG_REQUIRED,
    Scope,
    command_factory,
    create_engine_and_factory,
    delete_scope,
    observation,
    request,
    reset_shared_orm_engine,
    scope,
)
from .test_command_service import (
    CountingRandom,
    Harness,
    create_waiting,
    current_game_row,
    join_player,
    seed_players,
    start_game,
)

pytestmark = [pytest.mark.asyncio, PG_REQUIRED]


@pytest.fixture
async def harness() -> AsyncIterator[Harness]:
    async for engine, session_factory in create_engine_and_factory():
        await reset_shared_orm_engine()
        manager = CharacterBindingManager()
        await manager.initialize()
        try:
            yield Harness(engine, session_factory, manager)
        finally:
            with suppress(Exception):
                await manager.close()
            await reset_shared_orm_engine()


def _service(harness: Harness) -> RouletteCommandService:
    """Real renderer as the projector, deterministic chamber/random source."""

    return RouletteCommandService(
        session_factory=harness.session_factory,
        reply_projector=render_reply,
        random_source=CountingRandom(),
    )


async def _started_game(
    harness: Harness,
    current: Scope,
    members: Sequence[str],
) -> RouletteCommandService:
    service = _service(harness)
    await create_waiting(service, current)
    for number, member in enumerate(members[1:], start=2):
        await join_player(service, current, member, f"join-{number}")
    await start_game(service, current, members[0])
    return service


async def _row(
    harness: Harness,
    current: Scope,
) -> Mapping[str, Any]:
    row = await current_game_row(harness.session_factory, current)
    assert row is not None
    return row


def _observation(row: Mapping[str, Any]) -> Any:
    return observation(
        game_id=str(row["game_id"]),
        state_revision=int(row["state_revision"]),
        turn_seq=int(row["turn_seq"]),
    )


async def _execute(
    service: RouletteCommandService,
    harness: Harness,
    current: Scope,
    message_id: str,
    command: Any,
    *,
    member_openid: str,
) -> Any:
    """Execute a real command with the current persisted observation.

    Passing an observation is harmless for intents outside
    ``OBSERVED_ACTIVE_WRITES`` and required for the observed ones.
    """

    row = await _row(harness, current)
    return await service.execute_group_command(
        request(current, message_id, command, member_openid=member_openid),
        observation=_observation(row),
    )


async def test_real_shoot_follow_up_body_uses_authoritative_board(
    harness: Harness,
) -> None:
    """继续行动：块引用结果句 + 当前玩家 + 弹仓 + 分割线 + 玩家列表。"""
    current = scope("tsk278-renderer-shoot")
    members = await seed_players(harness.binding_manager, current, 2)
    service = await _started_game(harness, current, members)

    receipt = await _execute(
        service,
        harness,
        current,
        "shoot-1",
        command_factory("shoot"),
        member_openid=members[0],
    )

    assert receipt.result_code == "shot"
    body = receipt.reply.body
    assert body.startswith("> Seat 1打出一发空弹。"), body
    # 弹仓：初始 2 实弹/4 空弹，打掉一发空弹 → 5/6、实弹 2、空弹 3、40%。
    assert "**当前：Seat 1**" in body
    assert "弹仓：**5/6**｜实弹 **2**｜空弹 **3**｜中弹概率 **40%**" in body
    assert "- **Seat 1**｜当前｜道具 0" in body
    assert "- Seat 2｜存活｜道具 0" in body

    await delete_scope(harness.engine, current)


async def test_real_active_timeout_appends_rotation_after_fixed_notice(
    harness: Harness,
) -> None:
    """TSK-266 11.3：进行中的超时淘汰是固定提示 + 轮转，不是单句错误。

    3 人局淘汰当前玩家后仍有 2 人存活，因此提交的是轮转：固定超时提示后
    必须附上新的权威局面（当前玩家 / 弹仓 / 名单），且不得追加排行榜。
    """
    current = scope("tsk278-renderer-timeout")
    members = await seed_players(harness.binding_manager, current, 3)
    service = await _started_game(harness, current, members)
    row = await _row(harness, current)

    async with harness.session_factory() as session:
        await session.execute(
            text(
                "UPDATE komari_roulette_games "
                "SET turn_deadline_at = NOW() - INTERVAL '1 minute' "
                "WHERE game_id = :game_id"
            ),
            {"game_id": str(row["game_id"])},
        )
        await session.commit()

    receipt = await _execute(
        service,
        harness,
        current,
        "end-turn-1",
        command_factory("end_turn"),
        member_openid=members[0],
    )

    assert receipt.result_code == "turn_expired"
    body = receipt.reply.body
    fixed = "你的行动时间已经结束，本次命令未执行。"
    assert body.startswith(fixed), body
    assert body != fixed, body
    assert "排行榜" not in body
    # 轮转到下一位存活玩家（稳定编号 2），被淘汰的编号 1 显示为出局。
    assert "**当前：Seat 2**" in body
    assert "- Seat 1｜出局｜道具 0" in body
    assert "- **Seat 2**｜当前｜道具 0" in body
    assert "- Seat 3｜存活｜道具 0" in body

    await delete_scope(harness.engine, current)


async def test_real_lock_result_keeps_single_sentence_and_mention(
    harness: Harness,
) -> None:
    """成功上锁：只保留上锁句（含真实提及），后接权威局面与分割线。"""
    current = scope("tsk278-renderer-lock")
    members = await seed_players(harness.binding_manager, current, 2)
    service = await _started_game(harness, current, members)
    row = await _row(harness, current)

    async with harness.session_factory() as session:
        await session.execute(
            text(
                "UPDATE komari_roulette_players SET lock_count = 1 "
                "WHERE game_id = :game_id AND join_seq = 1"
            ),
            {"game_id": str(row["game_id"])},
        )
        await session.commit()

    receipt = await _execute(
        service,
        harness,
        current,
        "lock-1",
        command_factory("use_item", item=ItemType.LOCK, target_player_seq=2),
        member_openid=members[0],
    )

    assert receipt.result_code == "item_used"
    body = receipt.reply.body
    assert body.startswith(
        f'> Seat 1对Seat 2（<qqbot-at-user id="{members[1]}" />）使用了锁。'
    ), body
    assert "**当前：Seat 1**" in body
    assert "- Seat 2｜存活｜道具 0｜待锁" in body

    await delete_scope(harness.engine, current)


async def test_real_reward_choice_body_matches_confirmed_copy(
    harness: Harness,
) -> None:
    """奖励选择：满库存的第二次空弹进入 item_choice，正文使用定稿文案。"""
    current = scope("tsk278-renderer-reward")
    members = await seed_players(harness.binding_manager, current, 2)
    service = await _started_game(harness, current, members)
    row = await _row(harness, current)

    # 当前玩家满库存（4 件），下一次空弹奖励必然进入待处理队列。
    async with harness.session_factory() as session:
        await session.execute(
            text(
                "UPDATE komari_roulette_players "
                "SET magnifier_count = 1, beer_count = 1, "
                "burst_count = 1, lock_count = 1 "
                "WHERE game_id = :game_id AND join_seq = 1"
            ),
            {"game_id": str(row["game_id"])},
        )
        await session.commit()

    first = await _execute(
        service,
        harness,
        current,
        "shoot-1",
        command_factory("shoot"),
        member_openid=members[0],
    )
    assert first.result_code == "shot"

    second = await _execute(
        service,
        harness,
        current,
        "shoot-2",
        command_factory("shoot"),
        member_openid=members[0],
    )

    assert second.result_code == "item_choice_pending"
    body = second.reply.body
    assert "道具列表已满，选择一项来替换。" in body
    assert "当前新道具：**啤酒**" in body
    assert "已有道具：放大镜 ×1、啤酒 ×1、连发器 ×1、锁 ×1" in body
    assert "**当前：Seat 1**" in body

    await delete_scope(harness.engine, current)


async def test_real_host_cancel_renders_confirmed_waiting_end_copy(
    harness: Harness,
) -> None:
    """TSK-266 1G（评论 16 去编号）：局主取消的结束文案。"""
    current = scope("tsk278-renderer-cancel")
    members = await seed_players(harness.binding_manager, current, 2)
    service = _service(harness)
    await create_waiting(service, current)
    await join_player(service, current, members[1], "join-2")

    receipt = await _execute(
        service,
        harness,
        current,
        "cancel-1",
        command_factory("cancel"),
        member_openid=members[0],
    )

    assert receipt.result_code == "cancelled"
    assert (
        receipt.reply.body
        == "Seat 1取消了这局游戏，等候中的玩家已经全部离席。"
    ), receipt.reply.body

    await delete_scope(harness.engine, current)


async def test_real_last_player_leave_renders_confirmed_waiting_end_copy(
    harness: Harness,
) -> None:
    """TSK-266 1G（评论 16 去编号）：最后一名玩家退出的结束文案。"""
    current = scope("tsk278-renderer-last-leave")
    members = await seed_players(harness.binding_manager, current, 1)
    service = _service(harness)
    await create_waiting(service, current)

    receipt = await _execute(
        service,
        harness,
        current,
        "leave-1",
        command_factory("leave"),
        member_openid=members[0],
    )

    assert receipt.result_code == "cancelled"
    assert (
        receipt.reply.body == "Seat 1离开后，等候局已自动结束。"
    ), receipt.reply.body

    await delete_scope(harness.engine, current)


async def test_real_leave_after_start_renders_phase_specific_copy(
    harness: Harness,
) -> None:
    """TSK-266 11.3：开始后使用“退出”的专属文案，而非 11.2 通用文案。"""
    current = scope("tsk278-renderer-leave-active")
    members = await seed_players(harness.binding_manager, current, 2)
    service = await _started_game(harness, current, members)

    receipt = await _execute(
        service,
        harness,
        current,
        "leave-1",
        command_factory("leave"),
        member_openid=members[0],
    )

    assert receipt.result_code == "game_already_started"
    body = receipt.reply.body
    assert body.startswith("游戏已经开始，“退出”只用于等候阶段；"), body
    assert "/轮盘 弃权" in body
    assert "无法再改变等候阵容" not in body

    await delete_scope(harness.engine, current)
