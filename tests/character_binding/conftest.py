"""TSK-271 真实 PostgreSQL 夹具。"""

from __future__ import annotations

import os
from contextlib import suppress
from typing import TYPE_CHECKING
from urllib.parse import urlparse
from uuid import uuid4

import pytest

from komari_bot.plugins.character_binding.manager import CharacterBindingManager

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

POSTGRES_URL = os.environ.get("KOMARI_TEST_POSTGRES_URL", "")


def _configured_database_url() -> str:
    """读取 NoneBot 实际传给 ORM driver 的连接串。"""
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


def require_postgres() -> None:
    """对每个真实库测试显式执行连接配置与同库守卫。"""
    if not POSTGRES_URL:
        pytest.skip("未配置真实 PostgreSQL 测试连接")
    if not _same_database(POSTGRES_URL, _configured_database_url()):
        pytest.skip(
            "KOMARI_TEST_POSTGRES_URL 与 nonebot sqlalchemy_database_url 不一致"
        )


async def _reset_shared_orm_engine() -> None:
    """归还 nonebot-plugin-orm 共享引擎的连接。"""
    from nonebot import require

    require("nonebot_plugin_orm")
    import nonebot_plugin_orm as orm_module

    engines = getattr(orm_module, "_engines", None)
    if not engines:
        return
    for engine in list(engines.values()):
        with suppress(Exception):
            await engine.dispose()


@pytest.fixture
def app_id() -> str:
    return f"tsk271-{uuid4().hex}"


@pytest.fixture
async def binding_manager(app_id: str) -> AsyncIterator[CharacterBindingManager]:
    del app_id
    require_postgres()
    await _reset_shared_orm_engine()
    manager = CharacterBindingManager()
    await manager.initialize()
    try:
        yield manager
    finally:
        await manager.close()
        await _reset_shared_orm_engine()


async def bind_member(
    manager: CharacterBindingManager,
    *,
    app_id: str,
    group_id: str,
    group_openid: str,
    member_qq: str,
    member_openid: str,
    character_name: str,
    bot_self_id: str = "onebot-a",
) -> object:
    """绑定公共 seam 的单一测试入口。"""
    return await manager.bind_group_member(
        app_id=app_id,
        group_id=group_id,
        group_openid=group_openid,
        member_qq=member_qq,
        member_openid=member_openid,
        character_name=character_name,
        bot_self_id=bot_self_id,
    )


def lookup_name(
    manager: CharacterBindingManager,
    *,
    app_id: str,
    group_openid: str,
    member_openid: str,
    fallback_nickname: str | None = None,
) -> str | None:
    return manager.get_qq_character_name(
        app_id=app_id,
        group_openid=group_openid,
        member_openid=member_openid,
        fallback_nickname=fallback_nickname,
    )


def list_group_bindings(
    manager: CharacterBindingManager,
    *,
    app_id: str,
    group_openid: str,
) -> object:
    """读取当前群的公开成员关系，用于原子失败后的孤立记录断言。"""
    return manager.list_group_bindings(app_id=app_id, group_openid=group_openid)
