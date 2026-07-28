"""可选的 Prompt revision 与跨 worker 通知集成测试。"""

from __future__ import annotations

import asyncio
import os
import threading
from urllib.parse import unquote, urlparse
from uuid import uuid4

import pytest

from komari_bot.common import prompt_storage as storage_module
from komari_bot.common.database_config import DatabaseConfigSchema
from komari_bot.common.prompt_storage import PromptStorage

POSTGRES_URL = os.getenv("KOMARI_TEST_POSTGRES_URL", "")


def _database_config() -> DatabaseConfigSchema:
    parsed = urlparse(POSTGRES_URL)
    return DatabaseConfigSchema(
        pg_host=parsed.hostname or "127.0.0.1",
        pg_port=parsed.port or 5432,
        pg_database=parsed.path.lstrip("/") or "postgres",
        pg_user=unquote(parsed.username or "postgres"),
        pg_password=unquote(parsed.password or ""),
        pg_pool_min_size=1,
        pg_pool_max_size=1,
    )


@pytest.mark.skipif(not POSTGRES_URL, reason="未配置真实 PostgreSQL 测试连接")
@pytest.mark.asyncio
async def test_prompt_field_cas_and_notification_between_workers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _database_config()
    monkeypatch.setattr(storage_module, "get_shared_database_config", lambda: config)
    first = PromptStorage()
    second = PromptStorage()
    resource_id = f"integration-prompt-{uuid4().hex}"
    changed = threading.Event()
    second.register_invalidator(resource_id, changed.set)

    try:
        assert await first.fetch_async(resource_id) is None
        assert await second.fetch_async(resource_id) is None
        assert await second.wait_for_listener_ready() is True
        created = await first.replace_if_revision_async(
            resource_id=resource_id,
            display_name="集成测试 Prompt",
            prompt_data={"system_prompt": "初始", "memory_ack": "收到"},
            version="1.0",
            expected_revision=0,
        )
        assert created is not None
        assert created.revision == 1
        assert await asyncio.to_thread(changed.wait, 3) is True

        first_update = await first.update_field_if_revision_async(
            resource_id=resource_id,
            display_name="集成测试 Prompt",
            field_name="system_prompt",
            value="第一处更新",
            version="1.0",
            expected_revision=1,
        )
        stale_update = await second.update_field_if_revision_async(
            resource_id=resource_id,
            display_name="集成测试 Prompt",
            field_name="memory_ack",
            value="第二处更新",
            version="1.0",
            expected_revision=1,
        )

        assert first_update is not None
        assert first_update.revision == 2
        assert stale_update is None

        latest = await second.fetch_async(resource_id)
        assert latest is not None
        retried = await second.update_field_if_revision_async(
            resource_id=resource_id,
            display_name="集成测试 Prompt",
            field_name="memory_ack",
            value="第二处更新",
            version="1.0",
            expected_revision=latest.revision,
        )
        assert retried is not None
        assert retried.prompt_data == {
            "system_prompt": "第一处更新",
            "memory_ack": "第二处更新",
        }
        assert retried.revision == 3
    finally:
        await asyncio.to_thread(first.close)
        await asyncio.to_thread(second.close)
