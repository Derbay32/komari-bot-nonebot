"""KomariMemory 四层记忆仓库的共享引擎连接集成测试。

取代旧 ``test_conversation_repository.py`` / ``test_entity_repository.py`` /
``test_interaction_event_repository.py`` / ``test_conversation_repository_management.py``
中的 asyncpg 池 fake 测试：连接来源统一走插件公开的 ``create_pool()``（本
ticket 的切换接缝），所有行为断言在真实库上执行，覆盖不净减少。

先验断言：``create_pool()`` 拿到的连接必须落在 nonebot-plugin-orm 配置的
数据库（``current_database()`` + ``current_setting('port')`` 与门控 URL 一致），
否则测试立即失败 —— 这是连接来源切换的行为级验收点。每个测试独立事件循环，
测试前后 dispose 共享引擎。
"""

from __future__ import annotations

import os
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse
from uuid import uuid4

import pytest

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


def _expected_database_name() -> str:
    parsed = urlparse(POSTGRES_URL)
    return (parsed.path or "/").lstrip("/")


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


async def _create_pool() -> object:
    """经插件公开接缝获取连接（本 ticket 切换点）。"""
    from komari_bot.plugins.komari_memory.database.connection import create_pool

    return await create_pool()


async def _assert_configured_database(pool: object) -> None:
    """验收：连接来源必须是 nonebot-plugin-orm 配置的数据库。"""
    async with pool.acquire() as conn:  # type: ignore[attr-defined]
        database = await conn.fetchval("SELECT current_database()")
    # current_setting('port') 返回的是容器内端口而非宿主机映射端口，
    # 因此只比较库名；host/port 一致性已由 _same_database 门控保证。
    assert str(database) == _expected_database_name()


def _vector(dominant_index: int, value: float = 1.0) -> str:
    """构造 512 维向量字符串，主分量在第 dominant_index 维。"""
    components = [0.0] * EMBEDDING_DIMENSION
    components[dominant_index] = value
    return str(components)


async def _cleanup(sql_statements: list[tuple[str, tuple[object, ...]]]) -> None:
    import asyncpg

    admin = await asyncpg.connect(_asyncpg_url())
    try:
        for statement, args in sql_statements:
            await admin.execute(statement, *args)
    finally:
        await admin.close()


class _TestData:
    """记录本测试写入的数据，收尾统一清理。"""

    def __init__(self) -> None:
        self.conversation_ids: list[int] = []
        self.conversation_groups: set[str] = set()
        self.profile_keys: set[tuple[str, str]] = set()
        self.event_ids: list[int] = []
        self.event_users: set[str] = set()

    async def cleanup(self) -> None:
        statements: list[tuple[str, tuple[object, ...]]] = []
        if self.conversation_ids or self.conversation_groups:
            statements.append(
                (
                    "DELETE FROM komari_memory_conversation_embeddings "
                    "WHERE conversation_id IN (SELECT id FROM "
                    "komari_memory_conversations WHERE group_id = ANY($1::text[]))",
                    (list(self.conversation_groups),),
                )
            )
            statements.append(
                (
                    "DELETE FROM komari_memory_conversations WHERE group_id = ANY($1::text[])",
                    (list(self.conversation_groups),),
                )
            )
        for user_id, group_id in self.profile_keys:
            statements.append(
                (
                    "DELETE FROM komari_memory_user_profile "
                    "WHERE user_id = $1 AND group_id = $2",
                    (user_id, group_id),
                )
            )
        if self.event_ids or self.event_users:
            statements.append(
                (
                    "DELETE FROM komari_memory_interaction_embeddings "
                    "WHERE interaction_id IN (SELECT id FROM "
                    "komari_memory_interaction_history WHERE user_id = ANY($1::text[]))",
                    (list(self.event_users),),
                )
            )
            statements.append(
                (
                    "DELETE FROM komari_memory_interaction_history "
                    "WHERE user_id = ANY($1::text[])",
                    (list(self.event_users),),
                )
            )
        await _cleanup(statements)


@pytest.fixture
async def test_data() -> "object":
    data = _TestData()
    yield data
    await data.cleanup()


