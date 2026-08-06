"""可选的配置多 worker PostgreSQL 集成测试。

模拟两个进程实例（各自独立的 ConfigStorage）：初始化竞态不覆盖既有行，
CAS 更新后另一实例通过 revision 轮询在陈限内收到新快照。

异步存储路径只使用 nonebot-plugin-orm 引擎（配置中的
``sqlalchemy_database_url``）；``KOMARI_TEST_POSTGRES_URL`` 只作为
opt-in 开关，且其库地址必须与 nonebot 配置一致，否则跳过。
"""

from __future__ import annotations

import asyncio
import os
import threading
from urllib.parse import urlparse

import pytest
from sqlalchemy import delete

from komari_bot.plugins.config_manager import storage as storage_module
from komari_bot.plugins.config_manager.storage import ConfigStorage, StoredConfig
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
async def test_config_initialization_and_revision_propagate_across_instances() -> None:
    if not _same_database(POSTGRES_URL, _configured_database_url()):
        pytest.skip(
            "KOMARI_TEST_POSTGRES_URL 与 nonebot sqlalchemy_database_url 不一致"
        )
    loop = asyncio.get_running_loop()
    first = ConfigStorage()
    second = ConfigStorage()
    changed = threading.Event()
    observed: list[StoredConfig] = []

    def _observe(snapshot: StoredConfig) -> None:
        observed.append(snapshot)
        changed.set()

    second.register_watcher(
        PLUGIN_NAME,
        _observe,
        max_staleness_seconds=0.2,
    )
    second.bind_app_loop(loop)

    try:
        await _reset_table()

        first_stored, second_stored = await asyncio.gather(
            first.insert_if_absent_async(
                plugin_name=PLUGIN_NAME,
                config=UserDataConfigSchema(initial_favorability=11),
            ),
            second.insert_if_absent_async(
                plugin_name=PLUGIN_NAME,
                config=UserDataConfigSchema(initial_favorability=22),
            ),
        )

        assert first_stored.config_data == second_stored.config_data
        assert first_stored.revision == second_stored.revision == 1

        assert await asyncio.to_thread(changed.wait, 3) is True
        changed.clear()
        observed.clear()

        updated = await first.update_fields_if_revision_async(
            plugin_name=PLUGIN_NAME,
            config=UserDataConfigSchema(initial_favorability=300),
            field_names={"initial_favorability"},
            expected_revision=1,
        )

        assert updated is not None
        assert updated.revision == 2
        assert await asyncio.to_thread(changed.wait, 3) is True
        assert any(
            snapshot.revision == 2
            and snapshot.config_data["initial_favorability"] == 300
            for snapshot in observed
        )
    finally:
        await first.close_async()
        await second.close_async()
        await _reset_table()
