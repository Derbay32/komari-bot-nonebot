"""user_data ORM 仓储 PostgreSQL 集成测试（依赖已 alembic upgrade head 的 schema）。

覆盖原 asyncpg fake 池单测的行为契约：首次读取建档、读取不覆盖既有值、
set 的 before 语义与边界值、adjust 的钳制与账本、账本清理保留期、
总用户数与未初始化保护。数据准备与清理走同一套 SQLModel 表模型，
断言全部通过仓储公共接口。
"""

from __future__ import annotations

import os
from contextlib import suppress
from datetime import UTC, datetime, timedelta
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


def _make_user_id() -> str:
    return f"favor-{uuid4().hex}"


def _build_db(initial_favorability: int = 0) -> UserDataDB:
    return UserDataDB(DynamicConfigSchema(initial_favorability=initial_favorability))


async def _insert_rows(rows: list[object]) -> None:
    session = _open_session()
    try:
        session.add_all(rows)
        await session.commit()
    finally:
        await session.close()


async def _cleanup(user_ids: list[str], operation_ids: list[str] | None = None) -> None:
    session = _open_session()
    try:
        if user_ids:
            await session.execute(
                delete(UserFavorabilityRow).where(
                    UserFavorabilityRow.__table__.c.user_id.in_(user_ids)
                )
            )
        op_ids = operation_ids or []
        if op_ids:
            await session.execute(
                delete(UserFavorabilityAdjustmentLedgerRow).where(
                    UserFavorabilityAdjustmentLedgerRow.__table__.c.operation_id.in_(
                        op_ids
                    )
                )
            )
        await session.commit()
    finally:
        await session.close()


async def _fetch_score(user_id: str) -> int:
    session = _open_session()
    try:
        value = (
            await session.execute(
                select(UserFavorabilityRow.__table__.c.favorability).where(
                    UserFavorabilityRow.__table__.c.user_id == user_id
                )
            )
        ).scalar_one_or_none()
    finally:
        await session.close()
    if value is None:
        msg = f"测试期望存在好感度行: {user_id}"
        raise AssertionError(msg)
    return int(value)


@pytest.mark.skipif(not POSTGRES_URL, reason="未配置真实 PostgreSQL 测试连接")
@pytest.mark.asyncio
async def test_get_creates_initial_row_on_first_read() -> None:
    if not _same_database(POSTGRES_URL, _configured_database_url()):
        pytest.skip(
            "KOMARI_TEST_POSTGRES_URL 与 nonebot sqlalchemy_database_url 不一致"
        )
    user_id = _make_user_id()
    await _reset_shared_orm_engine()
    try:
        database = _build_db(initial_favorability=100)
        await database.initialize()

        result = await database.get_user_favorability(user_id)

        assert result.user_id == user_id
        assert result.favorability == 100
        assert result.stage_index == 2
        assert await _fetch_score(user_id) == 100
    finally:
        await _cleanup([user_id])
        await _reset_shared_orm_engine()


@pytest.mark.skipif(not POSTGRES_URL, reason="未配置真实 PostgreSQL 测试连接")
@pytest.mark.asyncio
async def test_get_returns_stored_value_without_overwriting() -> None:
    if not _same_database(POSTGRES_URL, _configured_database_url()):
        pytest.skip(
            "KOMARI_TEST_POSTGRES_URL 与 nonebot sqlalchemy_database_url 不一致"
        )
    user_id = _make_user_id()
    await _reset_shared_orm_engine()
    await _insert_rows([UserFavorabilityRow(user_id=user_id, favorability=250)])
    try:
        database = _build_db(initial_favorability=0)
        await database.initialize()

        result = await database.get_user_favorability(user_id)

        assert result.favorability == 250
        assert result.stage_index == 3
        assert await _fetch_score(user_id) == 250
    finally:
        await _cleanup([user_id])
        await _reset_shared_orm_engine()


@pytest.mark.skipif(not POSTGRES_URL, reason="未配置真实 PostgreSQL 测试连接")
@pytest.mark.asyncio
async def test_set_new_user_uses_initial_favorability_as_before() -> None:
    if not _same_database(POSTGRES_URL, _configured_database_url()):
        pytest.skip(
            "KOMARI_TEST_POSTGRES_URL 与 nonebot sqlalchemy_database_url 不一致"
        )
    user_id = _make_user_id()
    await _reset_shared_orm_engine()
    try:
        database = _build_db(initial_favorability=100)
        await database.initialize()

        result = await database.set_user_favorability(user_id, 200)

        assert result.before == 100
        assert result.after == 200
        assert result.stage_index == 3
        assert await _fetch_score(user_id) == 200
    finally:
        await _cleanup([user_id])
        await _reset_shared_orm_engine()


