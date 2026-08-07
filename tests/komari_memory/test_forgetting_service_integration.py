"""ForgettingService 模糊化与每日任务账本的共享引擎连接集成测试。

取代 ``test_forgetting_service.py`` 中 asyncpg 池 fake 测试：模糊化写回
（CAS / 向量替换 / 失败删向量）、占位重试删除、跨群事件模糊化、以及每日
任务 stage 流水线（真实 ForgettingJobRepository）全部在真实库上断言。
LLM 输出通过模块级 ``llm_provider`` 替身注入（业务输入，非连接来源）。
"""

from __future__ import annotations

import asyncio
import os
from contextlib import suppress
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

import pytest

from komari_bot.plugins.komari_memory.core import retry as retry_module
from komari_bot.plugins.komari_memory.services import (
    forgetting_service as forgetting_service_module,
)
from komari_bot.plugins.komari_memory.services.forgetting_service import (
    ForgettingService,
)

POSTGRES_URL = os.getenv("KOMARI_TEST_POSTGRES_URL", "")

EMBEDDING_DIMENSION = 512
_STAGE_RUN_DATE = date(8888, 8, 8)


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


def _config(**overrides: object) -> object:
    defaults: dict[str, object] = {
        "forgetting_enabled": True,
        "forgetting_importance_threshold": 3,
        "forgetting_min_age_days": 0,
        "forgetting_decay_factor": 0.95,
        "forgetting_fuzzify_concurrency": 2,
        "forgetting_job_lease_seconds": 900,
        "response_tag": "content",
        "llm_model_summary": "summary-model",
        "llm_temperature_summary": 0.3,
        "llm_max_tokens_summary": 256,
        "llm_thinking_mode_summary": False,
        "llm_reasoning_effort_summary": "",
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


class _FakeEmbedding:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[str] = []

    async def embed(self, text: str) -> list[float]:
        if self.fail:
            raise RuntimeError("向量服务不可用")
        self.calls.append(text)
        import hashlib

        digest = hashlib.sha256(text.encode("utf-8")).digest()
        return [digest[index % len(digest)] / 255.0 for index in range(EMBEDDING_DIMENSION)]


class _FakeLLM:
    def __init__(self, content: str = "<content>模糊化后的结果</content>") -> None:
        self.content = content
        self.calls: list[dict[str, object]] = []

    async def generate_text(self, **kwargs: object) -> str:
        self.calls.append(dict(kwargs))
        return self.content


@pytest.fixture
async def service(
    monkeypatch: pytest.MonkeyPatch,
) -> "object":
    """真实共享引擎租约 + 可注入 LLM/嵌入替身的 ForgettingService。"""
    await _reset_shared_orm_engine()
    if not _same_database(POSTGRES_URL, _configured_database_url()):
        pytest.skip("KOMARI_TEST_POSTGRES_URL 与 nonebot sqlalchemy_database_url 不一致")
    from komari_bot.plugins.komari_memory.database.connection import create_pool

    pool = await create_pool()
    service = ForgettingService(  # type: ignore[arg-type]
        pool,  # type: ignore[arg-type]
        config_provider=lambda: _config(),  # type: ignore[arg-type]
        embedding_plugin=_FakeEmbedding(),
    )
    monkeypatch.setattr(forgetting_service_module, "llm_provider", _FakeLLM())
    yield service
    await _cleanup_test_rows()
    await _reset_shared_orm_engine()


async def _seed_conversation(
    service: ForgettingService,
    *,
    summary: str,
    importance_initial: int,
    old_days: int = 10,
) -> int:
    from komari_bot.plugins.komari_memory.repositories.conversation_repository import (
        ConversationRepository,
    )

    repo = ConversationRepository(service.pg_pool)  # type: ignore[arg-type]
    old = datetime.now(UTC) - timedelta(days=old_days)
    conv_id = await repo.insert_conversation(
        group_id=f"fz-{uuid4().hex}",
        summary=summary,
        embedding=_vector(0),
        participants=["u1"],
        importance_initial=importance_initial,
        dedup_key=f"fz-dedup-{uuid4().hex}",
        start_time=old.replace(tzinfo=None),
        end_time=old.replace(tzinfo=None),
    )
    assert conv_id is not None
    async with service.pg_pool.acquire() as conn:  # type: ignore[attr-defined]
        await conn.execute(
            "UPDATE komari_memory_conversations SET importance_current = 0, "
            "created_at = $1 WHERE id = $2",
            old.replace(tzinfo=None),
            conv_id,
        )
    return conv_id


async def _seed_interaction_event(
    service: ForgettingService,
    *,
    summary: str,
    importance_initial: int,
    old_days: int = 10,
) -> int:
    from komari_bot.plugins.komari_memory.repositories.interaction_event_repository import (
        InteractionEventRepository,
    )

    repo = InteractionEventRepository(service.pg_pool)  # type: ignore[arg-type]
    old = datetime.now(UTC) - timedelta(days=old_days)
    event_id = await repo.insert_interaction_event(
        user_id=f"fz-ev-{uuid4().hex}",
        display_name="待模糊用户",
        event_summary=summary,
        embedding=_vector(1),
        source_message_count=2,
        first_seen_at=old,
        last_seen_at=old,
        importance_initial=importance_initial,
        dedup_key=f"fz-ev-dedup-{uuid4().hex}",
    )
    async with service.pg_pool.acquire() as conn:  # type: ignore[attr-defined]
        await conn.execute(
            "UPDATE komari_memory_interaction_history SET importance_current = 0, "
            "created_at = $1 WHERE id = $2",
            old,
            event_id,
        )
    return event_id


async def _cleanup_test_rows() -> None:
    import asyncpg

    admin = await asyncpg.connect(_asyncpg_url())
    try:
        await admin.execute(
            "DELETE FROM komari_memory_conversation_embeddings "
            "WHERE conversation_id IN (SELECT id FROM komari_memory_conversations "
            "WHERE group_id LIKE 'fz-%')"
        )
        await admin.execute(
            "DELETE FROM komari_memory_conversations WHERE group_id LIKE 'fz-%'"
        )
        await admin.execute(
            "DELETE FROM komari_memory_interaction_embeddings "
            "WHERE interaction_id IN (SELECT id FROM komari_memory_interaction_history "
            "WHERE user_id LIKE 'fz-ev-%')"
        )
        await admin.execute(
            "DELETE FROM komari_memory_interaction_history WHERE user_id LIKE 'fz-ev-%'"
        )
        await admin.execute(
            "DELETE FROM komari_memory_jobs WHERE run_date = $1", _STAGE_RUN_DATE
        )
    finally:
        await admin.close()


# ---------- 模糊化写回 ----------


@pytest.mark.skipif(not POSTGRES_URL, reason="未配置真实 PostgreSQL 测试连接")
@pytest.mark.asyncio
async def test_fuzzify_conversation_writes_summary_and_replaces_vector(
    service: ForgettingService,
) -> None:
    conv_id = await _seed_conversation(
        service,
        summary="原始总结包含具体细节。",
        importance_initial=5,
    )
    ok = await service._fuzzify_conversation(conv_id, "原始总结包含具体细节。")
    assert ok is True

    async with service.pg_pool.acquire() as conn:  # type: ignore[attr-defined]
        row = await conn.fetchrow(
            "SELECT summary, is_fuzzy, importance_current FROM "
            "komari_memory_conversations WHERE id = $1",
            conv_id,
        )
        assert row is not None
        assert row["summary"] == "模糊化后的结果"
        assert row["is_fuzzy"] is True
        assert int(row["importance_current"]) == 5
        vector_row = await conn.fetchrow(
            "SELECT content_hash, embedding_dim FROM "
            "komari_memory_conversation_embeddings WHERE conversation_id = $1",
            conv_id,
        )
        assert vector_row is not None
        assert int(vector_row["embedding_dim"]) == EMBEDDING_DIMENSION

    # LLM 输出必须只保留 <content> 标签内正文
    llm: Any = forgetting_service_module.llm_provider
    assert "标签外不要输出任何解释" in str(llm.calls[0]["prompt"])
    assert 'source_type="memory"' in str(llm.calls[0]["prompt"])


@pytest.mark.skipif(not POSTGRES_URL, reason="未配置真实 PostgreSQL 测试连接")
@pytest.mark.asyncio
async def test_fuzzify_embedding_failure_deletes_old_vector(
    service: ForgettingService,
) -> None:
    service._embedding_plugin = _FakeEmbedding(fail=True)  # type: ignore[assignment]
    conv_id = await _seed_conversation(
        service,
        summary="包含可识别旧细节。",
        importance_initial=5,
    )
    ok = await service._fuzzify_conversation(conv_id, "包含可识别旧细节。")
    assert ok is True

    async with service.pg_pool.acquire() as conn:  # type: ignore[attr-defined]
        row = await conn.fetchrow(
            "SELECT summary, is_fuzzy FROM komari_memory_conversations WHERE id = $1",
            conv_id,
        )
        assert row is not None
        assert row["summary"] == "模糊化后的结果"
        assert row["is_fuzzy"] is True
        vector_row = await conn.fetchrow(
            "SELECT content_hash FROM komari_memory_conversation_embeddings "
            "WHERE conversation_id = $1",
            conv_id,
        )
        assert vector_row is None


@pytest.mark.skipif(not POSTGRES_URL, reason="未配置真实 PostgreSQL 测试连接")
@pytest.mark.asyncio
async def test_fuzzify_cas_does_not_overwrite_concurrently_touched_memory(
    service: ForgettingService,
) -> None:
    conv_id = await _seed_conversation(
        service,
        summary="被并发访问的原正文。",
        importance_initial=5,
    )
    async with service.pg_pool.acquire() as conn:  # type: ignore[attr-defined]
        await conn.execute(
            "UPDATE komari_memory_conversations SET summary = $1 WHERE id = $2",
            "并发更新后的正文。",
            conv_id,
        )

    ok = await service._fuzzify_conversation(conv_id, "被并发访问的原正文。")
    assert ok is False

    async with service.pg_pool.acquire() as conn:  # type: ignore[attr-defined]
        row = await conn.fetchrow(
            "SELECT summary, is_fuzzy FROM komari_memory_conversations WHERE id = $1",
            conv_id,
        )
        assert row is not None
        assert row["summary"] == "并发更新后的正文。"
        assert row["is_fuzzy"] is False


@pytest.mark.skipif(not POSTGRES_URL, reason="未配置真实 PostgreSQL 测试连接")
@pytest.mark.asyncio
async def test_fuzzify_interaction_event_writes_fuzzy_summary(
    service: ForgettingService,
) -> None:
    event_id = await _seed_interaction_event(
        service,
        summary="跨群聊了具体作品名。",
        importance_initial=5,
    )
    ok = await service._fuzzify_interaction_event(event_id, "跨群聊了具体作品名。")
    assert ok is True

    async with service.pg_pool.acquire() as conn:  # type: ignore[attr-defined]
        row = await conn.fetchrow(
            "SELECT event_summary, is_fuzzy, importance_current FROM "
            "komari_memory_interaction_history WHERE id = $1",
            event_id,
        )
        assert row is not None
        assert row["event_summary"] == "模糊化后的结果"
        assert row["is_fuzzy"] is True
        assert int(row["importance_current"]) == 5


@pytest.mark.skipif(not POSTGRES_URL, reason="未配置真实 PostgreSQL 测试连接")
@pytest.mark.asyncio
async def test_fuzzify_placeholder_retries_then_deletes(
    service: ForgettingService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _PlaceholderLLM:
        async def generate_text(self, **_kwargs: object) -> str:
            return "<content>对话内容已模糊化处理</content>"

    monkeypatch.setattr(forgetting_service_module, "llm_provider", _PlaceholderLLM())
    monkeypatch.setattr(retry_module.asyncio, "sleep", lambda _seconds: None)

    conv_id = await _seed_conversation(
        service,
        summary="占位重试后应删除。",
        importance_initial=5,
    )
    ok = await service._fuzzify_conversation(conv_id, "占位重试后应删除。")
    assert ok is True

    async with service.pg_pool.acquire() as conn:  # type: ignore[attr-defined]
        row = await conn.fetchrow(
            "SELECT id FROM komari_memory_conversations WHERE id = $1", conv_id
        )
        assert row is None


# ---------- 每日任务 stage 流水线（真实 ForgettingJobRepository） ----------


@pytest.mark.skipif(not POSTGRES_URL, reason="未配置真实 PostgreSQL 测试连接")
@pytest.mark.asyncio
async def test_daily_job_runs_stages_once_on_real_ledger(
    service: ForgettingService,
) -> None:
    low_value_id = await _seed_conversation(
        service,
        summary="低价值旧记忆。",
        importance_initial=2,
        old_days=10,
    )
    # 高价值归零记忆进入模糊化批次（默认 LLM 替身输出有效正文）
    high_value_id = await _seed_conversation(
        service,
        summary="高价值归零记忆。",
        importance_initial=5,
        old_days=10,
    )

    first = await service.decay_and_cleanup(run_date=_STAGE_RUN_DATE)
    second = await service.decay_and_cleanup(run_date=_STAGE_RUN_DATE)
    assert first is True
    assert second is False

    async with service.pg_pool.acquire() as conn:  # type: ignore[attr-defined]
        low_row = await conn.fetchrow(
            "SELECT id FROM komari_memory_conversations WHERE id = $1", low_value_id
        )
        assert low_row is None
        high_row = await conn.fetchrow(
            "SELECT summary, is_fuzzy, importance_current FROM "
            "komari_memory_conversations WHERE id = $1",
            high_value_id,
        )
        assert high_row is not None
        assert high_row["is_fuzzy"] is True
        assert high_row["summary"] == "模糊化后的结果"
        assert int(high_row["importance_current"]) == 5
        job = await conn.fetchrow(
            "SELECT stage FROM komari_memory_jobs "
            "WHERE job_name = 'daily_forgetting' AND run_date = $1",
            _STAGE_RUN_DATE,
        )
        assert job is not None
        assert job["stage"] == "completed"


@pytest.mark.skipif(not POSTGRES_URL, reason="未配置真实 PostgreSQL 测试连接")
@pytest.mark.asyncio
async def test_fuzzify_batch_limits_concurrency_on_real_rows(
    service: ForgettingService,
) -> None:
    for index in range(4):
        await _seed_conversation(
            service, summary=f"并发条目 {index}", importance_initial=5
        )
    current_in_flight = 0
    max_in_flight = 0

    async def _slow_fuzzify(_conv_id: int, original_summary: str) -> bool:
        del original_summary
        nonlocal current_in_flight, max_in_flight
        current_in_flight += 1
        max_in_flight = max(max_in_flight, current_in_flight)
        await asyncio.sleep(0.01)
        current_in_flight -= 1
        return True

    service._fuzzify_conversation = _slow_fuzzify  # type: ignore[method-assign]
    total = await service._fuzzify_and_cleanup_high_value_memories()
    assert total == 4
    assert max_in_flight <= 2