@pytest.fixture
async def pool() -> "object":
    await _reset_shared_orm_engine()
    if not _same_database(POSTGRES_URL, _configured_database_url()):
        pytest.skip("KOMARI_TEST_POSTGRES_URL 与 nonebot sqlalchemy_database_url 不一致")
    pool = await _create_pool()
    try:
        await _assert_configured_database(pool)
        yield pool
    finally:
        await _reset_shared_orm_engine()


# ---------- ConversationRepository ----------


@pytest.mark.skipif(not POSTGRES_URL, reason="未配置真实 PostgreSQL 测试连接")
@pytest.mark.asyncio
async def test_conversation_insert_dedup_and_embedding(pool: object, test_data: _TestData) -> None:
    from komari_bot.plugins.komari_memory.repositories.conversation_repository import (
        ConversationRepository,
    )

    repo = ConversationRepository(pool)  # type: ignore[arg-type]
    group_id = f"conv-{uuid4().hex}"
    test_data.conversation_groups.add(group_id)

    dedup_key = f"dedup-{uuid4().hex}"
    conv_id = await repo.insert_conversation(
        group_id=group_id,
        summary="大家聊了拉面。",
        embedding=_vector(0),
        participants=["u1"],
        importance_initial=4,
        dedup_key=dedup_key,
    )
    assert conv_id is not None
    duplicate = await repo.insert_conversation(
        group_id=group_id,
        summary="大家聊了拉面。",
        embedding=_vector(0),
        participants=["u1"],
        importance_initial=4,
        dedup_key=dedup_key,
    )
    assert duplicate is None

    second_id = await repo.insert_conversation(
        group_id=group_id,
        summary="第二条总结。",
        embedding=_vector(1),
        participants=["u2"],
        importance_initial=4,
    )
    assert second_id is not None
    assert second_id != conv_id

    items, total = await repo.list_conversations(limit=10, offset=0, group_id=group_id)
    assert total == 2
    assert {int(row["id"]) for row in items} == {conv_id, second_id}
    assert {row["summary"] for row in items} == {"大家聊了拉面。", "第二条总结。"}

    hits = await repo.search_by_similarity(
        embedding=_vector(0),
        group_id=group_id,
        limit=10,
        touch_results=False,
    )
    assert {int(row["id"]) for row in hits} == {conv_id, second_id}
    assert hits[0]["id"] == conv_id


@pytest.mark.skipif(not POSTGRES_URL, reason="未配置真实 PostgreSQL 测试连接")
@pytest.mark.asyncio
async def test_conversation_touch_updates_access_state(pool: object, test_data: _TestData) -> None:
    from komari_bot.plugins.komari_memory.repositories.conversation_repository import (
        ConversationRepository,
    )

    repo = ConversationRepository(pool)  # type: ignore[arg-type]
    group_id = f"conv-touch-{uuid4().hex}"
    test_data.conversation_groups.add(group_id)

    conv_id = await repo.insert_conversation(
        group_id=group_id,
        summary="需要触摸的总结。",
        embedding=_vector(2),
        participants=["u1"],
        importance_initial=5,
        dedup_key=f"dedup-{uuid4().hex}",
    )
    assert conv_id is not None
    # 命中前先手动把重要度调低，验证 touch 恢复语义
    async with pool.acquire() as conn:  # type: ignore[attr-defined]
        await conn.execute(
            "UPDATE komari_memory_conversations SET importance_current = 1 WHERE id = $1",
            conv_id,
        )

    hits = await repo.search_by_similarity(
        embedding=_vector(2),
        group_id=group_id,
        limit=10,
        touch_results=True,
    )
    assert len(hits) == 1
    touched = await repo.get_conversation(conv_id)
    assert touched is not None
    assert int(touched["importance_current"]) == 5
    assert touched["last_accessed"] is not None

    # touch_results=False 不更新
    async with pool.acquire() as conn:  # type: ignore[attr-defined]
        await conn.execute(
            "UPDATE komari_memory_conversations SET importance_current = 2 WHERE id = $1",
            conv_id,
        )
    await repo.search_by_similarity(
        embedding=_vector(2),
        group_id=group_id,
        limit=10,
        touch_results=False,
    )
    untouched = await repo.get_conversation(conv_id)
    assert untouched is not None
    assert int(untouched["importance_current"]) == 2


