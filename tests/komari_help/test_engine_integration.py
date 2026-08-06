"""HelpEngine 共享引擎连接集成测试。

取代 ``test_engine_lifecycle.py`` / ``test_engine_list.py`` /
``test_engine_validation.py`` / ``test_sync_auto_generated_help.py`` 中 asyncpg
池 fake 测试：连接来源统一走引擎 ``initialize()`` 真实路径或共享引擎租约，
扫描租约、自动帮助同步、xmin CAS 更新等行为全部在真实库上断言。每个测试
独立事件循环，测试前后 dispose 共享引擎；写入数据按唯一 plugin_name 前缀
收尾清理。
"""

from __future__ import annotations

import os
from contextlib import suppress
from types import SimpleNamespace
from typing import TYPE_CHECKING
from urllib.parse import urlparse
from uuid import uuid4

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

import pytest

from komari_bot.plugins.komari_help import engine as engine_module
from komari_bot.plugins.komari_help.engine import HelpEngine

POSTGRES_URL = os.getenv("KOMARI_TEST_POSTGRES_URL", "")

EMBEDDING_DIMENSION = 512


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


def _asyncpg_url() -> str:
    return POSTGRES_URL.replace("postgresql+asyncpg://", "postgresql://")


async def _reset_shared_orm_engine() -> None:
    from nonebot import require

    require("nonebot_plugin_orm")
    import nonebot_plugin_orm as orm_module

    engines = getattr(orm_module, "_engines", None)
    if not engines:
        return
    for engine in list(engines.values()):
        with suppress(Exception):
            await engine.dispose()


def _vector(dominant_index: int, value: float = 1.0) -> str:
    components = [0.0] * EMBEDDING_DIMENSION
    components[dominant_index] = value
    return str(components)


async def _cleanup_help(plugin_prefixes: list[str]) -> None:
    import asyncpg

    admin = await asyncpg.connect(_asyncpg_url())
    try:
        for prefix in plugin_prefixes:
            await admin.execute(
                "DELETE FROM komari_help WHERE plugin_name LIKE $1",
                f"{prefix}%",
            )
    finally:
        await admin.close()


@pytest.fixture
async def test_config(monkeypatch: pytest.MonkeyPatch) -> "AsyncIterator[object]":
    """模块级 get_config 打桩（配置属于业务输入，不涉及连接来源）。"""
    config = engine_module.DynamicConfigSchema()
    monkeypatch.setattr(engine_module, "get_config", lambda: config)
    yield config


@pytest.fixture
async def initialized_engine() -> "AsyncIterator[HelpEngine]":
    """通过 initialize() 真实路径构造引擎（连接来源切换的验收点）。"""
    await _reset_shared_orm_engine()
    if not _same_database(POSTGRES_URL, _configured_database_url()):
        pytest.skip("KOMARI_TEST_POSTGRES_URL 与 nonebot sqlalchemy_database_url 不一致")
    original_nonebot_mode = engine_module.state.nonebot_mode
    original_standalone_config = engine_module.state.standalone_config
    engine_module.state.nonebot_mode = False
    engine_module.state.standalone_config = engine_module.DynamicConfigSchema()

    class _FakeEmbeddingService:
        def __init__(self) -> None:
            self.config = SimpleNamespace(embedding_dimension=EMBEDDING_DIMENSION)
            self.cleaned = False

        async def cleanup(self) -> None:
            self.cleaned = True

    engine = HelpEngine()
    engine._embedding_service = _FakeEmbeddingService()
    try:
        await engine.initialize()
        yield engine
    finally:
        await engine.close()
        engine_module.state.nonebot_mode = original_nonebot_mode
        engine_module.state.standalone_config = original_standalone_config
        await _reset_shared_orm_engine()


@pytest.fixture
async def data_engine() -> "AsyncIterator[HelpEngine]":
    """直接挂共享引擎租约的引擎（检索/管理行为断言；非 fake 池）。"""
    await _reset_shared_orm_engine()
    if not _same_database(POSTGRES_URL, _configured_database_url()):
        pytest.skip("KOMARI_TEST_POSTGRES_URL 与 nonebot sqlalchemy_database_url 不一致")
    from komari_bot.common.orm_connection import get_shared_orm_connection_pool

    engine = HelpEngine()
    engine._pool = get_shared_orm_connection_pool()

    async def _noop_cleanup() -> None:
        return None

    engine._embedding_service = SimpleNamespace(
        config=SimpleNamespace(embedding_dimension=EMBEDDING_DIMENSION),
        cleanup=_noop_cleanup,
    )

    async def _fake_embedding(text: str) -> list[float]:
        import hashlib

        digest = hashlib.sha256(text.encode("utf-8")).digest()
        return [digest[index % len(digest)] / 255.0 for index in range(EMBEDDING_DIMENSION)]

    engine._get_embedding = _fake_embedding  # type: ignore[method-assign]
    try:
        await engine._build_keyword_index()
        yield engine
    finally:
        await engine.close()
        await _cleanup_help([f"t10-{_HELP_PREFIX_TOKEN}"])
        await _reset_shared_orm_engine()


