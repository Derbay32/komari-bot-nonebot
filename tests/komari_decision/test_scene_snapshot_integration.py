"""可选的 Scene 不可变快照 PostgreSQL 集成测试。"""

from __future__ import annotations

import os
from uuid import uuid4

import asyncpg
import pytest

from komari_bot.plugins.komari_decision.repositories.scene_repository import (
    SceneRepository,
)

POSTGRES_URL = os.getenv("KOMARI_TEST_POSTGRES_URL", "")


@pytest.mark.skipif(not POSTGRES_URL, reason="未配置真实 PostgreSQL 测试连接")
@pytest.mark.asyncio
async def test_scene_item_snapshot_survives_schema_upgrade_and_scene_edit() -> None:
    """历史 set 必须保留创建时文本，且被引用 scene 删除只能转为停用。"""
    schema_name = f"scene_snapshot_{uuid4().hex}"
    admin = await asyncpg.connect(POSTGRES_URL)
    pool: asyncpg.Pool | None = None
    try:
        await admin.execute(f"CREATE SCHEMA {schema_name}")
        pool = await asyncpg.create_pool(
            POSTGRES_URL,
            min_size=1,
            max_size=2,
            server_settings={"search_path": schema_name},
        )
        repository = SceneRepository(pool)
        await repository.ensure_schema()

        scene = await repository.upsert_scene(
            scene_key="INTEGRATION_GENERAL",
            scene_type="general",
            content_text="历史版本文本",
            enabled=True,
            order_index=3,
        )
        scene_set, created = await repository.get_or_create_scene_set(
            source_path="postgresql:integration",
            source_hash="source-v1",
            embedding_model="integration-model",
            embedding_instruction_hash="instruction-v1",
        )
        assert created is True
        inserted = await repository.insert_scene_items(
            int(scene_set["id"]),
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

        async with pool.acquire() as connection:
            await connection.execute(
                """
                ALTER TABLE komari_memory_scene_item
                    DROP COLUMN scene_key_snapshot,
                    DROP COLUMN scene_type_snapshot,
                    DROP COLUMN content_text_snapshot,
                    DROP COLUMN enabled_snapshot,
                    DROP COLUMN order_index_snapshot
                """
            )

        migrated_repository = SceneRepository(pool)
        await migrated_repository.ensure_schema()
        migrated_items = await migrated_repository.list_items_by_set(
            int(scene_set["id"])
        )
        assert migrated_items[0]["content_text"] == "历史版本文本"
        assert migrated_items[0]["enabled"] is True
        assert migrated_items[0]["order_index"] == 3

        await migrated_repository.upsert_scene(
            scene_key="INTEGRATION_GENERAL",
            scene_type="general",
            content_text="当前版本已修改",
            enabled=True,
            order_index=99,
        )
        immutable_items = await migrated_repository.list_items_by_set(
            int(scene_set["id"])
        )
        assert immutable_items[0]["content_text"] == "历史版本文本"
        assert immutable_items[0]["order_index"] == 3

        assert await migrated_repository.delete_scene("INTEGRATION_GENERAL") is True
        current_scene = await migrated_repository.get_scene_by_key(
            "INTEGRATION_GENERAL"
        )
        assert current_scene is not None
        assert current_scene["enabled"] is False
        after_delete = await migrated_repository.list_items_by_set(
            int(scene_set["id"])
        )
        assert after_delete[0]["content_text"] == "历史版本文本"
    finally:
        if pool is not None:
            await pool.close()
        await admin.execute(f"DROP SCHEMA IF EXISTS {schema_name} CASCADE")
        await admin.close()
