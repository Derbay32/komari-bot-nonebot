"""TSK-182：Prompt 加载器的版本化快照 seam。

判定请求必须一次取得「正文 + 匹配修订」的不可变快照：正文与 revision 只能
来自同一次存储读取，缓存刷新与并发修订不得让两者错配。测试只经公开的
``PromptTemplateLoader.get_versioned_snapshot_async`` 与既有 Prompt 存储写入
入口观察行为，不绑定缓存字段、私有方法或 SQL 形状。

集成路径沿用既有的 Prompt 存储集成测试约定：使用 nonebot-plugin-orm 配置的
数据库（``sqlalchemy_database_url``，与 ``KOMARI_TEST_POSTGRES_URL`` 同库且
已迁移到 head），用例内重建单行后清理，不降级共享门控库。
"""

from __future__ import annotations

import asyncio
import os
from contextlib import suppress
from urllib.parse import urlparse

import pytest
from sqlalchemy import delete

from komari_bot.config import prompt_storage as storage_module
from komari_bot.config.prompt_storage import (
    PromptTemplateLoader,
    StoredPrompt,
    get_prompt_storage,
)
from komari_bot.plugins.komari_decision.prompt_schema import (
    RESOURCE_ID as DECISION_RESOURCE_ID,
)

POSTGRES_URL = os.getenv("KOMARI_TEST_POSTGRES_URL", "")

#: 与 Schema 字段集一致的手写字面量顺序（不读取生产默认值当期望答案）。
DECISION_FIELDS = (
    "retention_purpose",
    "reply_eligibility",
    "role_description",
    "summary_recognition",
)


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


def _require_gate() -> None:
    if not POSTGRES_URL:
        pytest.skip("未设置 KOMARI_TEST_POSTGRES_URL，跳过集成测试")
    if not _same_database(POSTGRES_URL, _configured_database_url()):
        pytest.skip("KOMARI_TEST_POSTGRES_URL 与 nonebot sqlalchemy_database_url 不一致")


async def _reset_shared_orm_engine() -> None:
    """pytest-asyncio 每个用例一个事件循环：先释放共享引擎连接池。"""

    from nonebot import require

    require("nonebot_plugin_orm")
    import nonebot_plugin_orm as orm_module

    engines = getattr(orm_module, "_engines", None)
    if not engines:
        return
    for engine in list(engines.values()):
        with suppress(Exception):
            await engine.dispose()


def _prompt_data(revision_marker: str) -> dict[str, str]:
    """手写四字段正文：每段都携带同一个修订标记，便于检测正文/修订错配。"""
    return {
        "role_description": f"{revision_marker} 角色描述",
        "reply_eligibility": f"{revision_marker} 回应资格",
        "retention_purpose": f"{revision_marker} 保留用途",
        "summary_recognition": f"{revision_marker} 总结识别",
    }


def _loader() -> PromptTemplateLoader:
    return PromptTemplateLoader(
        resource_id=DECISION_RESOURCE_ID,
        display_name="Komari Decision Prompt",
        log_prefix="[test]",
    )


async def _drop_prompt_row() -> None:
    from nonebot import require

    require("nonebot_plugin_orm")
    from nonebot_plugin_orm import get_session

    model_cls = storage_module.ensure_typed_prompt_model(DECISION_RESOURCE_ID)
    assert model_cls is not None
    async with get_session() as session:
        await session.execute(delete(model_cls))
        await session.commit()


async def _write_revision(expected_revision: int, marker: str) -> int:
    """经公开存储入口写入一个新修订；返回写入后的修订号。"""
    stored = await get_prompt_storage().replace_if_revision_async(
        resource_id=DECISION_RESOURCE_ID,
        prompt_data=_prompt_data(marker),
        expected_revision=expected_revision,
    )
    assert stored is not None
    return stored.revision


def _assert_snapshot_consistent(snapshot: StoredPrompt, marker: str) -> None:
    assert snapshot.resource_id == DECISION_RESOURCE_ID
    assert set(snapshot.prompt_data) == set(DECISION_FIELDS)
    for field in DECISION_FIELDS:
        assert snapshot.prompt_data[field] == _prompt_data(marker)[field]


@pytest.mark.asyncio
async def test_snapshot_returns_stored_text_with_matching_revision() -> None:
    """AC：一次读取同时得到正文、修订号与写入时间。"""
    _require_gate()
    await _reset_shared_orm_engine()
    try:
        await _drop_prompt_row()
        assert await _write_revision(0, "rev-1") == 1
        written = await get_prompt_storage().fetch_async(DECISION_RESOURCE_ID)
        assert written is not None

        snapshot = await _loader().get_versioned_snapshot_async()

        _assert_snapshot_consistent(snapshot, "rev-1")
        assert snapshot.revision == 1
        assert snapshot.updated_at == written.updated_at
    finally:
        await _drop_prompt_row()
        await _reset_shared_orm_engine()


@pytest.mark.asyncio
async def test_returned_snapshot_is_unchanged_by_later_refresh() -> None:
    """AC：已返回的快照是防御性副本，后续修订不改写它。"""
    _require_gate()
    await _reset_shared_orm_engine()
    try:
        await _drop_prompt_row()
        assert await _write_revision(0, "rev-1") == 1
        loader = _loader()
        first = await loader.get_versioned_snapshot_async()
        _assert_snapshot_consistent(first, "rev-1")

        assert await _write_revision(1, "rev-2") == 2
        second = await loader.get_versioned_snapshot_async()

        _assert_snapshot_consistent(first, "rev-1")
        assert first.revision == 1
        _assert_snapshot_consistent(second, "rev-2")
        assert second.revision == 2
        assert first.prompt_data is not second.prompt_data
    finally:
        await _drop_prompt_row()
        await _reset_shared_orm_engine()


@pytest.mark.asyncio
async def test_concurrent_revisions_never_mix_text_and_revision() -> None:
    """AC：并发刷新下，任一次读取的正文与修订号必须来自同一次存储读取。"""
    _require_gate()
    await _reset_shared_orm_engine()
    try:
        await _drop_prompt_row()
        assert await _write_revision(0, "rev-1") == 1
        loader = _loader()
        await loader.get_versioned_snapshot_async()

        finished = asyncio.Event()

        async def writer() -> int:
            revision = 1
            for step in range(2, 8):
                revision = await _write_revision(revision, f"rev-{step}")
            finished.set()
            return revision

        async def reader(snapshots: list[StoredPrompt]) -> None:
            # 读取侧的每次迭代都必须与写入侧交错，否则本用例只证明缓存命中。
            while not finished.is_set():
                snapshots.append(await loader.get_versioned_snapshot_async())
                await asyncio.sleep(0.005)
            snapshots.append(await loader.get_versioned_snapshot_async())

        snapshots: list[StoredPrompt] = []
        await asyncio.gather(writer(), reader(snapshots), reader(snapshots))
        assert len(snapshots) >= 4

        seen: set[int] = set()
        for snapshot in snapshots:
            _assert_snapshot_consistent(snapshot, f"rev-{snapshot.revision}")
            seen.add(snapshot.revision)
        assert 1 in seen
        assert max(seen) >= 2
    finally:
        await _drop_prompt_row()
        await _reset_shared_orm_engine()
