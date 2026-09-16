"""跨测试目录共享的低层 PostgreSQL 辅助。

只承载被多个测试目录复用的真实 PG 操作：nonebot-plugin-orm 共享引擎
归还、后端 PID 读取、锁等待者有界等待。作用域、门控与业务夹具仍归各
测试目录自己的 support 模块。
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import TYPE_CHECKING

from sqlalchemy import text

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


async def reset_shared_orm_engine() -> None:
    """Dispose nonebot-plugin-orm engines before crossing pytest event loops."""

    from nonebot import require

    require("nonebot_plugin_orm")
    import nonebot_plugin_orm as orm_module

    engines = getattr(orm_module, "_engines", None)
    if not engines:
        return
    for engine in list(engines.values()):
        with suppress(Exception):
            await engine.dispose()


async def backend_pid(session: AsyncSession) -> int:
    return int(await session.scalar(text("SELECT pg_backend_pid()")))


async def wait_for_blocked(
    session_factory: async_sessionmaker[AsyncSession],
    blocker_pid: int,
    *,
    min_count: int = 1,
) -> None:
    """Wait for at least ``min_count`` real PostgreSQL lock waiters."""

    async with asyncio.timeout(5):
        while True:
            async with session_factory() as session:
                blocked = await session.scalar(
                    text(
                        "SELECT count(*) FROM pg_stat_activity "
                        "WHERE :blocker = ANY(pg_blocking_pids(pid))"
                    ),
                    {"blocker": blocker_pid},
                )
            if int(blocked or 0) >= min_count:
                return
            await asyncio.sleep(0.02)