_HELP_PREFIX_TOKEN = uuid4().hex[:8]


def _plugin_name(tag: str) -> str:
    return f"t10-{_HELP_PREFIX_TOKEN}-{tag}"


# ---------- 初始化与连接来源 ----------


@pytest.mark.skipif(not POSTGRES_URL, reason="未配置真实 PostgreSQL 测试连接")
@pytest.mark.asyncio
async def test_initialize_bootstraps_pool_from_shared_engine(
    initialized_engine: HelpEngine,
) -> None:
    engine = initialized_engine
    assert engine._initialized is True
    assert engine._pool is not None
    # 验收：初始化建立的连接必须落在 nonebot-plugin-orm 配置的数据库
    async with engine._pool.acquire() as conn:  # type: ignore[attr-defined]
        database = await conn.fetchval("SELECT current_database()")
    expected = (urlparse(POSTGRES_URL).path or "/").lstrip("/")
    assert str(database) == expected
    assert engine._keyword_index.loaded is True


# ---------- 检索与管理行为 ----------


@pytest.mark.skipif(not POSTGRES_URL, reason="未配置真实 PostgreSQL 测试连接")
@pytest.mark.asyncio
async def test_add_get_list_search_help(
    data_engine: HelpEngine,
    test_config: object,
) -> None:
    engine = data_engine
    del test_config
    plugin_name = _plugin_name(f"add-{uuid4().hex[:6]}")
    help_id = await engine.add_help(
        title="小鞠使用说明",
        content="输入 .docs 查询帮助。",
        keywords=["docs", "帮助"],
        category="feature",
        plugin_name=plugin_name,
        notes="测试备注",
    )

    fetched = await engine.get_help(help_id)
    assert fetched is not None
    assert fetched.title == "小鞠使用说明"
    assert fetched.keywords == ["docs", "帮助"]
    assert fetched.notes == "测试备注"
    assert await engine.get_help(999_999_999) is None

    by_keyword = await engine.search_by_keyword("docs")
    assert any(hit.id == help_id for hit in by_keyword)

    items, total = await engine.list_help(limit=10, offset=0, query="小鞠")
    assert total == 1
    assert items[0].id == help_id

    _by_category, total_c = await engine.list_help(
        limit=10, offset=0, query="小鞠", category="other"
    )
    assert total_c == 0

    escaped, total_e = await engine.list_help(
        limit=10, offset=0, query=r"100%_x\tag"
    )
    assert total_e == 0
    assert escaped == []


@pytest.mark.skipif(not POSTGRES_URL, reason="未配置真实 PostgreSQL 测试连接")
@pytest.mark.asyncio
async def test_scan_lease_multi_worker_semantics(data_engine: HelpEngine) -> None:
    engine = data_engine
    first_token = f"worker-a-{uuid4().hex}"
    second_token = f"worker-b-{uuid4().hex}"
    try:
        assert await engine.acquire_scan_lease(first_token, lease_seconds=30) is True
        assert await engine.acquire_scan_lease(second_token, lease_seconds=30) is False
        assert await engine.renew_scan_lease(second_token, lease_seconds=30) is False
        assert await engine.renew_scan_lease(first_token, lease_seconds=30) is True
        await engine.release_scan_lease(first_token)
        assert await engine.acquire_scan_lease(second_token, lease_seconds=30) is True
    finally:
        await engine.release_scan_lease(second_token)
        await engine.release_scan_lease(first_token)


@pytest.mark.skipif(not POSTGRES_URL, reason="未配置真实 PostgreSQL 测试连接")
@pytest.mark.asyncio
async def test_sync_auto_generated_help_skip_unchanged(
    data_engine: HelpEngine,
    test_config: object,
) -> None:
    engine = data_engine
    del test_config
    plugin_name = _plugin_name(f"sync-{uuid4().hex[:6]}")
    assert await engine.sync_auto_generated_help(
        plugin_name=plugin_name,
        title="演示插件",
        content="/demo help",
        keywords=["演示", "帮助"],
        rebuild_index=False,
    ) is True
    assert await engine.sync_auto_generated_help(
        plugin_name=plugin_name,
        title="演示插件",
        content="/demo help",
        keywords=["帮助", "演示"],
        rebuild_index=False,
    ) is False

    manual_id = await engine.add_help(
        title="人工条目",
        content="人工内容",
        keywords=["人工"],
        plugin_name=_plugin_name(f"manual-{uuid4().hex[:6]}"),
    )
    manual = await engine.get_help(manual_id)
    assert manual is not None
    assert manual.is_auto_generated is False
    assert manual.plugin_name is not None
    blocked = await engine.sync_auto_generated_help(
        plugin_name=manual.plugin_name,
        title="自动覆盖尝试",
        content="不应写入",
        keywords=[],
        rebuild_index=False,
    )
    assert blocked is False
    after = await engine.get_help(manual_id)
    assert after is not None
    assert after.title == "人工条目"