@pytest.mark.skipif(not POSTGRES_URL, reason="未配置真实 PostgreSQL 测试连接")
@pytest.mark.asyncio
async def test_set_existing_row_preserves_actual_before() -> None:
    if not _same_database(POSTGRES_URL, _configured_database_url()):
        pytest.skip(
            "KOMARI_TEST_POSTGRES_URL 与 nonebot sqlalchemy_database_url 不一致"
        )
    user_id = _make_user_id()
    await _reset_shared_orm_engine()
    await _insert_rows([UserFavorabilityRow(user_id=user_id, favorability=150)])
    try:
        database = _build_db()
        await database.initialize()

        result = await database.set_user_favorability(user_id, 350)

        assert result.before == 150
        assert result.after == 350
        assert result.stage_index == 4
    finally:
        await _cleanup([user_id])
        await _reset_shared_orm_engine()


@pytest.mark.skipif(not POSTGRES_URL, reason="未配置真实 PostgreSQL 测试连接")
@pytest.mark.asyncio
async def test_set_accepts_boundary_values_zero_and_400() -> None:
    if not _same_database(POSTGRES_URL, _configured_database_url()):
        pytest.skip(
            "KOMARI_TEST_POSTGRES_URL 与 nonebot sqlalchemy_database_url 不一致"
        )
    floor_user = _make_user_id()
    ceiling_user = _make_user_id()
    await _reset_shared_orm_engine()
    await _insert_rows(
        [
            UserFavorabilityRow(user_id=floor_user, favorability=100),
            UserFavorabilityRow(user_id=ceiling_user, favorability=50),
        ]
    )
    try:
        database = _build_db()
        await database.initialize()

        floor = await database.set_user_favorability(floor_user, 0)
        ceiling = await database.set_user_favorability(ceiling_user, 400)

        assert floor.after == 0
        assert floor.stage_index == 1
        assert ceiling.after == 400
        assert ceiling.stage_index == 4
    finally:
        await _cleanup([floor_user, ceiling_user])
        await _reset_shared_orm_engine()


@pytest.mark.skipif(not POSTGRES_URL, reason="未配置真实 PostgreSQL 测试连接")
@pytest.mark.asyncio
async def test_set_rejects_out_of_range_without_database_access() -> None:
    if not _same_database(POSTGRES_URL, _configured_database_url()):
        pytest.skip(
            "KOMARI_TEST_POSTGRES_URL 与 nonebot sqlalchemy_database_url 不一致"
        )
    await _reset_shared_orm_engine()
    try:
        database = _build_db()

        with pytest.raises(ValueError, match="好感度值 -1 越界"):
            await database.set_user_favorability("u1", -1)
        with pytest.raises(ValueError, match="好感度值 401 越界"):
            await database.set_user_favorability("u1", 401)
    finally:
        await _reset_shared_orm_engine()


@pytest.mark.skipif(not POSTGRES_URL, reason="未配置真实 PostgreSQL 测试连接")
@pytest.mark.asyncio
async def test_adjust_clamps_at_floor_and_ceiling() -> None:
    if not _same_database(POSTGRES_URL, _configured_database_url()):
        pytest.skip(
            "KOMARI_TEST_POSTGRES_URL 与 nonebot sqlalchemy_database_url 不一致"
        )
    user_id = _make_user_id()
    await _reset_shared_orm_engine()
    try:
        database = _build_db()
        await database.initialize()
        await database.adjust_user_favorability(user_id, 10)

        clamped_floor = await database.adjust_user_favorability(user_id, -100)
        clamped_ceiling = await database.adjust_user_favorability(user_id, 500)

        assert clamped_floor.before == 10
        assert clamped_floor.after == 0
        assert clamped_ceiling.before == 0
        assert clamped_ceiling.after == 400
        assert await _fetch_score(user_id) == 400
    finally:
        await _cleanup([user_id])
        await _reset_shared_orm_engine()


@pytest.mark.skipif(not POSTGRES_URL, reason="未配置真实 PostgreSQL 测试连接")
@pytest.mark.asyncio
async def test_adjust_duplicate_operation_reuses_stored_result_and_rejects_conflict() -> (
    None
):
    if not _same_database(POSTGRES_URL, _configured_database_url()):
        pytest.skip(
            "KOMARI_TEST_POSTGRES_URL 与 nonebot sqlalchemy_database_url 不一致"
        )
    user_id = _make_user_id()
    operation_id = f"op-{uuid4().hex}"
    await _reset_shared_orm_engine()
    try:
        database = _build_db(initial_favorability=100)
        await database.initialize()

        first = await database.adjust_user_favorability(
            user_id, 5, operation_id=operation_id
        )
        duplicate = await database.adjust_user_favorability(
            user_id, 5, operation_id=operation_id
        )

        assert first.before == duplicate.before == 100
        assert first.after == duplicate.after == 105
        assert await _fetch_score(user_id) == 105

        with pytest.raises(ValueError, match="载荷冲突"):
            await database.adjust_user_favorability(
                user_id, 99, operation_id=operation_id
            )
        assert await _fetch_score(user_id) == 105

        with pytest.raises(ValueError, match="不能为空"):
            await database.adjust_user_favorability(user_id, 5, operation_id="  ")
    finally:
        await _cleanup([user_id], [operation_id])
        await _reset_shared_orm_engine()