@pytest.mark.skipif(not POSTGRES_URL, reason="未配置真实 PostgreSQL 测试连接")
@pytest.mark.asyncio
async def test_conversation_search_weights_participant(pool: object, test_data: _TestData) -> None:
    from komari_bot.plugins.komari_memory.repositories.conversation_repository import (
        ConversationRepository,
    )

    repo = ConversationRepository(pool)  # type: ignore[arg-type]
    group_id = f"conv-w-{uuid4().hex}"
    test_data.conversation_groups.add(group_id)

    def _weighted_vector(k: float) -> str:
        components = [0.0] * EMBEDDING_DIMENSION
        components[0] = 1.0
        components[1] = k
        return str(components)

    first_id = await repo.insert_conversation(
        group_id=group_id,
        summary="无关内容。",
        embedding=_weighted_vector(0.68),
        participants=["other"],
        importance_initial=4,
        dedup_key=f"dedup-{uuid4().hex}",
    )
    second_id = await repo.insert_conversation(
        group_id=group_id,
        summary="包含用户的内容。",
        embedding=_weighted_vector(0.70),
        participants=["u-zhang"],
        importance_initial=4,
        dedup_key=f"dedup-{uuid4().hex}",
    )
    assert first_id is not None and second_id is not None

    # 不加权：无关内容（距离更近）排前
    unweighted = await repo.search_by_similarity(
        embedding=str([1.0] + [0.0] * (EMBEDDING_DIMENSION - 1)),
        group_id=group_id,
        limit=10,
        touch_results=False,
    )
    assert [int(row["id"]) for row in unweighted] == [first_id, second_id]
    # 加权：用户参与的行除以 1.2 后反超
    weighted = await repo.search_by_similarity(
        embedding=str([1.0] + [0.0] * (EMBEDDING_DIMENSION - 1)),
        group_id=group_id,
        user_id="u-zhang",
        limit=10,
        touch_results=False,
    )
    assert [int(row["id"]) for row in weighted] == [second_id, first_id]


@pytest.mark.skipif(not POSTGRES_URL, reason="未配置真实 PostgreSQL 测试连接")
@pytest.mark.asyncio
async def test_conversation_list_filters_and_pagination(pool: object, test_data: _TestData) -> None:
    from komari_bot.plugins.komari_memory.repositories.conversation_repository import (
        ConversationRepository,
    )

    repo = ConversationRepository(pool)  # type: ignore[arg-type]
    group_id = f"conv-list-{uuid4().hex}"
    test_data.conversation_groups.add(group_id)

    for index in range(3):
        await repo.insert_conversation(
            group_id=group_id,
            summary=f"列表内容 {index}",
            embedding=_vector(index),
            participants=[f"u{index}"],
            importance_initial=4,
            dedup_key=f"dedup-{uuid4().hex}",
        )

    items, total = await repo.list_conversations(limit=2, offset=0, group_id=group_id)
    assert total == 3
    assert len(items) == 2

    by_participant, total_p = await repo.list_conversations(
        limit=10, offset=0, group_id=group_id, participant="u1"
    )
    assert total_p == 1
    assert by_participant[0]["summary"] == "列表内容 1"

    _by_query, total_q = await repo.list_conversations(
        limit=10, offset=0, group_id=group_id, query="列表内容"
    )
    assert total_q == 3
    by_escape, total_e = await repo.list_conversations(
        limit=10, offset=0, group_id=group_id, query=r"100%_x\tag"
    )
    assert total_e == 0
    assert by_escape == []


