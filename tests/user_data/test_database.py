"""user_data 数据库访问层测试。"""

from __future__ import annotations

import pytest

from komari_bot.plugins.user_data.config_schema import DynamicConfigSchema
from komari_bot.plugins.user_data.database import UserDataDB


def _build_db() -> UserDataDB:
    return UserDataDB(DynamicConfigSchema())


@pytest.mark.asyncio
async def test_get_user_favorability_requires_initialized_pool() -> None:
    db = _build_db()

    with pytest.raises(RuntimeError, match="UserDataDB 连接池未初始化"):
        await db.get_user_favorability("1047195267")


@pytest.mark.asyncio
async def test_get_user_count_requires_initialized_pool() -> None:
    db = _build_db()

    with pytest.raises(RuntimeError, match="UserDataDB 连接池未初始化"):
        await db.get_user_count()


@pytest.mark.asyncio
async def test_set_user_favorability_requires_initialized_pool() -> None:
    """未初始化连接池时 set_user_favorability 应抛出明确 RuntimeError。"""
    db = _build_db()

    with pytest.raises(RuntimeError, match="UserDataDB 连接池未初始化"):
        await db.set_user_favorability("1047195267", 100)


@pytest.mark.asyncio
async def test_set_user_favorability_rejects_out_of_range_value() -> None:
    """set_user_favorability 在值越界时应抛出 ValueError（无需连接池）。"""
    db = _build_db()

    with pytest.raises(ValueError, match="好感度值 -1 越界"):
        await db.set_user_favorability("u1", -1)

    with pytest.raises(ValueError, match="好感度值 401 越界"):
        await db.set_user_favorability("u1", 401)


@pytest.mark.asyncio
async def test_set_user_favorability_accepts_boundary_values() -> None:
    """0 和 400 边界值应通过参数校验（不抛 ValueError，下一阶段因未初始化池失败）。"""
    db = _build_db()
    with pytest.raises(RuntimeError, match="UserDataDB 连接池未初始化"):
        await db.set_user_favorability("u1", 0)
    with pytest.raises(RuntimeError, match="UserDataDB 连接池未初始化"):
        await db.set_user_favorability("u1", 400)
