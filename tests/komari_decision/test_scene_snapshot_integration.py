"""可选的 Scene 不可变快照 PostgreSQL 集成测试。

依赖已执行 alembic upgrade head 的迁移管理 schema；测试使用唯一 scene/set
标识并通过 ORM 会话清理行。仓储调用走公共接口，清理使用同一套 SQLModel
表模型（不保留 asyncpg 直连路径）。
"""

from __future__ import annotations

import os
from contextlib import suppress
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


@pytest.mark.skipif(not POSTGRES_URL, reason="未配置真实 PostgreSQL 测试连接")
@pytest.mark.asyncio
async def test_scene_item_snapshot_survives_scene_edit_and_delete() -> None:
    """历史 set 必须保留创建时文本，且被引用 scene 删除只能转为停用。"""
    if not _same_database(POSTGRES_URL, _configured_database_url()):
        pytest.skip(
            "KOMARI_TEST_POSTGRES_URL 与 nonebot sqlalchemy_database_url 不一致"
        )
    run_id = uuid4().hex
    scene_key = f"INTEGRATION_GENERAL_{run_id}"
    source_path = f"postgresql:integration:{run_id}"
    scene_set_id: int | None = None
    await _reset_shared_orm_engine()
    try:
        repository = _repository()

        scene = await repository.upsert_scene(
            scene_key=scene_key,
            scene_type="general",
            content_text="历史版本文本",
            enabled=True,
            order_index=3,
        )
        scene_set, created = await repository.get_or_create_scene_set(
            source_path=source_path,
            source_hash="source-v1",
            embedding_model="integration-model",
            embedding_instruction_hash="instruction-v1",
        )
        assert created is True
        scene_set_id = int(scene_set["id"])
        inserted = await repository.insert_scene_items(
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
        assert inserted == 1

        await repository.upsert_scene(
            scene_key=scene_key,
            scene_type="general",
            content_text="当前版本已修改",
            enabled=True,
            order_index=99,
        )
        immutable_items = await repository.list_items_by_set(scene_set_id)
        assert immutable_items[0]["content_text"] == "历史版本文本"
        assert immutable_items[0]["order_index"] == 3

        assert await repository.delete_scene(scene_key) is True
        current_scene = await repository.get_scene_by_key(scene_key)
        assert current_scene is not None
        assert current_scene["enabled"] is False
        after_delete = await repository.list_items_by_set(scene_set_id)
        assert after_delete[0]["content_text"] == "历史版本文本"
    finally:
        session = _open_session()
        try:
            await session.execute(
                update(_RUNTIME)
                .where(_RUNTIME.c.id == 1)
                .values(active_set_id=None)
            )
            if scene_set_id is not None:
                await session.execute(
                    delete(_ITEMS).where(_ITEMS.c.set_id == scene_set_id)
                )
                await session.execute(
                    delete(_SETS).where(_SETS.c.id == scene_set_id)
                )
            await session.execute(
                delete(_SCENES).where(_SCENES.c.scene_key == scene_key)
            )
            await session.commit()
        finally:
            await session.close()
        await _reset_shared_orm_engine()