@pytest.mark.skipif(not POSTGRES_URL, reason="未配置真实 PostgreSQL 测试连接")
@pytest.mark.asyncio
async def test_sync_auto_generated_help_updates_changed_content(
    data_engine: HelpEngine,
    test_config: object,
) -> None:
    engine = data_engine
    del test_config
    plugin_name = _plugin_name(f"sync-upd-{uuid4().hex[:6]}")
    assert await engine.sync_auto_generated_help(
        plugin_name=plugin_name,
        title="旧标题",
        content="旧内容",
        keywords=["旧词"],
        rebuild_index=False,
    ) is True
    assert await engine.sync_auto_generated_help(
        plugin_name=plugin_name,
        title="新标题",
        content="新内容",
        keywords=["新词"],
        rebuild_index=False,
    ) is True

    items, total = await engine.list_help(limit=10, offset=0, query="新标题")
    assert total == 1
    assert items[0].title == "新标题"
    assert items[0].keywords == ["新词"]


@pytest.mark.skipif(not POSTGRES_URL, reason="未配置真实 PostgreSQL 测试连接")
@pytest.mark.asyncio
async def test_delete_auto_generated_help_by_plugins(
    data_engine: HelpEngine,
    test_config: object,
) -> None:
    engine = data_engine
    del test_config
    auto_plugin = _plugin_name(f"del-auto-{uuid4().hex[:6]}")
    manual_plugin = _plugin_name(f"del-manual-{uuid4().hex[:6]}")
    assert await engine.sync_auto_generated_help(
        plugin_name=auto_plugin,
        title="自动条目",
        content="自动内容",
        keywords=[],
        rebuild_index=False,
    ) is True
    manual_id = await engine.add_help(
        title="人工条目",
        content="人工内容",
        keywords=[],
        plugin_name=manual_plugin,
    )

    deleted = await engine.delete_auto_generated_help_by_plugins(
        {auto_plugin, manual_plugin},
        rebuild_index=False,
    )
    assert deleted == 1
    assert await engine.get_help(manual_id) is not None


@pytest.mark.skipif(not POSTGRES_URL, reason="未配置真实 PostgreSQL 测试连接")
@pytest.mark.asyncio
async def test_update_help_cas_and_delete(
    data_engine: HelpEngine,
    test_config: object,
) -> None:
    engine = data_engine
    del test_config
    plugin_name = _plugin_name(f"upd-{uuid4().hex[:6]}")
    help_id = await engine.add_help(
        title="旧标题",
        content="旧内容",
        keywords=["旧词"],
        plugin_name=plugin_name,
        notes="旧备注",
    )

    embedding_inputs: list[str] = []

    async def _recording_embedding(text: str) -> list[float]:
        embedding_inputs.append(text)
        import hashlib

        digest = hashlib.sha256(text.encode("utf-8")).digest()
        return [
            digest[index % len(digest)] / 255.0
            for index in range(EMBEDDING_DIMENSION)
        ]

    engine._get_embedding = _recording_embedding  # type: ignore[method-assign]

    assert await engine.update_help(help_id, title="  新标题  ") is True
    # 标题更新必须用规范化标题 + 原内容重新生成向量
    assert embedding_inputs == ["新标题\n旧内容"]
    assert await engine.update_help(help_id, notes=None) is True
    fetched = await engine.get_help(help_id)
    assert fetched is not None
    assert fetched.title == "新标题"
    assert fetched.notes is None

    assert await engine.update_help(999_999_999, title="不存在") is False

    assert await engine.delete_help(help_id) is True
    assert await engine.delete_help(help_id) is False
    assert await engine.get_help(help_id) is None


# ---------- 生命周期 ----------


@pytest.mark.skipif(not POSTGRES_URL, reason="未配置真实 PostgreSQL 测试连接")
@pytest.mark.asyncio
async def test_close_resets_engine_and_keeps_shared_engine_usable(
    initialized_engine: HelpEngine,
) -> None:
    engine = initialized_engine
    await engine.close()
    assert engine._pool is None
    assert engine._initialized is False
    assert engine._keyword_index.loaded is False

    from komari_bot.common.orm_connection import get_shared_orm_connection_pool

    pool = get_shared_orm_connection_pool()
    async with pool.acquire() as conn:  # type: ignore[attr-defined]
        value = await conn.fetchval("SELECT 1")
        assert value == 1