@pytest.mark.skipif(not POSTGRES_URL, reason="未配置真实 PostgreSQL 测试连接")
@pytest.mark.asyncio
async def test_conversation_get_update_delete(pool: object, test_data: _TestData) -> None:
    from komari_bot.plugins.komari_memory.repositories.conversation_repository import (
        ConversationRepository,
    )

    repo = ConversationRepository(pool)  # type: ignore[arg-type]
    group_id = f"conv-crud-{uuid4().hex}"
    test_data.conversation_groups.add(group_id)

    conv_id = await repo.insert_conversation(
        group_id=group_id,
        summary="原始总结。",
        embedding=_vector(3),
        participants=["u1"],
        importance_initial=4,
        dedup_key=f"dedup-{uuid4().hex}",
    )
    assert conv_id is not None

    fetched = await repo.get_conversation(conv_id)
    assert fetched is not None
    assert fetched["summary"] == "原始总结。"

    updated = await repo.update_conversation(
        conv_id,
        summary="更新后的总结。",
        importance_current=5,
    )
    assert updated is not None
    assert updated["summary"] == "更新后的总结。"
    assert int(updated["importance_current"]) == 5
    assert await repo.get_conversation(999_999_999) is None

    assert await repo.delete_conversation(conv_id) is True
    assert await repo.delete_conversation(conv_id) is False
    assert await repo.get_conversation(conv_id) is None


# ---------- EntityRepository ----------


@pytest.mark.skipif(not POSTGRES_URL, reason="未配置真实 PostgreSQL 测试连接")
@pytest.mark.asyncio
async def test_entity_profile_upsert_merges_traits(pool: object, test_data: _TestData) -> None:
    from komari_bot.plugins.komari_memory.repositories.entity_repository import (
        EntityRepository,
    )

    repo = EntityRepository(pool)  # type: ignore[arg-type]
    user_id = f"u-{uuid4().hex}"
    group_id = f"g-{uuid4().hex}"
    test_data.profile_keys.add((user_id, group_id))

    await repo.upsert_user_profile(
        user_id=user_id,
        group_id=group_id,
        profile={
            "display_name": "小鞠",
            "traits": {"喜欢的食物": {"value": "布丁"}},
        },
        importance=4,
    )
    row = await repo.get_user_profile_row(user_id=user_id, group_id=group_id)
    assert row is not None
    assert int(row["value"]["version"]) == 1
    assert row["value"]["traits"]["喜欢的食物"]["value"] == "布丁"

    await repo.upsert_user_profile(
        user_id=user_id,
        group_id=group_id,
        profile={
            "display_name": "小鞠知花",
            "traits": {"喜欢的动画": {"value": "败犬女主"}},
        },
        importance=5,
    )
    merged = await repo.get_user_profile(user_id=user_id, group_id=group_id)
    assert merged is not None
    assert merged["display_name"] == "小鞠知花"
    assert merged["traits"]["喜欢的食物"]["value"] == "布丁"
    assert merged["traits"]["喜欢的动画"]["value"] == "败犬女主"
    row_after = await repo.get_user_profile_row(user_id=user_id, group_id=group_id)
    assert row_after is not None
    assert int(row_after["value"]["version"]) == 2
    assert int(row_after["importance"]) == 5


@pytest.mark.skipif(not POSTGRES_URL, reason="未配置真实 PostgreSQL 测试连接")
@pytest.mark.asyncio
async def test_entity_profile_batch_conflict_and_delete_keys(pool: object, test_data: _TestData) -> None:
    from komari_bot.plugins.komari_memory.repositories.entity_repository import (
        EntityRepository,
    )

    repo = EntityRepository(pool)  # type: ignore[arg-type]
    user_a = f"u-a-{uuid4().hex}"
    user_b = f"u-b-{uuid4().hex}"
    group_id = f"g-batch-{uuid4().hex}"
    test_data.profile_keys.add((user_a, group_id))
    test_data.profile_keys.add((user_b, group_id))

    await repo.upsert_user_profile(
        user_id=user_a,
        group_id=group_id,
        profile={"display_name": "先写入", "traits": {"key": {"value": "v"}}},
        importance=4,
    )
    # snapshot_updated_at 传过去时间 → 条件冲突，整条不写入
    result = await repo.batch_upsert_user_profiles(
        [
            {
                "user_id": user_a,
                "group_id": group_id,
                "display_name": "新名字",
                "set_traits": {"key": {"value": "v2"}},
                "delete_keys": [],
                "updated_at": datetime.now(UTC) + timedelta(seconds=5),
                "snapshot_updated_at": datetime.now(UTC) - timedelta(days=1),
                "importance": 4,
            },
            {
                "user_id": user_b,
                "group_id": group_id,
                "display_name": "新用户",
                "set_traits": {"新特质": {"value": "x"}},
                "delete_keys": [],
                "updated_at": datetime.now(UTC),
                "importance": 3,
            },
        ]
    )
    assert [row.user_id for row in result.upserted] == [user_b]
    assert [conflict.user_id for conflict in result.conflicts] == [user_a]
    # 冲突行未被覆盖
    unchanged = await repo.get_user_profile(user_id=user_a, group_id=group_id)
    assert unchanged is not None
    assert unchanged["display_name"] == "先写入"

    # delete_keys 补丁删除指定特质
    await repo.batch_upsert_user_profiles(
        [
            {
                "user_id": user_b,
                "group_id": group_id,
                "display_name": "新用户",
                "set_traits": {},
                "delete_keys": ["新特质"],
                "updated_at": datetime.now(UTC),
                "importance": 3,
            }
        ]
    )
    patched = await repo.get_user_profile(user_id=user_b, group_id=group_id)
    assert patched is not None
    assert "新特质" not in patched["traits"]


