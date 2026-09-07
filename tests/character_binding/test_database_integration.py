"""角色绑定管理器与迁移管理 PostgreSQL 的集成行为。"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from sqlalchemy import text

from tests.character_binding.conftest import bind_member, lookup_name

if TYPE_CHECKING:
    from komari_bot.plugins.character_binding.manager import CharacterBindingManager


async def _execute_legacy_sql(statement: str, **parameters: object) -> None:
    from nonebot import require

    require("nonebot_plugin_orm")
    from nonebot_plugin_orm import get_session

    session = get_session(expire_on_commit=False)
    try:
        await session.execute(text(statement), parameters)
        await session.commit()
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_manager_reads_only_scoped_binding_and_explicit_display_fallback(
    binding_manager: CharacterBindingManager,
    app_id: str,
) -> None:
    await bind_member(
        binding_manager,
        app_id=app_id,
        group_id="100201",
        group_openid="openid-database",
        member_qq="10201",
        member_openid="member-database",
        character_name="群内名字",
    )

    assert (
        lookup_name(
            binding_manager,
            app_id=app_id,
            group_openid="openid-database",
            member_openid="member-database",
        )
        == "群内名字"
    )
    assert (
        lookup_name(
            binding_manager,
            app_id=app_id,
            group_openid="openid-database",
            member_openid="unknown-member",
            fallback_nickname="平台昵称",
        )
        == "平台昵称"
    )


@pytest.mark.asyncio
async def test_legacy_global_value_is_not_a_default_group_lookup(
    binding_manager: CharacterBindingManager,
    app_id: str,
) -> None:
    legacy_user_id = str(int(app_id.removeprefix("tsk271-")[:15], 16))
    await _execute_legacy_sql(
        """
        INSERT INTO komari_character_bindings (user_id, character_name)
        VALUES (:user_id, :character_name)
        ON CONFLICT (user_id) DO UPDATE
        SET character_name = EXCLUDED.character_name
        """,
        user_id=legacy_user_id,
        character_name="旧全局名字",
    )
    try:
        assert (
            binding_manager.get_character_name(
                group_id="100203",
                user_id=legacy_user_id,
                fallback_nickname="平台昵称",
            )
            == "平台昵称"
        )
    finally:
        await _execute_legacy_sql(
            "DELETE FROM komari_character_bindings WHERE user_id = :user_id",
            user_id=legacy_user_id,
        )


@pytest.mark.asyncio
async def test_manager_close_reloads_persisted_group_binding(
    binding_manager: CharacterBindingManager,
    app_id: str,
) -> None:
    await bind_member(
        binding_manager,
        app_id=app_id,
        group_id="100202",
        group_openid="openid-reload",
        member_qq="10202",
        member_openid="member-reload",
        character_name="重启后仍在库",
    )

    from komari_bot.plugins.character_binding.manager import CharacterBindingManager

    reloaded = CharacterBindingManager()
    await reloaded.initialize()
    try:
        assert (
            lookup_name(
                reloaded,
                app_id=app_id,
                group_openid="openid-reload",
                member_openid="member-reload",
            )
            == "重启后仍在库"
        )
    finally:
        await reloaded.close()
