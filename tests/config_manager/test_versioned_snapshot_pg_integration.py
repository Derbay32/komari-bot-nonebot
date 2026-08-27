"""TSK-221 真实 PostgreSQL 集成测试：版本化快照、listener 发布与 strict CAS。

门控约定与既有多 worker 集成测试一致：``KOMARI_TEST_POSTGRES_URL`` 为
opt-in 开关，且其库地址必须与 nonebot 配置的
``sqlalchemy_database_url`` 同库，否则跳过。全程使用真实
ConfigStorage / ORM 表（``komari_user_data_config``），不经任何 fake。
"""

from __future__ import annotations

import asyncio
import os
from contextlib import suppress
from typing import Any
from urllib.parse import urlparse

import pytest
from sqlalchemy import delete

from komari_bot.plugins.config_manager import storage as storage_module
from komari_bot.plugins.config_manager.manager import ConfigManager
from komari_bot.plugins.config_manager.storage import ConfigStorage
from komari_bot.plugins.user_data.config_schema import (
    DynamicConfigSchema as UserDataConfigSchema,
)

POSTGRES_URL = os.getenv("KOMARI_TEST_POSTGRES_URL", "")

PLUGIN_NAME = "user_data"


def _configured_database_url() -> str:
    from nonebot import get_driver

    return str(
        getattr(get_driver().config, "sqlalchemy_database_url", "") or ""
    )


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


async def _reset_table() -> None:
    from nonebot import require

    require("nonebot_plugin_orm")
    from nonebot_plugin_orm import get_session

    model_cls = storage_module.ensure_typed_config_model(PLUGIN_NAME)
    assert model_cls is not None
    async with get_session() as session:
        await session.execute(delete(model_cls))
        await session.commit()


@pytest.mark.skipif(
    not POSTGRES_URL, reason="未设置 KOMARI_TEST_POSTGRES_URL，跳过集成测试"
)
@pytest.mark.asyncio
async def test_versioned_snapshot_listener_and_strict_cas_over_real_storage() -> None:
    if not _same_database(POSTGRES_URL, _configured_database_url()):
        pytest.skip(
            "KOMARI_TEST_POSTGRES_URL 与 nonebot sqlalchemy_database_url 不一致"
        )

    loop = asyncio.get_running_loop()
    await storage_module.close_config_storage_if_created()
    await _reset_shared_orm_engine()

    manager = ConfigManager(PLUGIN_NAME, UserDataConfigSchema)
    bootstrap = ConfigStorage()
    external = ConfigStorage()
    observed: list[Any] = []
    external_published = asyncio.Event()

    def _listener(snapshot: Any) -> None:
        observed.append(snapshot)
        if getattr(snapshot, "revision", None) == 3:
            external_published.set()

    try:
        await _reset_table()
        seeded = await bootstrap.insert_if_absent_async(
            plugin_name=PLUGIN_NAME,
            config=UserDataConfigSchema(initial_favorability=10),
        )
        assert seeded.revision == 1

        await manager.initialize_async()
        storage_module.get_config_storage().bind_app_loop(loop)

        snapshot = manager.get_cached_versioned_snapshot()
        assert snapshot.revision == 1
        assert snapshot.value.initial_favorability == 10
        assert snapshot.updated_at == seeded.updated_at

        manager.register_snapshot_listener(_listener)

        # 本地 strict CAS 写入：接纳新修订后同步发布
        updated = await manager.update_field_if_revision_async(
            "initial_favorability", 123, expected_revision=1
        )
        assert updated is not None
        assert updated.revision == 2
        assert updated.value.initial_favorability == 123
        assert len(observed) == 1
        assert observed[0] == updated == manager.get_cached_versioned_snapshot()

        # strict CAS 冲突：明确返回 None，快照与发布均保持不变
        conflict = await manager.update_field_if_revision_async(
            "initial_favorability", 200, expected_revision=1
        )
        assert conflict is None
        assert len(observed) == 1
        assert manager.get_cached_versioned_snapshot().revision == 2

        externally = await external.update_fields_if_revision_async(
            plugin_name=PLUGIN_NAME,
            config=UserDataConfigSchema(initial_favorability=300),
            field_names={"initial_favorability"},
            expected_revision=2,
        )
        assert externally is not None
        assert externally.revision == 3

        # 外部实例写入更高修订 → 真实 watcher 轮询发现 → 同步发布；
        # watcher 未按约发布时直接以超时失败，不吞掉 TimeoutError
        await asyncio.wait_for(external_published.wait(), timeout=5.0)

        published = [item for item in observed if item.revision == 3]
        assert len(published) == 1
        assert published[0].value.initial_favorability == 300
        current = manager.get_cached_versioned_snapshot()
        assert current.revision == 3
        assert published[0] == current
    finally:
        manager.unregister_snapshot_listener(_listener)
        await bootstrap.close_async()
        await external.close_async()
        await storage_module.close_config_storage_if_created()
        await _reset_table()
        await _reset_shared_orm_engine()