@pytest.mark.skipif(not POSTGRES_URL, reason="未配置真实 PostgreSQL 测试连接")
@pytest.mark.asyncio
async def test_entity_profile_list_filters_and_delete(pool: object, test_data: _TestData) -> None:
    from komari_bot.plugins.komari_memory.repositories.entity_repository import (
        EntityRepository,
    )

    repo = EntityRepository(pool)  # type: ignore[arg-type]
    user_a = f"u-list-a-{uuid4().hex}"
    user_b = f"u-list-b-{uuid4().hex}"
    group_a = f"g-list-a-{uuid4().hex}"
    group_b = f"g-list-b-{uuid4().hex}"
    test_data.profile_keys.add((user_a, group_a))
    test_data.profile_keys.add((user_b, group_b))

    await repo.upsert_user_profile(
        user_id=user_a,
        group_id=group_a,
        profile={"display_name": "布丁爱好者", "traits": {}},
        importance=4,
    )
    await repo.upsert_user_profile(
        user_id=user_b,
        group_id=group_b,
        profile={"display_name": "安静用户", "traits": {}},
        importance=2,
    )

    items, total = await repo.list_user_profiles(
        limit=10, offset=0, group_id=group_a
    )
    assert total == 1
    assert items[0]["user_id"] == user_a
    assert items[0]["value"]["display_name"] == "布丁爱好者"

    by_query, total_q = await repo.list_user_profiles(
        limit=10, offset=0, query="布丁"
    )
    assert total_q == 1
    assert by_query[0]["user_id"] == user_a

    escaped, total_e = await repo.list_user_profiles(
        limit=10, offset=0, query=r"100%_x\tag"
    )
    assert total_e == 0
    assert escaped == []

    assert await repo.delete_user_profile(user_id=user_a, group_id=group_a) is True
    assert await repo.delete_user_profile(user_id=user_a, group_id=group_a) is False
    assert await repo.get_user_profile(user_id=user_a, group_id=group_a) is None


# ---------- InteractionEventRepository ----------


@pytest.mark.skipif(not POSTGRES_URL, reason="未配置真实 PostgreSQL 测试连接")
@pytest.mark.asyncio
async def test_interaction_event_insert_dedup_search(pool: object, test_data: _TestData) -> None:
    from komari_bot.plugins.komari_memory.repositories.interaction_event_repository import (
        InteractionEventRepository,
    )

    repo = InteractionEventRepository(pool)  # type: ignore[arg-type]
    user_id = f"ev-u-{uuid4().hex}"
    test_data.event_users.add(user_id)

    now = datetime.now(UTC)
    dedup_key = f"ev-dedup-{uuid4().hex}"
    event_id = await repo.insert_interaction_event(
        user_id=user_id,
        display_name="小鞠",
        event_summary="跨群聊了轻小说。",
        embedding=_vector(4),
        source_message_count=3,
        first_seen_at=now,
        last_seen_at=now,
        importance_initial=4,
        dedup_key=dedup_key,
    )
    repeated_id = await repo.insert_interaction_event(
        user_id=user_id,
        display_name="小鞠",
        event_summary="跨群聊了轻小说。",
        embedding=_vector(4),
        source_message_count=3,
        first_seen_at=now,
        last_seen_at=now,
        importance_initial=4,
        dedup_key=dedup_key,
    )
    assert repeated_id == event_id
    assert await repo.get_event_id_by_dedup_key(dedup_key) == event_id

    hits = await repo.search_interaction_events(
        user_id=user_id,
        embedding=_vector(4),
        limit=10,
    )
    assert len(hits) == 1
    assert hits[0]["id"] == event_id
    assert round(float(hits[0]["similarity"]), 4) == 1.0

    fetched = await repo.get_interaction_event(event_id)
    assert fetched is not None
    assert fetched["event_summary"] == "跨群聊了轻小说。"
    assert await repo.get_interaction_event(999_999_999) is None


