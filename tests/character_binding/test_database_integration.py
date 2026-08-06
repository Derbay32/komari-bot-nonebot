"""角色名绑定 ORM 仓储 PostgreSQL 集成测试（依赖已 alembic upgrade head 的 schema）。

覆盖原 asyncpg 直连集成测试与 fake 池单测的行为契约：初始化零 DDL、
load_all 快照读取、upsert 幂等覆盖、删除存在性语义与未初始化保护。
数据准备与清理走同一套 SQLModel 表模型，断言全部通过仓储公共接口。
"""

from __future__ import annotations

import os
from contextlib import suppress
from typing import TYPE_CHECKING
from urllib.parse import urlparse
from uuid import uuid4

import pytest
from sqlalchemy import delete

from komari_bot.plugins.character_binding.database import CharacterBindingDB
from komari_bot.plugins.character_binding.orm_models import CharacterBindingRow

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
    return f"bind-{uuid4().hex}"


async def _cleanup_bindings(user_ids: list[str]) -> None:
    session = _open_session()
    try:
        await session.execute(
            delete(CharacterBindingRow).where(
                CharacterBindingRow.__table__.c.user_id.in_(user_ids)
            )
        )
        await session.commit()
    finally:
        await session.close()


def _row(user_id: str, character_name: str) -> CharacterBindingRow:
    return CharacterBindingRow(
        user_id=user_id,
        character_name=character_name,
    )


@pytest.mark.skipif(not POSTGRES_URL, reason="未配置真实 PostgreSQL 测试连接")
@pytest.mark.asyncio
async def test_upsert_and_delete_are_idempotent_on_migration_managed_table() -> None:
    if not _same_database(POSTGRES_URL, _configured_database_url()):
        pytest.skip(
            "KOMARI_TEST_POSTGRES_URL 与 nonebot sqlalchemy_database_url 不一致"
        )
    user_id = _make_user_id()
    await _reset_shared_orm_engine()
    try:
        database = CharacterBindingDB()
        await database.initialize()

        await database.upsert(user_id, "泉此方")
        await database.upsert(user_id, "柊镜")

        all_bindings = await database.load_all()
        assert all_bindings[user_id] == "柊镜"
        assert await database.delete(user_id) is True
        assert await database.delete(user_id) is False
    finally:
        await _cleanup_bindings([user_id])
        await _reset_shared_orm_engine()


@pytest.mark.skipif(not POSTGRES_URL, reason="未配置真实 PostgreSQL 测试连接")
@pytest.mark.asyncio
async def test_load_all_returns_snapshot_ordered_by_user_id() -> None:
    if not _same_database(POSTGRES_URL, _configured_database_url()):
        pytest.skip(
            "KOMARI_TEST_POSTGRES_URL 与 nonebot sqlalchemy_database_url 不一致"
        )
    first_id = _make_user_id()
    second_id = _make_user_id()
    await _reset_shared_orm_engine()
    session = _open_session()
    try:
        session.add_all(
            [
                _row(second_id, "泉此方"),
                _row(first_id, "柊镜"),
            ]
        )
        await session.commit()
    finally:
        await session.close()

    try:
        database = CharacterBindingDB()
        await database.initialize()

        loaded = await database.load_all()

        # user_id 为随机 UUID，断言必须按字典序而非插入顺序
        assert list(loaded) == sorted([first_id, second_id])
        assert loaded[first_id] == "柊镜"
        assert loaded[second_id] == "泉此方"
    finally:
        await _cleanup_bindings([first_id, second_id])
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
        database = CharacterBindingDB()

        with pytest.raises(RuntimeError, match="character_binding 数据库尚未初始化"):
            await database.load_all()
        with pytest.raises(RuntimeError, match="character_binding 数据库尚未初始化"):
            await database.upsert(user_id, "泉此方")
        with pytest.raises(RuntimeError, match="character_binding 数据库尚未初始化"):
            await database.delete(user_id)
    finally:
        await _cleanup_bindings([user_id])
        await _reset_shared_orm_engine()


@pytest.mark.skipif(not POSTGRES_URL, reason="未配置真实 PostgreSQL 测试连接")
@pytest.mark.asyncio
async def test_close_resets_ready_state_and_rejects_writes() -> None:
    if not _same_database(POSTGRES_URL, _configured_database_url()):
        pytest.skip(
            "KOMARI_TEST_POSTGRES_URL 与 nonebot sqlalchemy_database_url 不一致"
        )
    user_id = _make_user_id()
    await _reset_shared_orm_engine()
    try:
        database = CharacterBindingDB()
        await database.initialize()
        await database.close()

        with pytest.raises(RuntimeError, match="character_binding 数据库尚未初始化"):
            await database.upsert(user_id, "泉此方")

        await database.initialize()
        await database.upsert(user_id, "泉此方")
        assert (await database.load_all())[user_id] == "泉此方"
    finally:
        await _cleanup_bindings([user_id])
        await _reset_shared_orm_engine()
