"""SceneRepository ORM 化后的 PostgreSQL 集成测试。

依赖已执行 ``alembic upgrade head`` 的迁移管理 schema（``KOMARI_TEST_POSTGRES_URL``
门控）。本文件取代旧 ``test_scene_repository_leases.py`` 中与 asyncpg SQL 字符串
耦合的 fake 单测：行为契约（fingerprint 唯一、租约认领/退避/失败标记、并发唯一
认领者、运行时单例切换、场景 CRUD 与快照不可变）全部通过仓储公共接口在真实库
上断言，行为覆盖不减少。数据准备与清理走同一套 SQLModel 表模型。
"""

from __future__ import annotations

import os
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import urlparse
from uuid import uuid4

import pytest
from sqlalchemy import delete, update

from komari_bot.plugins.komari_decision.orm_models import (
    DecisionSceneRow,
    MemorySceneItemRow,
    MemorySceneRuntimeRow,
    MemorySceneSetRow,
)
from komari_bot.plugins.komari_decision.repositories.scene_repository import (
    SceneRepository,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

POSTGRES_URL = os.getenv("KOMARI_TEST_POSTGRES_URL", "")

_SCENES = DecisionSceneRow.__table__
_SETS = MemorySceneSetRow.__table__
_ITEMS = MemorySceneItemRow.__table__
_RUNTIME = MemorySceneRuntimeRow.__table__


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


def _open_session() -> "AsyncSession":
    from nonebot_plugin_orm import get_session

    return get_session(expire_on_commit=False)


def _repository() -> SceneRepository:
    """构造仓储：连接来源切换是 ticket 10 的范围，此处传占位对象。"""
    return SceneRepository(cast("Any", object()))


def _now() -> datetime:
    return datetime.now(UTC)


def _make_scene_key() -> str:
    return f"INTEGRATION_GENERAL_{uuid4().hex}"


def _make_fingerprint() -> str:
    return f"src-{uuid4().hex}"


async def _create_scene(
    repo: SceneRepository,
    scene_key: str,
    *,
    scene_type: str = "general",
    content_text: str = "集成测试场景内容",
    enabled: bool = True,
    order_index: int = 0,
) -> dict[str, Any]:
    return await repo.upsert_scene(
        scene_key=scene_key,
        scene_type=scene_type,
        content_text=content_text,
        enabled=enabled,
        order_index=order_index,
    )


async def _create_building_set(
    repo: SceneRepository,
    *,
    tag: str,
    item_count: int = 2,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """创建一张 BUILDING 场景集（带指定数量 PENDING 条目）。

    每个条目引用独立 scene（``UNIQUE (set_id, scene_id)`` 会按 scene_id
    去重，同一 set 内不能重复引用同一 scene）。任一步失败时自行清理已创建
    数据，避免测试夹具断言失败泄漏行。返回 (scenes, scene_set)。
    """
    scenes = [
        await _create_scene(repo, _make_scene_key(), order_index=index)
        for index in range(item_count)
    ]
    scene_set, created = await repo.get_or_create_scene_set(
        source_path=f"postgresql:integration:{tag}",
        source_hash=_make_fingerprint(),
        embedding_model="integration-model",
        embedding_instruction_hash=f"inst-{tag}",
        status="BUILDING",
    )
    set_id = int(scene_set["id"])
    if not created:
        await _cleanup(
            set_ids=[set_id],
            scene_keys=[s["scene_key"] for s in scenes],
        )
    assert created, "scene set 创建未返回 created=True"
    items = [
        {
            "scene_id": scenes[index]["id"],
            "scene_key": scenes[index]["scene_key"],
            "scene_type": scenes[index]["scene_type"],
            "content_text": f"{scenes[index]['content_text']} #{index}",
            "enabled": True,
            "order_index": index,
            "content_hash": f"hash-{tag}-{index}",
            "status": "PENDING",
        }
        for index in range(item_count)
    ]
    inserted = await repo.insert_scene_items(set_id, items)
    if inserted != item_count:
        await _cleanup(
            set_ids=[set_id],
            scene_keys=[s["scene_key"] for s in scenes],
        )
    assert inserted == item_count, (
        f"scene item 插入数量不符: {item_count} vs {inserted}"
    )
    return scenes, scene_set


async def _cleanup(
    *,
    set_ids: list[int] | None = None,
    scene_keys: list[str] | None = None,
) -> None:
    """按唯一标识清理测试数据，并把运行时单例指针还原为 NULL。"""
    session = _open_session()
    try:
        await session.execute(
            update(_RUNTIME)
            .where(_RUNTIME.c.id == 1)
            .values(active_set_id=None)
        )
        if set_ids:
            await session.execute(
                delete(_ITEMS).where(_ITEMS.c.set_id.in_(set_ids))
            )
            await session.execute(
                delete(_SETS).where(_SETS.c.id.in_(set_ids))
            )
        if scene_keys:
            await session.execute(
                delete(_SCENES).where(_SCENES.c.scene_key.in_(scene_keys))
            )
        await session.commit()
    finally:
        await session.close()


@pytest.mark.skipif(not POSTGRES_URL, reason="未配置真实 PostgreSQL 测试连接")
@pytest.mark.asyncio
async def test_upsert_scene_validates_input() -> None:
    if not _same_database(POSTGRES_URL, _configured_database_url()):
        pytest.skip("KOMARI_TEST_POSTGRES_URL 与 nonebot sqlalchemy_database_url 不一致")
    await _reset_shared_orm_engine()
    repo = _repository()
    try:
        with pytest.raises(ValueError, match="scene_key 不能为空"):
            await repo.upsert_scene(
                scene_key="   ",
                scene_type="general",
                content_text="内容",
            )
        with pytest.raises(ValueError, match="scene_type 只能是 fixed 或 general"):
            await repo.upsert_scene(
                scene_key="SCENE_X",
                scene_type="invalid",
                content_text="内容",
            )
        with pytest.raises(ValueError, match="content_text 不能为空"):
            await repo.upsert_scene(
                scene_key="SCENE_X",
                scene_type="general",
                content_text="   ",
            )
    finally:
        await _cleanup(scene_keys=["SCENE_X"])
        await _reset_shared_orm_engine()


@pytest.mark.skipif(not POSTGRES_URL, reason="未配置真实 PostgreSQL 测试连接")
@pytest.mark.asyncio
async def test_upsert_and_get_scene_roundtrip() -> None:
    if not _same_database(POSTGRES_URL, _configured_database_url()):
        pytest.skip("KOMARI_TEST_POSTGRES_URL 与 nonebot sqlalchemy_database_url 不一致")
    await _reset_shared_orm_engine()
    repo = _repository()
    scene_key = _make_scene_key()
    try:
        created = await _create_scene(
            repo,
            scene_key,
            content_text="  首次写入内容  ",
            order_index=7,
        )
        assert created["scene_key"] == scene_key
        assert created["content_text"] == "首次写入内容"
        assert created["content_hash"] == repo.compute_text_hash("首次写入内容")
        assert created["enabled"] is True
        assert created["order_index"] == 7

        fetched = await repo.get_scene_by_key(scene_key)
        assert fetched is not None
        assert fetched["id"] == created["id"]
        assert fetched["scene_type"] == "general"
        assert fetched["created_at"] is not None
        assert fetched["updated_at"] is not None

        updated = await repo.upsert_scene(
            scene_key=scene_key,
            scene_type="general",
            content_text="修改后内容",
            enabled=False,
            order_index=9,
        )
        assert updated["content_hash"] == repo.compute_text_hash("修改后内容")
        assert updated["enabled"] is False
        assert updated["order_index"] == 9
        assert updated["id"] == created["id"]

        assert await repo.get_scene_by_key("SCENE_NOT_EXISTS") is None
    finally:
        await _cleanup(scene_keys=[scene_key])
        await _reset_shared_orm_engine()


@pytest.mark.skipif(not POSTGRES_URL, reason="未配置真实 PostgreSQL 测试连接")
@pytest.mark.asyncio
async def test_delete_scene_requires_not_reserved() -> None:
    if not _same_database(POSTGRES_URL, _configured_database_url()):
        pytest.skip("KOMARI_TEST_POSTGRES_URL 与 nonebot sqlalchemy_database_url 不一致")
    await _reset_shared_orm_engine()
    repo = _repository()
    try:
        for reserved in ("NOISE", "MEANINGFUL", "CALL_DIRECT", "CALL_MENTION"):
            with pytest.raises(ValueError, match="必需 fixed scene 不允许删除"):
                await repo.delete_scene(reserved)
    finally:
        await _reset_shared_orm_engine()


@pytest.mark.skipif(not POSTGRES_URL, reason="未配置真实 PostgreSQL 测试连接")
@pytest.mark.asyncio
async def test_delete_scene_unreferenced_removes_row() -> None:
    if not _same_database(POSTGRES_URL, _configured_database_url()):
        pytest.skip("KOMARI_TEST_POSTGRES_URL 与 nonebot sqlalchemy_database_url 不一致")
    await _reset_shared_orm_engine()
    repo = _repository()
    scene_key = _make_scene_key()
    try:
        await _create_scene(repo, scene_key)
        assert await repo.has_any_scene() is True
        assert await repo.delete_scene(scene_key) is True
        assert await repo.get_scene_by_key(scene_key) is None
        assert await repo.delete_scene(scene_key) is False
    finally:
        await _cleanup(scene_keys=[scene_key])
        await _reset_shared_orm_engine()


@pytest.mark.skipif(not POSTGRES_URL, reason="未配置真实 PostgreSQL 测试连接")
@pytest.mark.asyncio
async def test_delete_scene_referenced_disables_instead_of_deleting() -> None:
    if not _same_database(POSTGRES_URL, _configured_database_url()):
        pytest.skip("KOMARI_TEST_POSTGRES_URL 与 nonebot sqlalchemy_database_url 不一致")
    await _reset_shared_orm_engine()
    repo = _repository()
    tag = uuid4().hex
    scene_key = _make_scene_key()
    scene_set_id: int | None = None
    try:
        scene = await _create_scene(repo, scene_key, content_text="历史版本文本", order_index=3)
        scene_set, _created = await repo.get_or_create_scene_set(
            source_path=f"postgresql:integration:{tag}",
            source_hash=_make_fingerprint(),
            embedding_model="integration-model",
            embedding_instruction_hash=f"inst-{tag}",
        )
        scene_set_id = int(scene_set["id"])
        await repo.insert_scene_items(
            scene_set_id,
            [
                {
                    "scene_id": scene["id"],
                    "scene_key": scene["scene_key"],
                    "scene_type": scene["scene_type"],
                    "content_text": scene["content_text"],
                    "enabled": scene["enabled"],
                    "order_index": scene["order_index"],
                    "content_hash": scene["content_hash"],
                    "status": "PENDING",
                }
            ],
        )

        assert await repo.delete_scene(scene_key) is True
        current = await repo.get_scene_by_key(scene_key)
        assert current is not None
        assert current["enabled"] is False
        items = await repo.list_items_by_set(scene_set_id)
        assert items[0]["content_text"] == "历史版本文本"
    finally:
        await _cleanup(
            set_ids=[scene_set_id] if scene_set_id is not None else None,
            scene_keys=[scene_key],
        )
        await _reset_shared_orm_engine()


@pytest.mark.skipif(not POSTGRES_URL, reason="未配置真实 PostgreSQL 测试连接")
@pytest.mark.asyncio
async def test_get_or_create_scene_set_reuses_unique_fingerprint() -> None:
    if not _same_database(POSTGRES_URL, _configured_database_url()):
        pytest.skip("KOMARI_TEST_POSTGRES_URL 与 nonebot sqlalchemy_database_url 不一致")
    await _reset_shared_orm_engine()
    repo = _repository()
    source_hash = _make_fingerprint()
    set_ids: list[int] = []
    try:
        first, created = await repo.get_or_create_scene_set(
            source_path="postgresql:integration",
            source_hash=source_hash,
            embedding_model="model-x",
            embedding_instruction_hash="instruction-hash",
        )
        assert created is True
        set_ids.append(int(first["id"]))

        reused, created_again = await repo.get_or_create_scene_set(
            source_path="postgresql:integration",
            source_hash=source_hash,
            embedding_model="model-x",
            embedding_instruction_hash="instruction-hash",
        )
        assert created_again is False
        assert int(reused["id"]) == int(first["id"])
        assert reused["status"] == "BUILDING"

        different, created_third = await repo.get_or_create_scene_set(
            source_path="postgresql:integration",
            source_hash=_make_fingerprint(),
            embedding_model="model-x",
            embedding_instruction_hash="instruction-hash",
        )
        assert created_third is True
        set_ids.append(int(different["id"]))
        assert int(different["id"]) != int(first["id"])
    finally:
        await _cleanup(set_ids=set_ids)
        await _reset_shared_orm_engine()


@pytest.mark.skipif(not POSTGRES_URL, reason="未配置真实 PostgreSQL 测试连接")
@pytest.mark.asyncio
async def test_insert_scene_items_skips_concurrent_duplicate_rows() -> None:
    if not _same_database(POSTGRES_URL, _configured_database_url()):
        pytest.skip("KOMARI_TEST_POSTGRES_URL 与 nonebot sqlalchemy_database_url 不一致")
    await _reset_shared_orm_engine()
    repo = _repository()
    scene = await _create_scene(repo, _make_scene_key())
    scene_set, _created = await repo.get_or_create_scene_set(
        source_path="postgresql:integration",
        source_hash=_make_fingerprint(),
        embedding_model="model-x",
        embedding_instruction_hash="inst-dup",
    )
    set_id = int(scene_set["id"])
    item = {
        "scene_id": scene["id"],
        "scene_key": scene["scene_key"],
        "scene_type": scene["scene_type"],
        "content_text": "重复写入条目",
        "enabled": True,
        "order_index": 1,
        "content_hash": "content-hash",
        "embedding": None,
        "embedding_dim": None,
        "status": "PENDING",
        "error_message": None,
        "embedded_at": None,
    }
    try:
        assert await repo.insert_scene_items(set_id, [item, item]) == 1
        assert len(await repo.list_items_by_set(set_id)) == 1
    finally:
        await _cleanup(set_ids=[set_id], scene_keys=[scene["scene_key"]])
        await _reset_shared_orm_engine()


@pytest.mark.skipif(not POSTGRES_URL, reason="未配置真实 PostgreSQL 测试连接")
@pytest.mark.asyncio
async def test_insert_scene_items_resolves_scene_by_key() -> None:
    if not _same_database(POSTGRES_URL, _configured_database_url()):
        pytest.skip("KOMARI_TEST_POSTGRES_URL 与 nonebot sqlalchemy_database_url 不一致")
    await _reset_shared_orm_engine()
    repo = _repository()
    scene = await _create_scene(repo, _make_scene_key())
    scene_set, _created = await repo.get_or_create_scene_set(
        source_path="postgresql:integration",
        source_hash=_make_fingerprint(),
        embedding_model="model-x",
        embedding_instruction_hash="inst-key",
    )
    set_id = int(scene_set["id"])
    try:
        inserted = await repo.insert_scene_items(
            set_id,
            [
                {
                    "scene_key": scene["scene_key"],
                    "scene_type": "general",
                    "content_text": "按 key 解析",
                    "enabled": True,
                    "order_index": 0,
                    "content_hash": "hash-key",
                    "status": "PENDING",
                }
            ],
        )
        assert inserted == 1
        with pytest.raises(ValueError, match="scene 内容记录不存在"):
            await repo.insert_scene_items(
                set_id,
                [
                    {
                        "scene_key": "SCENE_MISSING",
                        "scene_type": "general",
                        "content_text": "缺失",
                        "enabled": True,
                        "order_index": 0,
                        "content_hash": "hash-missing",
                        "status": "PENDING",
                    }
                ],
            )
    finally:
        await _cleanup(set_ids=[set_id], scene_keys=[scene["scene_key"]])
        await _reset_shared_orm_engine()


@pytest.mark.skipif(not POSTGRES_URL, reason="未配置真实 PostgreSQL 测试连接")
@pytest.mark.asyncio
async def test_list_items_by_set_filters_and_orders() -> None:
    if not _same_database(POSTGRES_URL, _configured_database_url()):
        pytest.skip("KOMARI_TEST_POSTGRES_URL 与 nonebot sqlalchemy_database_url 不一致")
    await _reset_shared_orm_engine()
    repo = _repository()
    scenes = [
        await _create_scene(repo, _make_scene_key(), order_index=index)
        for index in range(3)
    ]
    scene_set, _created = await repo.get_or_create_scene_set(
        source_path="postgresql:integration",
        source_hash=_make_fingerprint(),
        embedding_model="model-x",
        embedding_instruction_hash="inst-list",
    )
    set_id = int(scene_set["id"])
    try:
        await repo.insert_scene_items(
            set_id,
            [
                {
                    "scene_id": scenes[0]["id"],
                    "scene_key": "KEY_DISABLED",
                    "scene_type": "general",
                    "content_text": "停用条目",
                    "enabled": False,
                    "order_index": 0,
                    "content_hash": "hash-0",
                    "status": "READY",
                },
                {
                    "scene_id": scenes[1]["id"],
                    "scene_key": "KEY_PENDING",
                    "scene_type": "general",
                    "content_text": "待处理条目",
                    "enabled": True,
                    "order_index": 2,
                    "content_hash": "hash-2",
                    "status": "PENDING",
                },
                {
                    "scene_id": scenes[2]["id"],
                    "scene_key": "KEY_READY",
                    "scene_type": "general",
                    "content_text": "就绪条目",
                    "enabled": True,
                    "order_index": 1,
                    "content_hash": "hash-1",
                    "status": "READY",
                },
            ],
        )
        all_items = await repo.list_items_by_set(set_id)
        assert [row["scene_key"] for row in all_items] == [
            "KEY_DISABLED",
            "KEY_READY",
            "KEY_PENDING",
        ]
        assert all_items[0]["enabled"] is False
        assert all_items[0]["status"] == "READY"

        ready_only = await repo.list_items_by_set(set_id, status="READY")
        assert [row["scene_key"] for row in ready_only] == [
            "KEY_DISABLED",
            "KEY_READY",
        ]

        enabled_only = await repo.list_items_by_set(set_id, enabled_only=True)
        assert [row["scene_key"] for row in enabled_only] == [
            "KEY_READY",
            "KEY_PENDING",
        ]

        limited = await repo.list_items_by_set(set_id, limit=2)
        assert len(limited) == 2
    finally:
        await _cleanup(
            set_ids=[set_id],
            scene_keys=[s["scene_key"] for s in scenes],
        )
        await _reset_shared_orm_engine()


@pytest.mark.skipif(not POSTGRES_URL, reason="未配置真实 PostgreSQL 测试连接")
@pytest.mark.asyncio
async def test_claim_pending_items_only_claims_building_pending() -> None:
    if not _same_database(POSTGRES_URL, _configured_database_url()):
        pytest.skip("KOMARI_TEST_POSTGRES_URL 与 nonebot sqlalchemy_database_url 不一致")
    await _reset_shared_orm_engine()
    repo = _repository()
    tag = uuid4().hex
    scenes, scene_set = await _create_building_set(repo, tag=tag, item_count=2)
    set_id = int(scene_set["id"])
    try:
        items = await repo.list_items_by_set(set_id)
        first_id, second_id = int(items[0]["id"]), int(items[1]["id"])
        await _set_item_state(
            second_id,
            status="PROCESSING",
            attempt_count=1,
            lease_owner="other-worker",
            lease_expires_at=_now() + timedelta(seconds=60),
        )

        claimed = await repo.claim_pending_items(
            set_id,
            owner_token="owner-1",
            limit=32,
            lease_seconds=120,
            max_attempts=3,
            retry_base_seconds=30,
        )

        assert [int(row["id"]) for row in claimed] == [first_id]
        assert claimed[0]["lease_owner"] == "owner-1"
        assert int(claimed[0]["attempt_count"]) == 1
        assert claimed[0]["status"] == "PROCESSING"
        assert claimed[0]["next_retry_at"] is None
        assert claimed[0]["lease_expires_at"] > _now()

        still_processing = await repo.list_items_by_set(set_id, status="PROCESSING")
        assert {int(row["id"]) for row in still_processing} == {
            first_id,
            second_id,
        }
    finally:
        await _cleanup(set_ids=[set_id], scene_keys=[s["scene_key"] for s in scenes])
        await _reset_shared_orm_engine()


@pytest.mark.skipif(not POSTGRES_URL, reason="未配置真实 PostgreSQL 测试连接")
@pytest.mark.asyncio
async def test_claim_pending_items_concurrent_workers_get_disjoint_items() -> None:
    if not _same_database(POSTGRES_URL, _configured_database_url()):
        pytest.skip("KOMARI_TEST_POSTGRES_URL 与 nonebot sqlalchemy_database_url 不一致")
    await _reset_shared_orm_engine()
    repo = _repository()
    tag = uuid4().hex
    scenes, scene_set = await _create_building_set(repo, tag=tag, item_count=4)
    set_id = int(scene_set["id"])
    try:
        item_ids = {int(row["id"]) for row in await repo.list_items_by_set(set_id)}
        assert len(item_ids) == 4

        async def _worker(token: str) -> list[dict[str, Any]]:
            return await repo.claim_pending_items(
                set_id,
                owner_token=token,
                limit=32,
                lease_seconds=120,
                max_attempts=3,
                retry_base_seconds=30,
            )

        import asyncio

        first, second = await asyncio.gather(_worker("worker-a"), _worker("worker-b"))

        first_ids = {int(row["id"]) for row in first}
        second_ids = {int(row["id"]) for row in second}
        assert first_ids | second_ids == item_ids
        assert first_ids.isdisjoint(second_ids)
        claimed = await repo.list_items_by_set(set_id, status="PROCESSING")
        assert len(claimed) == 4
        owners = {row["lease_owner"] for row in claimed}
        assert owners <= {"worker-a", "worker-b"}
        assert len(owners) >= 1
        assert all(int(row["attempt_count"]) == 1 for row in claimed)
    finally:
        await _cleanup(set_ids=[set_id], scene_keys=[s["scene_key"] for s in scenes])
        await _reset_shared_orm_engine()


@pytest.mark.skipif(not POSTGRES_URL, reason="未配置真实 PostgreSQL 测试连接")
@pytest.mark.asyncio
async def test_claim_reclaims_expired_lease_with_backoff() -> None:
    if not _same_database(POSTGRES_URL, _configured_database_url()):
        pytest.skip("KOMARI_TEST_POSTGRES_URL 与 nonebot sqlalchemy_database_url 不一致")
    await _reset_shared_orm_engine()
    repo = _repository()
    tag = uuid4().hex
    scenes, scene_set = await _create_building_set(repo, tag=tag, item_count=1)
    set_id = int(scene_set["id"])
    item_id = int((await repo.list_items_by_set(set_id))[0]["id"])
    try:
        await _set_item_state(
            item_id,
            status="PROCESSING",
            attempt_count=1,
            lease_owner="stale-worker",
            lease_expires_at=_now() - timedelta(seconds=1),
        )

        claimed = await repo.claim_pending_items(
            set_id,
            owner_token="owner-2",
            limit=32,
            lease_seconds=120,
            max_attempts=3,
            retry_base_seconds=30,
        )

        assert claimed == []
        reclaimed = await repo.list_items_by_set(set_id)
        assert reclaimed[0]["status"] == "PENDING"
        assert reclaimed[0]["lease_owner"] is None
        assert int(reclaimed[0]["attempt_count"]) == 1
        assert reclaimed[0]["last_error_code"] == "lease_expired"
        assert reclaimed[0]["next_retry_at"] is not None
        assert reclaimed[0]["next_retry_at"] > _now()
    finally:
        await _cleanup(set_ids=[set_id], scene_keys=[s["scene_key"] for s in scenes])
        await _reset_shared_orm_engine()


@pytest.mark.skipif(not POSTGRES_URL, reason="未配置真实 PostgreSQL 测试连接")
@pytest.mark.asyncio
async def test_claim_fails_lease_when_attempts_exhausted() -> None:
    if not _same_database(POSTGRES_URL, _configured_database_url()):
        pytest.skip("KOMARI_TEST_POSTGRES_URL 与 nonebot sqlalchemy_database_url 不一致")
    await _reset_shared_orm_engine()
    repo = _repository()
    tag = uuid4().hex
    scenes, scene_set = await _create_building_set(repo, tag=tag, item_count=1)
    set_id = int(scene_set["id"])
    item_id = int((await repo.list_items_by_set(set_id))[0]["id"])
    try:
        await _set_item_state(
            item_id,
            status="PROCESSING",
            attempt_count=3,
            lease_owner="stale-worker",
            lease_expires_at=_now() - timedelta(seconds=1),
        )

        claimed = await repo.claim_pending_items(
            set_id,
            owner_token="owner-3",
            limit=32,
            lease_seconds=120,
            max_attempts=3,
            retry_base_seconds=30,
        )

        assert claimed == []
        failed = await repo.list_items_by_set(set_id, status="FAILED")
        assert [int(row["id"]) for row in failed] == [item_id]
        assert failed[0]["last_error_code"] == "lease_expired"
        assert failed[0]["error_message"] == "embedding 处理租约超过最大重试次数"
        assert failed[0]["next_retry_at"] is None
        assert failed[0]["lease_owner"] is None
    finally:
        await _cleanup(set_ids=[set_id], scene_keys=[s["scene_key"] for s in scenes])
        await _reset_shared_orm_engine()


@pytest.mark.skipif(not POSTGRES_URL, reason="未配置真实 PostgreSQL 测试连接")
@pytest.mark.asyncio
async def test_mark_item_ready_only_for_lease_owner() -> None:
    if not _same_database(POSTGRES_URL, _configured_database_url()):
        pytest.skip("KOMARI_TEST_POSTGRES_URL 与 nonebot sqlalchemy_database_url 不一致")
    await _reset_shared_orm_engine()
    repo = _repository()
    tag = uuid4().hex
    scenes, scene_set = await _create_building_set(repo, tag=tag, item_count=1)
    set_id = int(scene_set["id"])
    item_id = int((await repo.list_items_by_set(set_id))[0]["id"])
    try:
        await repo.claim_pending_items(
            set_id,
            owner_token="owner-ready",
            limit=32,
            lease_seconds=120,
            max_attempts=3,
            retry_base_seconds=30,
        )

        stale = await repo.mark_item_ready(
            item_id,
            "wrong-owner",
            [0.1, 0.2],
            2,
        )
        assert stale is False

        accepted = await repo.mark_item_ready(
            item_id,
            "owner-ready",
            [0.1, 0.2],
            2,
        )
        assert accepted is True
        ready = await repo.list_items_by_set(set_id, status="READY")
        assert ready[0]["embedding"] == pytest.approx([0.1, 0.2])
        assert ready[0]["embedding_dim"] == 2
        assert ready[0]["lease_owner"] is None
        assert ready[0]["embedded_at"] is not None
    finally:
        await _cleanup(set_ids=[set_id], scene_keys=[s["scene_key"] for s in scenes])
        await _reset_shared_orm_engine()


@pytest.mark.skipif(not POSTGRES_URL, reason="未配置真实 PostgreSQL 测试连接")
@pytest.mark.asyncio
async def test_complete_item_failure_schedules_retry_or_fails() -> None:
    if not _same_database(POSTGRES_URL, _configured_database_url()):
        pytest.skip("KOMARI_TEST_POSTGRES_URL 与 nonebot sqlalchemy_database_url 不一致")
    await _reset_shared_orm_engine()
    repo = _repository()
    tag = uuid4().hex
    scenes, scene_set = await _create_building_set(repo, tag=tag, item_count=2)
    set_id = int(scene_set["id"])
    items = await repo.list_items_by_set(set_id)
    retry_id, fail_id = int(items[0]["id"]), int(items[1]["id"])
    try:
        await repo.claim_pending_items(
            set_id,
            owner_token="owner-fail",
            limit=32,
            lease_seconds=120,
            max_attempts=3,
            retry_base_seconds=30,
        )

        stale = await repo.complete_item_failure(
            retry_id,
            owner_token="wrong-owner",
            error_code="embedding_timeout",
            error_message="embedding 请求超时",
            max_attempts=3,
            retry_base_seconds=30,
        )
        assert stale == "stale"

        outcome = await repo.complete_item_failure(
            retry_id,
            owner_token="owner-fail",
            error_code="embedding_timeout",
            error_message="embedding 请求超时",
            max_attempts=3,
            retry_base_seconds=30,
        )
        assert outcome == "pending"
        rescheduled = await repo.list_items_by_set(set_id)
        assert rescheduled[0]["status"] == "PENDING"
        assert rescheduled[0]["last_error_code"] == "embedding_timeout"
        assert rescheduled[0]["error_message"] == "embedding 请求超时"
        assert rescheduled[0]["lease_owner"] is None
        assert rescheduled[0]["next_retry_at"] is not None
        assert rescheduled[0]["next_retry_at"] > _now()

        await _set_item_state(
            fail_id,
            status="PROCESSING",
            attempt_count=3,
            lease_owner="owner-fail",
            lease_expires_at=_now() + timedelta(seconds=120),
        )
        final = await repo.complete_item_failure(
            fail_id,
            owner_token="owner-fail",
            error_code="embedding_timeout",
            error_message="embedding 请求超时",
            max_attempts=3,
            retry_base_seconds=30,
        )
        assert final == "failed"
        failed = await repo.list_items_by_set(set_id, status="FAILED")
        assert [int(row["id"]) for row in failed] == [fail_id]
        assert failed[0]["next_retry_at"] is None
    finally:
        await _cleanup(
            set_ids=[set_id],
            scene_keys=[s["scene_key"] for s in scenes],
        )
        await _reset_shared_orm_engine()


@pytest.mark.skipif(not POSTGRES_URL, reason="未配置真实 PostgreSQL 测试连接")
@pytest.mark.asyncio
async def test_refresh_set_progress_transitions_to_ready_and_failed() -> None:
    if not _same_database(POSTGRES_URL, _configured_database_url()):
        pytest.skip("KOMARI_TEST_POSTGRES_URL 与 nonebot sqlalchemy_database_url 不一致")
    await _reset_shared_orm_engine()
    repo = _repository()
    tag = uuid4().hex
    scenes, scene_set = await _create_building_set(repo, tag=tag, item_count=2)
    set_id = int(scene_set["id"])
    try:
        progress = await repo.refresh_set_progress(set_id)
        assert progress["status"] == "BUILDING"
        assert progress["previous_status"] == "BUILDING"
        assert int(progress["item_total"]) == 2
        assert int(progress["item_ready"]) == 0

        items = await repo.list_items_by_set(set_id)
        await _set_item_state(int(items[0]["id"]), status="READY")
        await _set_item_state(int(items[1]["id"]), status="READY")
        ready = await repo.refresh_set_progress(set_id)
        assert ready["status"] == "READY"
        assert ready["previous_status"] == "BUILDING"
        assert int(ready["item_ready"]) == 2
        assert ready["error_message"] is None
        assert ready["ready_at"] is not None

        ready_again = await repo.refresh_set_progress(set_id)
        assert ready_again["status"] == "READY"
        assert ready_again["previous_status"] == "READY"
    finally:
        await _cleanup(set_ids=[set_id], scene_keys=[s["scene_key"] for s in scenes])
        await _reset_shared_orm_engine()


@pytest.mark.skipif(not POSTGRES_URL, reason="未配置真实 PostgreSQL 测试连接")
@pytest.mark.asyncio
async def test_refresh_set_progress_failed_with_failed_item() -> None:
    if not _same_database(POSTGRES_URL, _configured_database_url()):
        pytest.skip("KOMARI_TEST_POSTGRES_URL 与 nonebot sqlalchemy_database_url 不一致")
    await _reset_shared_orm_engine()
    repo = _repository()
    tag = uuid4().hex
    scenes, scene_set = await _create_building_set(repo, tag=tag, item_count=2)
    set_id = int(scene_set["id"])
    try:
        items = await repo.list_items_by_set(set_id)
        await _set_item_state(int(items[0]["id"]), status="READY")
        await _set_item_state(int(items[1]["id"]), status="FAILED")
        failed = await repo.refresh_set_progress(set_id)
        assert failed["status"] == "FAILED"
        assert failed["previous_status"] == "BUILDING"
        assert int(failed["item_failed"]) == 1
        assert failed["error_message"] == "scene embedding 存在最终失败条目"
        assert failed["ready_at"] is None
    finally:
        await _cleanup(set_ids=[set_id], scene_keys=[s["scene_key"] for s in scenes])
        await _reset_shared_orm_engine()


@pytest.mark.skipif(not POSTGRES_URL, reason="未配置真实 PostgreSQL 测试连接")
@pytest.mark.asyncio
async def test_refresh_set_progress_missing_set_raises_value_error() -> None:
    if not _same_database(POSTGRES_URL, _configured_database_url()):
        pytest.skip("KOMARI_TEST_POSTGRES_URL 与 nonebot sqlalchemy_database_url 不一致")
    await _reset_shared_orm_engine()
    repo = _repository()
    try:
        with pytest.raises(ValueError, match="scene set 不存在"):
            await repo.refresh_set_progress(999_999_999)
    finally:
        await _reset_shared_orm_engine()


@pytest.mark.skipif(not POSTGRES_URL, reason="未配置真实 PostgreSQL 测试连接")
@pytest.mark.asyncio
async def test_get_active_set_returns_none_without_active() -> None:
    if not _same_database(POSTGRES_URL, _configured_database_url()):
        pytest.skip("KOMARI_TEST_POSTGRES_URL 与 nonebot sqlalchemy_database_url 不一致")
    await _reset_shared_orm_engine()
    repo = _repository()
    try:
        assert await repo.get_active_set() is None
    finally:
        await _cleanup()
        await _reset_shared_orm_engine()


@pytest.mark.skipif(not POSTGRES_URL, reason="未配置真实 PostgreSQL 测试连接")
@pytest.mark.asyncio
async def test_switch_active_set_requires_ready_set() -> None:
    if not _same_database(POSTGRES_URL, _configured_database_url()):
        pytest.skip("KOMARI_TEST_POSTGRES_URL 与 nonebot sqlalchemy_database_url 不一致")
    await _reset_shared_orm_engine()
    repo = _repository()
    tag = uuid4().hex
    scenes, scene_set = await _create_building_set(repo, tag=tag, item_count=1)
    set_id = int(scene_set["id"])
    try:
        with pytest.raises(ValueError, match="scene set 不存在"):
            await repo.switch_active_set(999_999_999)
        with pytest.raises(ValueError, match="非 READY 状态"):
            await repo.switch_active_set(set_id)
        assert await repo.get_active_set() is None
    finally:
        await _cleanup(set_ids=[set_id], scene_keys=[s["scene_key"] for s in scenes])
        await _reset_shared_orm_engine()


@pytest.mark.skipif(not POSTGRES_URL, reason="未配置真实 PostgreSQL 测试连接")
@pytest.mark.asyncio
async def test_switch_active_set_activates_and_get_active_returns_set() -> None:
    if not _same_database(POSTGRES_URL, _configured_database_url()):
        pytest.skip("KOMARI_TEST_POSTGRES_URL 与 nonebot sqlalchemy_database_url 不一致")
    await _reset_shared_orm_engine()
    repo = _repository()
    tag = uuid4().hex
    scenes, scene_set = await _create_building_set(repo, tag=tag, item_count=1)
    set_id = int(scene_set["id"])
    try:
        items = await repo.list_items_by_set(set_id)
        await _set_item_state(int(items[0]["id"]), status="READY")
        await repo.refresh_set_progress(set_id)

        await repo.switch_active_set(set_id)
        active = await repo.get_active_set()
        assert active is not None
        assert int(active["id"]) == set_id
        assert active["status"] == "READY"
        assert active["runtime_updated_at"] is not None

        await repo.switch_active_set(set_id)
        active_again = await repo.get_active_set()
        assert active_again is not None
        assert int(active_again["id"]) == set_id
    finally:
        await _cleanup(set_ids=[set_id], scene_keys=[s["scene_key"] for s in scenes])
        await _reset_shared_orm_engine()


@pytest.mark.skipif(not POSTGRES_URL, reason="未配置真实 PostgreSQL 测试连接")
@pytest.mark.asyncio
async def test_find_reusable_ready_item_matches_fingerprint() -> None:
    if not _same_database(POSTGRES_URL, _configured_database_url()):
        pytest.skip("KOMARI_TEST_POSTGRES_URL 与 nonebot sqlalchemy_database_url 不一致")
    await _reset_shared_orm_engine()
    repo = _repository()
    tag = uuid4().hex
    scenes, scene_set = await _create_building_set(repo, tag=tag, item_count=1)
    set_id = int(scene_set["id"])
    try:
        items = await repo.list_items_by_set(set_id)
        item_id = int(items[0]["id"])
        content_hash = items[0]["content_hash"]
        await _set_item_state(
            item_id,
            status="READY",
            embedding=[0.5, 0.5],
            embedding_dim=2,
        )
        await repo.refresh_set_progress(set_id)

        reusable = await repo.find_reusable_ready_item(
            scene_id=int(scenes[0]["id"]),
            content_hash=content_hash,
            embedding_model="integration-model",
            embedding_instruction_hash=f"inst-{tag}",
        )
        assert reusable is not None
        assert int(reusable["id"]) == item_id
        assert reusable["embedding"] == pytest.approx([0.5, 0.5])

        assert await repo.find_reusable_ready_item(
            scene_id=int(scenes[0]["id"]),
            content_hash="hash-different",
            embedding_model="integration-model",
            embedding_instruction_hash=f"inst-{tag}",
        ) is None
        assert await repo.find_reusable_ready_item(
            scene_id=int(scenes[0]["id"]),
            content_hash=content_hash,
            embedding_model="other-model",
            embedding_instruction_hash=f"inst-{tag}",
        ) is None

        by_key = await repo.find_reusable_ready_item(
            scene_key=scenes[0]["scene_key"],
            content_hash=content_hash,
            embedding_model="integration-model",
            embedding_instruction_hash=f"inst-{tag}",
        )
        assert by_key is not None
        assert int(by_key["id"]) == item_id

        with pytest.raises(ValueError, match="scene_id 或 scene_key 必须提供一个"):
            await repo.find_reusable_ready_item(
                content_hash=content_hash,
                embedding_model="integration-model",
                embedding_instruction_hash=f"inst-{tag}",
            )
    finally:
        await _cleanup(set_ids=[set_id], scene_keys=[s["scene_key"] for s in scenes])
        await _reset_shared_orm_engine()


@pytest.mark.skipif(not POSTGRES_URL, reason="未配置真实 PostgreSQL 测试连接")
@pytest.mark.asyncio
async def test_reopen_failed_set_resets_items() -> None:
    if not _same_database(POSTGRES_URL, _configured_database_url()):
        pytest.skip("KOMARI_TEST_POSTGRES_URL 与 nonebot sqlalchemy_database_url 不一致")
    await _reset_shared_orm_engine()
    repo = _repository()
    tag = uuid4().hex
    scenes, scene_set = await _create_building_set(repo, tag=tag, item_count=2)
    set_id = int(scene_set["id"])
    try:
        items = await repo.list_items_by_set(set_id)
        await _set_item_state(int(items[0]["id"]), status="FAILED")
        await _set_item_state(int(items[1]["id"]), status="FAILED")
        await repo.refresh_set_progress(set_id)
        failed_set = await repo.get_scene_set(set_id)
        assert failed_set is not None
        assert failed_set["status"] == "FAILED"

        with pytest.raises(ValueError, match="scene set 不存在"):
            await repo.reopen_failed_set(999_999_999)

        reset = await repo.reopen_failed_set(set_id)
        assert reset == 2
        reopened = await repo.get_scene_set(set_id)
        assert reopened is not None
        assert reopened["status"] == "BUILDING"
        assert reopened["error_message"] is None
        assert int(reopened["item_failed"]) == 0
        assert reopened["ready_at"] is None
        reopened_items = await repo.list_items_by_set(set_id)
        assert all(row["status"] == "PENDING" for row in reopened_items)
        assert all(int(row["attempt_count"]) == 0 for row in reopened_items)
        assert all(row["next_retry_at"] is not None for row in reopened_items)

        with pytest.raises(ValueError, match="仅允许重试 FAILED set"):
            await repo.reopen_failed_set(set_id)
    finally:
        await _cleanup(set_ids=[set_id], scene_keys=[s["scene_key"] for s in scenes])
        await _reset_shared_orm_engine()


@pytest.mark.skipif(not POSTGRES_URL, reason="未配置真实 PostgreSQL 测试连接")
@pytest.mark.asyncio
async def test_delete_set_cascades_items() -> None:
    if not _same_database(POSTGRES_URL, _configured_database_url()):
        pytest.skip("KOMARI_TEST_POSTGRES_URL 与 nonebot sqlalchemy_database_url 不一致")
    await _reset_shared_orm_engine()
    repo = _repository()
    tag = uuid4().hex
    scenes, scene_set = await _create_building_set(repo, tag=tag, item_count=2)
    set_id = int(scene_set["id"])
    try:
        assert await repo.delete_set(set_id) is True
        assert await repo.get_scene_set(set_id) is None
        assert await repo.list_items_by_set(set_id) == []
        assert await repo.delete_set(set_id) is False
    finally:
        await _cleanup(set_ids=[set_id], scene_keys=[s["scene_key"] for s in scenes])
        await _reset_shared_orm_engine()


@pytest.mark.skipif(not POSTGRES_URL, reason="未配置真实 PostgreSQL 测试连接")
@pytest.mark.asyncio
async def test_latest_ready_set_queries_order_by_recency() -> None:
    if not _same_database(POSTGRES_URL, _configured_database_url()):
        pytest.skip("KOMARI_TEST_POSTGRES_URL 与 nonebot sqlalchemy_database_url 不一致")
    await _reset_shared_orm_engine()
    repo = _repository()
    tag = uuid4().hex
    set_ids: list[int] = []
    scene_keys: list[str] = []
    try:
        for index in range(2):
            scene = await _create_scene(repo, _make_scene_key(), order_index=index)
            scene_keys.append(scene["scene_key"])
            scene_set, _created = await repo.get_or_create_scene_set(
                source_path=f"postgresql:integration:{tag}:{index}",
                source_hash=_make_fingerprint(),
                embedding_model="model-x",
                embedding_instruction_hash="inst-latest",
            )
            set_id = int(scene_set["id"])
            set_ids.append(set_id)
            await repo.insert_scene_items(
                set_id,
                [
                    {
                        "scene_id": scene["id"],
                        "scene_key": scene["scene_key"],
                        "scene_type": "general",
                        "content_text": f"版本 {index}",
                        "enabled": True,
                        "order_index": 0,
                        "content_hash": f"hash-{tag}-{index}",
                        "status": "READY",
                    }
                ],
            )
            await repo.refresh_set_progress(set_id)

        latest = await repo.get_latest_ready_set()
        assert latest is not None
        assert int(latest["id"]) == set_ids[-1]

        ordered = await repo.list_ready_sets()
        assert [int(row["id"]) for row in ordered] == list(reversed(set_ids))
        assert len(await repo.list_ready_sets(limit=1)) == 1

        first_set = await repo.get_scene_set(set_ids[0])
        assert first_set is not None
        by_fingerprint = await repo.get_latest_set_by_fingerprint(
            first_set["source_hash"],
            "model-x",
            "inst-latest",
            status="READY",
        )
        assert by_fingerprint is not None
        assert int(by_fingerprint["id"]) == set_ids[0]
        assert await repo.get_latest_set_by_fingerprint(
            "not-exists",
            "model-x",
            "inst-latest",
        ) is None
    finally:
        await _cleanup(set_ids=set_ids, scene_keys=scene_keys)
        await _reset_shared_orm_engine()


async def _set_item_state(
    item_id: int,
    *,
    status: str,
    attempt_count: int | None = None,
    lease_owner: str | None = None,
    lease_expires_at: datetime | None = None,
    next_retry_at: datetime | None = None,
    embedding: list[float] | None = None,
    embedding_dim: int | None = None,
) -> None:
    """直接以 ORM 模型调整条目租约状态（测试夹具）。"""
    values: dict[str, Any] = {"status": status}
    if attempt_count is not None:
        values["attempt_count"] = attempt_count
    if lease_owner is not None:
        values["lease_owner"] = lease_owner
    if lease_expires_at is not None:
        values["lease_expires_at"] = lease_expires_at
    if next_retry_at is not None:
        values["next_retry_at"] = next_retry_at
    if embedding is not None:
        values["embedding"] = embedding
        values["embedding_dim"] = embedding_dim
    session = _open_session()
    try:
        await session.execute(
            update(_ITEMS).where(_ITEMS.c.id == item_id).values(**values)
        )
        await session.commit()
    finally:
        await session.close()