@pytest.mark.skipif(not POSTGRES_URL, reason="未配置真实 PostgreSQL 测试连接")
@pytest.mark.asyncio
async def test_cleanup_ledger_removes_only_rows_older_than_retention() -> None:
    if not _same_database(POSTGRES_URL, _configured_database_url()):
        pytest.skip(
            "KOMARI_TEST_POSTGRES_URL 与 nonebot sqlalchemy_database_url 不一致"
        )
    old_op = f"old-{uuid4().hex}"
    new_op = f"new-{uuid4().hex}"
    user_id = _make_user_id()
    now = datetime.now(UTC)
    await _reset_shared_orm_engine()
    await _insert_rows(
        [
            UserFavorabilityAdjustmentLedgerRow(
                operation_id=old_op,
                user_id=user_id,
                requested_delta=5,
                created_at=now - timedelta(days=3),
            ),
            UserFavorabilityAdjustmentLedgerRow(
                operation_id=new_op,
                user_id=user_id,
                requested_delta=5,
                created_at=now,
            ),
        ]
    )
    try:
        database = _build_db()
        await database.initialize()

        deleted = await database.cleanup_adjustment_ledger(retention_days=1)

        assert deleted == 1
        session = _open_session()
        try:
            remaining = (
                await session.execute(
                    select(func.count())
                    .select_from(UserFavorabilityAdjustmentLedgerRow)
                    .where(
                        UserFavorabilityAdjustmentLedgerRow.__table__.c.user_id
                        == user_id
                    )
                )
            ).scalar_one()
        finally:
            await session.close()
        assert remaining == 1
    finally:
        await _cleanup([user_id], [old_op, new_op])
        await _reset_shared_orm_engine()


@pytest.mark.skipif(not POSTGRES_URL, reason="未配置真实 PostgreSQL 测试连接")
@pytest.mark.asyncio
async def test_get_user_count_counts_all_rows() -> None:
    if not _same_database(POSTGRES_URL, _configured_database_url()):
        pytest.skip(
            "KOMARI_TEST_POSTGRES_URL 与 nonebot sqlalchemy_database_url 不一致"
        )
    first_id = _make_user_id()
    second_id = _make_user_id()
    await _reset_shared_orm_engine()
    await _insert_rows(
        [
            UserFavorabilityRow(user_id=first_id, favorability=10),
            UserFavorabilityRow(user_id=second_id, favorability=20),
        ]
    )
    try:
        database = _build_db()
        await database.initialize()

        assert await database.get_user_count() >= 2
    finally:
        await _cleanup([first_id, second_id])
        await _reset_shared_orm_engine()


@pytest.mark.skipif(not POSTGRES_URL, reason="未配置真实 PostgreSQL 测试连接")
@pytest.mark.asyncio
async def test_database_methods_raise_before_initialize() -> None:
    if not _same_database(POSTGRES_URL, _configured_database_url()):
        pytest.skip(
            "KOMARI_TEST_POSTGRES_URL 与 nonebot sqlalchemy_database_url 不一致"
        )
    user_id = _make_user_id()
    await _reset_shared_orm_engine()
    try:
        database = _build_db()

        with pytest.raises(RuntimeError, match="UserDataDB 连接池未初始化"):
            await database.get_user_favorability(user_id)
        with pytest.raises(RuntimeError, match="UserDataDB 连接池未初始化"):
            await database.set_user_favorability(user_id, 100)
        with pytest.raises(RuntimeError, match="UserDataDB 连接池未初始化"):
            await database.adjust_user_favorability(user_id, 5)
        with pytest.raises(RuntimeError, match="UserDataDB 连接池未初始化"):
            await database.cleanup_adjustment_ledger(retention_days=1)
        with pytest.raises(RuntimeError, match="UserDataDB 连接池未初始化"):
            await database.get_user_count()
    finally:
        await _cleanup([user_id])
        await _reset_shared_orm_engine()


@pytest.mark.skipif(not POSTGRES_URL, reason="未配置真实 PostgreSQL 测试连接")
@pytest.mark.asyncio
async def test_close_resets_ready_state_and_can_reinitialize() -> None:
    if not _same_database(POSTGRES_URL, _configured_database_url()):
        pytest.skip(
            "KOMARI_TEST_POSTGRES_URL 与 nonebot sqlalchemy_database_url 不一致"
        )
    user_id = _make_user_id()
    await _reset_shared_orm_engine()
    try:
        database = _build_db()
        await database.initialize()
        await database.close()

        with pytest.raises(RuntimeError, match="UserDataDB 连接池未初始化"):
            await database.get_user_count()

        await database.initialize()
        result = await database.set_user_favorability(user_id, 300)
        assert result.after == 300
    finally:
        await _cleanup([user_id])
        await _reset_shared_orm_engine()
