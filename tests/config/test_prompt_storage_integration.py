"""可选的 Prompt 强类型表跨实例 CAS 集成测试。

模拟两个进程实例（各自独立的 PromptStorage）：expected_revision=0 仅允许
首次创建，revision CAS 冲突时旧修订号写入失败，重试后字段合并不丢失；
本进程写入立即触发本地失效回调（移除 LISTEN/NOTIFY 后的本地失效语义）。

异步存储路径只使用 nonebot-plugin-orm 引擎（配置中的
``sqlalchemy_database_url``）；``KOMARI_TEST_POSTGRES_URL`` 只作为
opt-in 开关，且其库地址必须与 nonebot 配置一致并已应用迁移
（``orm_bootstrap upgrade head``），否则跳过。
"""

from __future__ import annotations

import os
import threading
from contextlib import suppress
from urllib.parse import urlparse

import pytest
from sqlalchemy import delete

from komari_bot.config import prompt_storage as storage_module
from komari_bot.config.prompt_storage import PromptStorage
from komari_bot.plugins.komari_chat.prompt_schema import DEFAULTS

POSTGRES_URL = os.getenv("KOMARI_TEST_POSTGRES_URL", "")

RESOURCE_ID = "komari_chat"


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
    """清空 nonebot-plugin-orm 共享引擎的连接池。

    pytest-asyncio 为每个测试创建独立事件循环，而 nonebot-plugin-orm 的
    引擎整个测试会话共享：上一个测试遗留的 asyncpg 连接绑定在已关闭的
    事件循环上，复用会触发 "another operation is in progress"。这里显式
    dispose，保证本测试与后续测试都在各自事件循环上重建连接。
    """
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

    model_cls = storage_module.ensure_typed_prompt_model(RESOURCE_ID)
    assert model_cls is not None
    async with get_session() as session:
        await session.execute(delete(model_cls))
        await session.commit()


def _prompt_data(**overrides: str) -> dict[str, str]:
    return dict(DEFAULTS) | overrides


@pytest.mark.skipif(
    not POSTGRES_URL, reason="未设置 KOMARI_TEST_POSTGRES_URL，跳过集成测试"
)
@pytest.mark.asyncio
async def test_prompt_field_cas_between_storage_instances() -> None:
    if not _same_database(POSTGRES_URL, _configured_database_url()):
        pytest.skip(
            "KOMARI_TEST_POSTGRES_URL 与 nonebot sqlalchemy_database_url 不一致"
        )
    first = PromptStorage()
    second = PromptStorage()
    local_changes = threading.Event()
    second.register_invalidator(RESOURCE_ID, local_changes.set)

    try:
        await _reset_shared_orm_engine()
        await _reset_table()

        assert await first.fetch_async(RESOURCE_ID) is None
        assert await second.fetch_async(RESOURCE_ID) is None

        created = await first.replace_if_revision_async(
            resource_id=RESOURCE_ID,
            prompt_data=_prompt_data(system_prompt="初始"),
            expected_revision=0,
        )
        assert created is not None
        assert created.revision == 1
        assert created.prompt_data["system_prompt"] == "初始"

        # expected_revision=0 仅允许创建：已有记录时不再写入
        duplicate = await second.replace_if_revision_async(
            resource_id=RESOURCE_ID,
            prompt_data=_prompt_data(system_prompt="并发创建"),
            expected_revision=0,
        )
        assert duplicate is None

        first_update = await first.update_field_if_revision_async(
            resource_id=RESOURCE_ID,
            field_name="system_prompt",
            value="第一处更新",
            expected_revision=1,
        )
        stale_update = await second.update_field_if_revision_async(
            resource_id=RESOURCE_ID,
            field_name="memory_ack",
            value="第二处更新",
            expected_revision=1,
        )

        assert first_update is not None
        assert first_update.revision == 2
        assert stale_update is None

        latest = await second.fetch_async(RESOURCE_ID)
        assert latest is not None
        retried = await second.update_field_if_revision_async(
            resource_id=RESOURCE_ID,
            field_name="memory_ack",
            value="第二处更新",
            expected_revision=latest.revision,
        )
        assert retried is not None
        assert retried.prompt_data == _prompt_data(
            system_prompt="第一处更新",
            memory_ack="第二处更新",
        )
        assert retried.revision == 3

        # 本进程写入立即触发本地失效回调（无需 LISTEN/NOTIFY）
        local_changes.clear()
        replaced = await second.upsert_async(
            resource_id=RESOURCE_ID,
            prompt_data=_prompt_data(system_prompt="整体替换"),
        )
        assert replaced.revision == 4
        assert local_changes.is_set() is True
    finally:
        first.close()
        second.close()
        await _reset_table()
        await _reset_shared_orm_engine()
