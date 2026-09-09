"""TSK-280 夹具/辅助独立验证（不 import 未实现的 repair 模块）。

「缺失业务模块」的 ImportError 只证明业务代码缺失，不能证明夹具正确。
本文件在无 repair 模块时即可运行，独立验证 TSK-280 用到的真实 PG 辅助：
轮盘终局 seam（``persist_completed_game``）、作用域统计
（``roulette_counts``）、清理 SQL（对照真实表结构/列名）、提交失败注入
（真实 SQLAlchemy ``before_commit`` 事件，先健康验证）与共享组锁等待辅助。
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import text

from komari_bot.plugins.character_binding.manager import CharacterBindingManager
from tests.character_binding.tsk280_support import (
    PG_REQUIRED,
    CommitFailureSwitch,
    backend_pid,
    clear_roulette_scope,
    create_engine_and_factory,
    health_check_commit_failure_switch,
    hold_group_lock,
    persist_completed_game,
    reset_shared_orm_engine,
    roulette_counts,
    seed_binding,
    wait_for_blocked_count,
)
from tests.character_binding.tsk280_support import (
    scope as make_scope,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

pytestmark = [pytest.mark.asyncio, PG_REQUIRED]


@dataclass(frozen=True, slots=True)
class Harness:
    """与 test_tsk280_pg 同款的独立真实引擎 + 管理器（不含 repair）。"""

    engine: AsyncEngine
    session_factory: async_sessionmaker[AsyncSession]
    binding_manager: CharacterBindingManager


@pytest.fixture
async def harness() -> AsyncIterator[Harness]:
    async for engine, session_factory in create_engine_and_factory():
        manager = CharacterBindingManager()
        await manager.initialize()
        try:
            yield Harness(engine, session_factory, manager)
        finally:
            with suppress(Exception):
                await manager.close()
            await reset_shared_orm_engine()


async def test_roulette_completed_game_fixture_and_counts(
    harness: Harness,
) -> None:
    """终局 seam 真实可走通：shoot 需观测、2 人局首发 live 即终局。"""
    current = make_scope("probe-roulette")
    await seed_binding(harness.binding_manager, current, 1, name="甲")
    await seed_binding(harness.binding_manager, current, 2, name="乙")
    game_id = await persist_completed_game(harness.session_factory, current)

    counts = await roulette_counts(harness.engine, current)
    assert counts["games"] >= 1
    assert counts["results"] == 1
    assert counts["players"] == 2
    assert counts["wins"] == 1

    async with harness.session_factory() as session:
        row = (
            await session.execute(
                text(
                    """
                    SELECT lifecycle, winner_member_openid
                      FROM komari_roulette_results
                     WHERE game_id = :game_id
                    """
                ),
                {"game_id": game_id},
            )
        ).one()
    assert row[0] == "completed"
    assert row[1] is not None

    await clear_roulette_scope(harness.engine, current)
    after = await roulette_counts(harness.engine, current)
    assert after == {"games": 0, "results": 0, "players": 0, "wins": 0}


async def test_commit_failure_switch_health_check(harness: Harness) -> None:
    """提交失败注入必须先健康验证：未武装可提交、武装必失败且不落库。"""
    switch = CommitFailureSwitch()
    await health_check_commit_failure_switch(harness.session_factory, switch)
    assert switch.raised == 1


async def test_group_lock_wait_helper_is_bounded_and_clean(harness: Harness) -> None:
    """共享组锁 + 等待辅助真实可用；等待者在锁释放后继续、无泄漏。"""
    current = make_scope("probe-lock")

    async def _waiter() -> None:
        async with harness.session_factory() as session:
            await hold_group_lock(session, current)

    async with harness.session_factory() as blocker:
        await blocker.begin()
        blocker_pid = await backend_pid(blocker)
        await hold_group_lock(blocker, current)
        task = asyncio.create_task(_waiter())
        try:
            await wait_for_blocked_count(
                harness.session_factory, blocker_pid, min_count=1
            )
        finally:
            await blocker.rollback()
        await asyncio.wait_for(task, timeout=5)
    assert task.done()
