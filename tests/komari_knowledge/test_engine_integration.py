"""KnowledgeEngine 共享引擎连接集成测试。

取代 ``test_engine_search.py`` / ``test_engine_management.py`` 及
``test_engine_lifecycle.py`` 中 asyncpg 池 fake 测试：连接来源统一走引擎
``initialize()`` 的真实路径或共享引擎租约，检索/管理/生命周期行为全部在
真实库上断言，覆盖不净减少。每个测试独立事件循环，测试前后 dispose 共享
引擎；写入的数据按唯一 group/前缀收尾清理。
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

from komari_bot.plugins.komari_knowledge import engine as engine_module
from komari_bot.plugins.komari_knowledge.engine import KnowledgeEngine

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


async def _cleanup_knowledge(content_prefixes: list[str]) -> None:
    import asyncpg

    admin = await asyncpg.connect(_asyncpg_url())
    try:
        for prefix in content_prefixes:
            await admin.execute(
                "DELETE FROM komari_knowledge WHERE content LIKE $1",
                f"{prefix}%",
            )
    finally:
        await admin.close()


_KNOWLEDGE_TEST_PREFIXES = [
    "幂等知识 ",
    "关键词命中内容 ",
    "向量补充内容 ",
    "按关键词查询的内容 ",
    "小鞠喜欢布丁",
    "列表知识-",
    "更新管理内容 ",
    "关闭分层内容",
]


@pytest.fixture
async def test_config(
    monkeypatch: pytest.MonkeyPatch,
) -> "AsyncIterator[engine_module.DynamicConfigSchema]":
    """模块级 get_config 打桩（配置属于业务输入，不涉及连接来源）。"""
    config = engine_module.DynamicConfigSchema()
    monkeypatch.setattr(engine_module, "get_config", lambda: config)
    yield config


@pytest.fixture
async def initialized_engine() -> "AsyncIterator[KnowledgeEngine]":
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

    engine = KnowledgeEngine()
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
async def data_engine() -> "AsyncIterator[KnowledgeEngine]":
    """直接挂共享引擎租约的引擎（检索/管理行为断言；非 fake 池）。"""
    await _reset_shared_orm_engine()
    if not _same_database(POSTGRES_URL, _configured_database_url()):
        pytest.skip("KOMARI_TEST_POSTGRES_URL 与 nonebot sqlalchemy_database_url 不一致")
    from komari_bot.common.orm_connection import get_shared_orm_connection_pool

    engine = KnowledgeEngine()
    engine._pool = get_shared_orm_connection_pool()

    async def _noop_cleanup() -> None:
        return None

    engine._embedding_service = SimpleNamespace(
        config=SimpleNamespace(embedding_dimension=EMBEDDING_DIMENSION),
        cleanup=_noop_cleanup,
    )

    async def _fake_embedding(text: str) -> list[float]:
        # 按内容确定性生成向量，避免外部网络调用
        import hashlib

        digest = hashlib.sha256(text.encode("utf-8")).digest()
        components = [0.0] * EMBEDDING_DIMENSION
        for index in range(EMBEDDING_DIMENSION):
            components[index] = digest[index % len(digest)] / 255.0
        return components

    engine._get_embedding = _fake_embedding  # type: ignore[method-assign]
    try:
        await engine._build_keyword_index()
        yield engine
    finally:
        await engine.close()
        await _cleanup_knowledge(_KNOWLEDGE_TEST_PREFIXES)
        await _reset_shared_orm_engine()


# ---------- 初始化与连接来源 ----------


@pytest.mark.skipif(not POSTGRES_URL, reason="未配置真实 PostgreSQL 测试连接")
@pytest.mark.asyncio
async def test_initialize_bootstraps_pool_from_shared_engine(
    initialized_engine: KnowledgeEngine,
) -> None:
    engine = initialized_engine
    assert engine._initialized is True
    assert engine._pool is not None
    # 验收：初始化建立的连接必须落在 nonebot-plugin-orm 配置的数据库
    async with engine._pool.acquire() as conn:
        database = await conn.fetchval("SELECT current_database()")
    expected = (urlparse(POSTGRES_URL).path or "/").lstrip("/")
    assert str(database) == expected
    assert engine._keyword_index.loaded is True


# ---------- 检索行为 ----------


@pytest.mark.skipif(not POSTGRES_URL, reason="未配置真实 PostgreSQL 测试连接")
@pytest.mark.asyncio
async def test_add_knowledge_source_key_is_idempotent(
    data_engine: KnowledgeEngine,
    test_config: engine_module.DynamicConfigSchema,
) -> None:
    engine = data_engine
    del test_config
    content = f"幂等知识 {uuid4().hex}"
    keyword = f"幂等-{uuid4().hex[:8]}"
    source_key = f"t10:proposal:{uuid4().hex}"

    first_id = await engine.add_knowledge(
        content,
        [keyword],
        "custom",
        source_key=source_key,
    )
    second_id = await engine.add_knowledge(
        content,
        [keyword],
        "custom",
        source_key=source_key,
    )
    assert first_id == second_id
    fetched = await engine.get_knowledge(first_id)
    assert fetched is not None
    assert fetched.content == content
    assert fetched.keywords == [keyword]
    assert fetched.category == "custom"


@pytest.mark.skipif(not POSTGRES_URL, reason="未配置真实 PostgreSQL 测试连接")
@pytest.mark.asyncio
async def test_search_hybrid_keyword_then_vector(
    data_engine: KnowledgeEngine,
    test_config: engine_module.DynamicConfigSchema,
) -> None:
    engine = data_engine
    config = test_config
    config.total_limit = 5
    config.layer1_limit = 3
    config.layer2_limit = 2
    config.similarity_threshold = 0.0
    config.query_rewrite_rules = {}

    keyword_token = f"独特词条-{uuid4().hex[:8]}"
    keyword_hit = await engine.add_knowledge(
        f"关键词命中内容 {uuid4().hex}",
        [keyword_token],
        "general",
    )
    vector_only = await engine.add_knowledge(
        f"向量补充内容 {uuid4().hex}",
        [f"无关词-{uuid4().hex[:8]}"],
        "general",
    )

    hits = await engine.search(
        keyword_token,
        limit=5,
        query_vec=[1.0] * EMBEDDING_DIMENSION,
    )
    assert hits, "关键词检索至少应命中一条"
    assert hits[0].id == keyword_hit
    assert hits[0].source == "keyword"
    # 向量层补漏（旧行也会参与排名，只断言来源与去重）
    assert any(hit.source == "vector" for hit in hits)
    assert len({hit.id for hit in hits}) == len(hits)
    assert vector_only in {hit.id for hit in hits}


@pytest.mark.skipif(not POSTGRES_URL, reason="未配置真实 PostgreSQL 测试连接")
@pytest.mark.asyncio
async def test_search_by_keyword_exact_match(
    data_engine: KnowledgeEngine,
    test_config: engine_module.DynamicConfigSchema,
) -> None:
    engine = data_engine
    del test_config
    keyword = f"精确词-{uuid4().hex[:8]}"
    knowledge_id = await engine.add_knowledge(
        f"按关键词查询的内容 {uuid4().hex}",
        [keyword],
        "general",
    )

    hits = await engine.search_by_keyword(keyword)
    assert [hit.id for hit in hits] == [knowledge_id]
    assert await engine.search_by_keyword("不存在的词-xyz") == []


@pytest.mark.skipif(not POSTGRES_URL, reason="未配置真实 PostgreSQL 测试连接")
@pytest.mark.asyncio
async def test_search_rewrite_discards_stale_embedding(
    data_engine: KnowledgeEngine,
    test_config: engine_module.DynamicConfigSchema,
) -> None:
    engine = data_engine
    config = test_config
    config.total_limit = 5
    config.layer1_limit = 0
    config.layer2_limit = 2
    config.similarity_threshold = 0.0
    config.query_rewrite_rules = {"你": "小鞠"}

    await engine.add_knowledge(
        "小鞠喜欢布丁",
        [f"小鞠-{uuid4().hex[:8]}"],
        "general",
    )
    embedding_queries: list[str] = []

    async def _recording_embedding(text: str) -> list[float]:
        embedding_queries.append(text)
        import hashlib

        digest = hashlib.sha256(text.encode("utf-8")).digest()
        return [digest[index % len(digest)] / 255.0 for index in range(EMBEDDING_DIMENSION)]

    engine._get_embedding = _recording_embedding  # type: ignore[method-assign]

    await engine.search("你喜欢什么", limit=5, query_vec=[1.0] * EMBEDDING_DIMENSION)
    # 查询被改写后必须丢弃旧向量重新生成
    assert embedding_queries == ["小鞠喜欢什么"]


# ---------- 管理行为 ----------


@pytest.mark.skipif(not POSTGRES_URL, reason="未配置真实 PostgreSQL 测试连接")
@pytest.mark.asyncio
async def test_list_knowledge_filters_pagination_and_escaping(
    data_engine: KnowledgeEngine,
    test_config: engine_module.DynamicConfigSchema,
) -> None:
    engine = data_engine
    del test_config
    prefix = f"列表知识-{uuid4().hex[:8]}"
    for index in range(3):
        await engine.add_knowledge(
            f"{prefix}-{index}",
            [f"词{index}-{uuid4().hex[:6]}"],
            "general",
        )

    items, total = await engine.list_knowledge(limit=2, offset=0, query=prefix)
    assert total == 3
    assert len(items) == 2

    by_category, total_c = await engine.list_knowledge(
        limit=10, offset=0, query=prefix, category="character"
    )
    assert total_c == 0
    assert by_category == []

    escaped, total_e = await engine.list_knowledge(
        limit=10, offset=0, query=r"100%_x\tag"
    )
    assert total_e == 0
    assert escaped == []


@pytest.mark.skipif(not POSTGRES_URL, reason="未配置真实 PostgreSQL 测试连接")
@pytest.mark.asyncio
async def test_update_knowledge_clear_notes_and_delete(
    data_engine: KnowledgeEngine,
    test_config: engine_module.DynamicConfigSchema,
) -> None:
    engine = data_engine
    del test_config
    knowledge_id = await engine.add_knowledge(
        f"更新管理内容 {uuid4().hex}",
        [f"更新-{uuid4().hex[:8]}"],
        "general",
        notes="旧备注",
    )

    # 仅清空备注：不触碰 embedding（不触发向量重算）
    assert await engine.update_knowledge(knowledge_id, notes=None) is True
    fetched = await engine.get_knowledge(knowledge_id)
    assert fetched is not None
    assert fetched.notes is None

    assert await engine.update_knowledge(999_999_999, notes=None) is False

    all_knowledge = await engine.get_all_knowledge()
    assert any(entry["id"] == knowledge_id for entry in all_knowledge)

    assert await engine.delete_knowledge(knowledge_id) is True
    assert await engine.delete_knowledge(knowledge_id) is False
    assert await engine.get_knowledge(knowledge_id) is None


# ---------- 生命周期 ----------


@pytest.mark.skipif(not POSTGRES_URL, reason="未配置真实 PostgreSQL 测试连接")
@pytest.mark.asyncio
async def test_close_resets_engine_and_keeps_shared_engine_usable(
    initialized_engine: KnowledgeEngine,
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


# ---------- 编排行为（原 fake 测试上移） ----------


@pytest.mark.skipif(not POSTGRES_URL, reason="未配置真实 PostgreSQL 测试连接")
@pytest.mark.asyncio
async def test_search_skips_disabled_layers_and_respects_limits(
    data_engine: KnowledgeEngine,
    test_config: engine_module.DynamicConfigSchema,
) -> None:
    engine = data_engine
    config = test_config
    config.total_limit = 5
    config.layer1_limit = 0
    config.layer2_limit = 0
    config.similarity_threshold = 0.0
    config.query_rewrite_rules = {}

    await engine.add_knowledge("关闭分层内容", [f"关闭-{uuid4().hex[:8]}"], "general")
    assert (
        await engine.search("关闭分层内容", limit=5, query_vec=[1.0] * EMBEDDING_DIMENSION)
        == []
    )

    config.layer1_limit = 1
    config.layer2_limit = 1
    hits = await engine.search(
        "关闭分层内容",
        limit=5,
        query_vec=[1.0] * EMBEDDING_DIMENSION,
    )
    assert len(hits) >= 1
    assert len({hit.id for hit in hits}) == len(hits)


@pytest.mark.skipif(not POSTGRES_URL, reason="未配置真实 PostgreSQL 测试连接")
@pytest.mark.asyncio
async def test_add_knowledge_rejects_budget_before_embedding(
    data_engine: KnowledgeEngine,
    test_config: engine_module.DynamicConfigSchema,
) -> None:
    from komari_bot.common.content_budget import ContentValidationError

    engine = data_engine
    del test_config
    embedding_called = False

    async def _unexpected_embedding(_text: str) -> list[float]:
        nonlocal embedding_called
        embedding_called = True
        return []

    engine._get_embedding = _unexpected_embedding  # type: ignore[method-assign]

    with pytest.raises(ContentValidationError, match="估算 token 上限"):
        await engine.add_knowledge("测" * 6_001, ["测试"])
    assert embedding_called is False
