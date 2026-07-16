"""user_data 数据库访问层测试。"""

from __future__ import annotations

import importlib
from typing import Any, cast

import pytest

from komari_bot.plugins.user_data.config_schema import DynamicConfigSchema
from komari_bot.plugins.user_data.database import UserDataDB


def _build_db() -> UserDataDB:
    return UserDataDB(DynamicConfigSchema())


class _LifecyclePool:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_initialize_publishes_pool_only_after_schema_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_module = importlib.import_module(
        "komari_bot.plugins.user_data.database",
    )

    db = _build_db()
    pool = _LifecyclePool()
    observed: dict[str, object] = {}

    async def _create_pool(_config: object) -> object:
        return pool

    async def _create_tables(actual_pool: object) -> None:
        observed["pool"] = actual_pool
        observed["published_during_schema"] = db._pool

    monkeypatch.setattr(database_module, "create_postgres_pool", _create_pool)
    monkeypatch.setattr(db, "_create_tables", _create_tables)

    await db.initialize()

    assert observed == {"pool": pool, "published_during_schema": None}
    assert db._pool is cast("Any", pool)
    assert pool.closed is False


@pytest.mark.asyncio
async def test_initialize_closes_local_pool_when_schema_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_module = importlib.import_module(
        "komari_bot.plugins.user_data.database",
    )

    db = _build_db()
    pool = _LifecyclePool()

    async def _create_pool(_config: object) -> object:
        return pool

    async def _fail_create_tables(_pool: object) -> None:
        msg = "模拟建表失败"
        raise RuntimeError(msg)

    monkeypatch.setattr(database_module, "create_postgres_pool", _create_pool)
    monkeypatch.setattr(db, "_create_tables", _fail_create_tables)

    with pytest.raises(RuntimeError, match="模拟建表失败"):
        await db.initialize()

    assert pool.closed is True
    assert db._pool is None


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