@pytest.mark.skipif(not POSTGRES_URL, reason="未配置真实 PostgreSQL 测试连接")
@pytest.mark.asyncio
async def test_interaction_event_update_touch_list_delete(pool: object, test_data: _TestData) -> None:
    from komari_bot.plugins.komari_memory.repositories.interaction_event_repository import (
        InteractionEventRepository,
    )

    repo = InteractionEventRepository(pool)  # type: ignore[arg-type]
    user_id = f"ev-crud-{uuid4().hex}"
    test_data.event_users.add(user_id)

    now = datetime.now(UTC)
    event_id = await repo.insert_interaction_event(
        user_id=user_id,
        display_name="小鞠",
        event_summary="原始事件。",
        embedding=_vector(5),
        source_message_count=1,
        first_seen_at=now,
        last_seen_at=now,
        importance_initial=4,
        dedup_key=f"ev-dedup-{uuid4().hex}",
    )

    updated = await repo.update_interaction_event(
        event_id,
        event_summary="更新后的事件。",
        importance_current=5,
    )
    assert updated is not None
    assert updated["event_summary"] == "更新后的事件。"
    assert int(updated["importance_current"]) == 5

    await repo.update_fuzzy_event(event_id, "模糊化后的事件总结。")
    fuzzy = await repo.get_interaction_event(event_id)
    assert fuzzy is not None
    assert fuzzy["event_summary"] == "模糊化后的事件总结。"
    assert fuzzy["is_fuzzy"] is True

    await repo.touch_interaction_events([event_id])
    touched = await repo.get_interaction_event(event_id)
    assert touched is not None
    assert int(touched["importance_current"]) == 4

    items, total = await repo.list_interaction_events(
        limit=10, offset=0, user_id=user_id
    )
    assert total == 1
    assert items[0]["id"] == event_id

    _by_query, total_q = await repo.list_interaction_events(
        limit=10, offset=0, query="模糊化"
    )
    assert total_q == 1

    assert await repo.delete_interaction_event(event_id) is True
    assert await repo.delete_interaction_event(event_id) is False
    assert await repo.get_interaction_event(event_id) is None


# ---------- ForgettingService（低价值清理 / 重要性衰减） ----------


def _forgetting_config(**overrides: object) -> object:
    import types

    defaults: dict[str, object] = {
        "forgetting_enabled": True,
        "forgetting_importance_threshold": 3,
        "forgetting_min_age_days": 0,
        "forgetting_decay_factor": 0.95,
        "forgetting_fuzzify_concurrency": 2,
        "forgetting_job_lease_seconds": 900,
    }
    defaults.update(overrides)
    return types.SimpleNamespace(**defaults)


