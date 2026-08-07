"""可选的好感度 operation 幂等 PostgreSQL 集成测试。

依赖已执行 alembic upgrade head 的迁移管理 schema；测试使用唯一 user_id /
operation_id 并通过 SQLModel 会话清理写入的行。并发重复投递同一
operation_id 只应用一次，两个调用拿到同一份结果。
"""

from __future__ import annotations

import asyncio
import os
from contextlib import suppress
from typing import TYPE_CHECKING
from urllib.parse import urlparse
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select

from komari_bot.plugins.user_data.config_schema import DynamicConfigSchema
from komari_bot.plugins.user_data.database import UserDataDB
from komari_bot.plugins.user_data.orm_models import (
    UserFavorabilityAdjustmentLedgerRow,
    UserFavorabilityRow,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

POSTGRES_URL = os.getenv("KOMARI_TEST_POSTGRES_URL", "")


def _configured_database_url() -> str:
    from nonebot import get_driver

    return str(getattr(get_driver().config, "sqlalchemy_database_url", "") or "")


def _same_database(left: str, right: str) -> bool:
    left_parsed = urlparse(left)
    right_parsed = urlparse(right)
    return (
        left_parsed.hostname == right_parsed.hostname
        and (left_parsed.port or 5432) == (right_parsed.port or 5432)
        and left_parsed.path == right_parsed.path
    )


async def _reset_shared_orm_engine() -> None:
    """清空 nonebot-plugin-orm 共享引擎连接池（每个测试独立事件循环）。"""
    from nonebot import require

    require("nonebot_plugin_orm")
    import nonebot_plugin_orm as orm_module

    engines = getattr(orm_module, "_engines", None)
    if not engines:
        return
    for engine in list(engines.values()):
        with suppress(Exception):
            await engine.dispose()


def _open_session() -> "AsyncSession":
    from nonebot_plugin_orm import get_session

    return get_session(expire_on_commit=False)


@pytest.mark.skipif(not POSTGRES_URL, reason="未配置真实 PostgreSQL 测试连接")
@pytest.mark.asyncio
async def test_adjustment_operation_is_applied_once_under_concurrency() -> None:
    if not _same_database(POSTGRES_URL, _configured_database_url()):
        pytest.skip(
            "KOMARI_TEST_POSTGRES_URL 与 nonebot sqlalchemy_database_url 不一致"
        )
    run_id = uuid4().hex
    user_id = f"user-{run_id}"
    operation_id = f"reply-operation-{run_id}:favorability"
    await _reset_shared_orm_engine()
    try:
        database = UserDataDB(DynamicConfigSchema(initial_favorability=100))
        await database.initialize()

        first, duplicate = await asyncio.gather(
            database.adjust_user_favorability(
                user_id,
                5,
                operation_id=operation_id,
            ),
            database.adjust_user_favorability(
                user_id,
                5,
                operation_id=operation_id,
            ),
        )

        assert first.before == duplicate.before == 100
        assert first.after == duplicate.after == 105
        session = _open_session()
        try:
            score = (
                await session.execute(
                    select(UserFavorabilityRow.__table__.c.favorability).where(
                        UserFavorabilityRow.__table__.c.user_id == user_id
                    )
                )
            ).scalar_one()
            ledger_count = (
                await session.execute(
                    select(func.count())
                    .select_from(UserFavorabilityAdjustmentLedgerRow)
                    .where(
                        UserFavorabilityAdjustmentLedgerRow.__table__.c.operation_id
                        == operation_id
                    )
                )
            ).scalar_one()
        finally:
            await session.close()
        assert score == 105
        assert ledger_count == 1
    finally:
        session = _open_session()
        try:
            await session.execute(
                delete(UserFavorabilityAdjustmentLedgerRow).where(
                    UserFavorabilityAdjustmentLedgerRow.__table__.c.operation_id
                    == operation_id
                )
            )
            await session.execute(
                delete(UserFavorabilityRow).where(
                    UserFavorabilityRow.__table__.c.user_id == user_id
                )
            )
            await session.commit()
        finally:
            await session.close()
        await _reset_shared_orm_engine()