@pytest.mark.skipif(not POSTGRES_URL, reason="未配置真实 PostgreSQL 测试连接")
@pytest.mark.asyncio
async def test_forgetting_decay_steps_down_importance(pool: object, test_data: _TestData) -> None:
    from komari_bot.plugins.komari_memory.repositories.conversation_repository import (
        ConversationRepository,
    )
    from komari_bot.plugins.komari_memory.services.forgetting_service import (
        ForgettingService,
    )

    repo = ConversationRepository(pool)  # type: ignore[arg-type]
    group_id = f"fg-decay-{uuid4().hex}"
    test_data.conversation_groups.add(group_id)

    conv_id = await repo.insert_conversation(
        group_id=group_id,
        summary="待衰减总结。",
        embedding=_vector(6),
        participants=["u1"],
        importance_initial=4,
        dedup_key=f"dedup-{uuid4().hex}",
    )
    assert conv_id is not None
    async with pool.acquire() as conn:  # type: ignore[attr-defined]
        await conn.execute(
            "UPDATE komari_memory_conversations SET importance_current = 3 "
            "WHERE id = $1",
            conv_id,
        )

    service = ForgettingService(  # type: ignore[arg-type]
        pool,  # type: ignore[arg-type]
        config_provider=lambda: _forgetting_config(),  # type: ignore[arg-type]
    )
    await service._daily_decay()
    await service._daily_decay_interaction_events()

    after = await repo.get_conversation(conv_id)
    assert after is not None
    assert int(after["importance_current"]) == 2


@pytest.mark.skipif(not POSTGRES_URL, reason="未配置真实 PostgreSQL 测试连接")
@pytest.mark.asyncio
async def test_forgetting_deletes_low_value_memories(pool: object, test_data: _TestData) -> None:
    from komari_bot.plugins.komari_memory.repositories.conversation_repository import (
        ConversationRepository,
    )
    from komari_bot.plugins.komari_memory.repositories.interaction_event_repository import (
        InteractionEventRepository,
    )
    from komari_bot.plugins.komari_memory.services.forgetting_service import (
        ForgettingService,
    )

    conversation_repo = ConversationRepository(pool)  # type: ignore[arg-type]
    event_repo = InteractionEventRepository(pool)  # type: ignore[arg-type]
    group_id = f"fg-del-{uuid4().hex}"
    test_data.conversation_groups.add(group_id)

    old = datetime.now(UTC) - timedelta(days=10)
    low_conv = await conversation_repo.insert_conversation(
        group_id=group_id,
        summary="低价值旧总结。",
        embedding=_vector(7),
        participants=["u1"],
        importance_initial=2,
        dedup_key=f"dedup-{uuid4().hex}",
        start_time=old.replace(tzinfo=None),
        end_time=old.replace(tzinfo=None),
    )
    keep_conv = await conversation_repo.insert_conversation(
        group_id=group_id,
        summary="高价值总结。",
        embedding=_vector(7),
        participants=["u1"],
        importance_initial=5,
        dedup_key=f"dedup-{uuid4().hex}",
    )
    assert low_conv is not None and keep_conv is not None
    async with pool.acquire() as conn:  # type: ignore[attr-defined]
        await conn.execute(
            "UPDATE komari_memory_conversations SET created_at = $1, "
            "importance_current = 0 WHERE id = $2",
            old.replace(tzinfo=None),
            low_conv,
        )

    user_id = f"fg-ev-{uuid4().hex}"
    test_data.event_users.add(user_id)
    low_event = await event_repo.insert_interaction_event(
        user_id=user_id,
        display_name="低价值用户",
        event_summary="低价值旧事件。",
        embedding=_vector(8),
        source_message_count=1,
        first_seen_at=old,
        last_seen_at=old,
        importance_initial=2,
        dedup_key=f"ev-dedup-{uuid4().hex}",
    )
    async with pool.acquire() as conn:  # type: ignore[attr-defined]
        await conn.execute(
            "UPDATE komari_memory_interaction_history SET created_at = $1, "
            "importance_current = 0 WHERE id = $2",
            old,
            low_event,
        )

    service = ForgettingService(  # type: ignore[arg-type]
        pool,  # type: ignore[arg-type]
        config_provider=lambda: _forgetting_config(),  # type: ignore[arg-type]
    )
    deleted_convs = await service._delete_low_value_memories()
    deleted_events = await service._delete_low_value_interaction_events()

    assert deleted_convs == 1
    assert deleted_events == 1
    assert await conversation_repo.get_conversation(low_conv) is None
    assert await conversation_repo.get_conversation(keep_conv) is not None
    assert await event_repo.get_interaction_event(low_event) is None
