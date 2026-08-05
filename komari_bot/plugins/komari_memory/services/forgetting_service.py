"""记忆忘却服务 - 定期清理和模糊化到期记忆。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from datetime import datetime
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from nonebot import logger
from nonebot.plugin import require

from komari_bot.common.content_budget import (
    ContentValidationError,
    TextBudget,
    truncate_text_to_budget,
    validate_text_budget,
)
from komari_bot.common.untrusted_context import (
    UntrustedContext,
    render_untrusted_context,
)

from ..core.retry import retry_async
from ..repositories.forgetting_job_repository import (
    ForgettingJobLeaseLostError,
    ForgettingJobRepository,
    ForgettingJobStage,
)
from .config_interface import get_config

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import date

    import asyncpg

    from komari_bot.plugins.agent_run_logger.diagnostic import AgentRunCollector

    from ..config_schema import KomariMemoryConfigSchema

llm_provider = require("llm_provider")
embedding_provider = require("embedding_provider")
agent_run_logger_plugin = require("agent_run_logger")

_CONVERSATION_DECAY_SQL = """
UPDATE komari_memory_conversations
SET importance_current = GREATEST(importance_current - 1, 0)
"""
_INTERACTION_DECAY_SQL = """
UPDATE komari_memory_interaction_history
SET importance_current = GREATEST(importance_current - 1, 0)
"""
_DELETE_LOW_CONVERSATIONS_SQL = """
DELETE FROM komari_memory_conversations
WHERE importance_current = 0
  AND importance_initial <= $1
  AND created_at <= NOW() - ($2 * INTERVAL '1 day')
"""
_DELETE_LOW_INTERACTIONS_SQL = """
DELETE FROM komari_memory_interaction_history
WHERE importance_current = 0
  AND importance_initial <= $1
  AND created_at <= NOW() - ($2 * INTERVAL '1 day')
"""
_FORGETTING_SOURCE_BUDGET = TextBudget(1_000, 3_000, 1_000)
_FORGETTING_RENDERED_CONTEXT_BUDGET = TextBudget(7_000, 21_000, 3_500)


def _extract_tag_content(text: str, tag: str) -> str:
    """提取指定标签内的正文，避免把额外输出写入数据库。"""
    pattern = rf"<{tag}>([\s\S]*?)</{tag}>"
    match = re.search(pattern, text)
    if match:
        return re.sub(r"\s+", " ", match.group(1)).strip()

    cleaned = re.sub(r"<think>[\s\S]*?</think>", "", text).strip()
    lines = [line.strip() for line in cleaned.splitlines() if line.strip()]
    if not lines:
        return ""

    if len(lines) > 1:
        logger.warning("[KomariMemory] 模糊化返回多行内容，降级使用首行")
    return re.sub(r"\s+", " ", lines[0]).strip()


def _is_invalid_fuzzy_summary(value: str) -> bool:
    """判断模糊化结果是否为空或已知占位文本。"""
    normalized = value.strip()
    return normalized in {
        "",
        "对话内容已模糊化处理",
        "互动事件已模糊化处理",
    }


def _build_content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _render_bounded_memory_context(*, content: str, source_id: str) -> str:
    bounded, truncated = truncate_text_to_budget(
        content,
        label="待模糊化记忆",
        budget=_FORGETTING_SOURCE_BUDGET,
    )
    payload = json.dumps(
        {
            "content": bounded,
            "original_characters": len(content),
            "truncated": truncated,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    rendered = render_untrusted_context(
        UntrustedContext(
            source_type="memory",
            source_id=source_id,
            content=payload,
            max_chars=_FORGETTING_RENDERED_CONTEXT_BUDGET.max_characters,
        )
    )
    try:
        return validate_text_budget(
            rendered,
            label="待模糊化记忆上下文",
            budget=_FORGETTING_RENDERED_CONTEXT_BUDGET,
        )
    except ContentValidationError as exc:
        message = "待模糊化记忆转义后超过上下文预算"
        raise ContentValidationError(message) from exc


def _safe_record_value(record: asyncpg.Record | dict[str, Any], key: str) -> Any:
    """安全读取 asyncpg.Record 或测试替身中的字段。"""
    try:
        return record[key]
    except (KeyError, TypeError):
        return None


def _safe_record_id_for_log(record: asyncpg.Record | dict[str, Any]) -> str:
    """生成用于日志的记录 ID，坏行也不影响批处理。"""
    value = _safe_record_value(record, "id")
    return "<missing>" if value is None else str(value)


def _parse_fuzzify_record(
    record: asyncpg.Record | dict[str, Any],
    summary_key: str,
    task_label: str,
) -> tuple[int, str] | None:
    """解析待模糊化记录，坏行返回 None 而不是抛出异常。"""
    raw_id = _safe_record_value(record, "id")
    raw_summary = _safe_record_value(record, summary_key)

    try:
        record_id = int(raw_id)
    except (TypeError, ValueError):
        logger.warning(
            "[KomariMemory] 跳过无效{}模糊化记录: id={}",
            task_label,
            raw_id,
        )
        return None

    if not isinstance(raw_summary, str):
        logger.warning(
            "[KomariMemory] 跳过无效{}模糊化记录: id={}, {} 不是字符串",
            task_label,
            record_id,
            summary_key,
        )
        return None

    return record_id, raw_summary


class FuzzifyBatchError(RuntimeError):
    """至少一条待模糊记录未完成，当前阶段不能标记成功。"""

    def __init__(self, *, task_label: str, failed_count: int) -> None:
        super().__init__(f"{task_label}模糊化存在 {failed_count} 条失败记录")


class ForgettingService:
    """记忆忘却服务。"""

    def __init__(
        self,
        pg_pool: asyncpg.Pool,
        *,
        config_provider: Callable[[], KomariMemoryConfigSchema] = get_config,
        job_repository: ForgettingJobRepository | None = None,
        embedding_plugin: Any = embedding_provider,
    ) -> None:
        """初始化忘却服务。

        Args:
            pg_pool: PostgreSQL连接池
            config_provider: 每次执行时读取当前配置的函数
        """
        self.pg_pool = pg_pool
        self._config_provider = config_provider
        self._job_repository = job_repository or ForgettingJobRepository(pg_pool)
        self._embedding_plugin = embedding_plugin

    @property
    def config(self) -> KomariMemoryConfigSchema:
        """获取当前动态配置，避免服务长期持有启动快照。"""
        return self._config_provider()

    async def decay_and_cleanup(self, *, run_date: date | None = None) -> bool:
        """执行死神脚本（每天凌晨4点）。

        处理流程：
        1. 检查是否启用忘却
        2. 所有记忆重要性按整数退一
        3. 删除低价值记忆
        4. 高价值记忆第一次归零时模糊化并恢复重要性，第二次归零删除
        """
        config = self.config
        if not config.forgetting_enabled:
            logger.debug("[KomariMemory] 忘却功能未启用，跳过")
            return False

        effective_run_date = run_date or datetime.now().astimezone().date()
        owner_token = f"forgetting-{uuid4().hex}"
        lease_seconds = int(config.forgetting_job_lease_seconds)
        claim = await self._job_repository.claim(
            run_date=effective_run_date,
            owner_token=owner_token,
            lease_seconds=lease_seconds,
        )
        if claim.status != "claimed":
            logger.info(
                "[KomariMemory] 每日忘却任务跳过: run_date={} status={} stage={}",
                effective_run_date,
                claim.status,
                claim.stage,
            )
            return False

        logger.info(
            "[KomariMemory] 死神脚本开始执行: run_date={} stage={}",
            effective_run_date,
            claim.stage,
        )
        stop_heartbeat = asyncio.Event()
        lease_lost = asyncio.Event()
        heartbeat_task = asyncio.create_task(
            self._renew_job_lease(
                run_date=effective_run_date,
                owner_token=owner_token,
                lease_seconds=lease_seconds,
                stop=stop_heartbeat,
                lease_lost=lease_lost,
            )
        )
        try:
            stage = claim.stage
            if stage == "claimed":
                await self._job_repository.run_transactional_stage(
                    run_date=effective_run_date,
                    owner_token=owner_token,
                    lease_seconds=lease_seconds,
                    expected_stage="claimed",
                    next_stage="conversation_decay_done",
                    actions=((_CONVERSATION_DECAY_SQL, ()),),
                )
                stage = "conversation_decay_done"
            self._ensure_job_lease(
                run_date=effective_run_date,
                stage=stage,
                lease_lost=lease_lost,
            )

            if stage == "conversation_decay_done":
                await self._job_repository.run_transactional_stage(
                    run_date=effective_run_date,
                    owner_token=owner_token,
                    lease_seconds=lease_seconds,
                    expected_stage="conversation_decay_done",
                    next_stage="interaction_decay_done",
                    actions=((_INTERACTION_DECAY_SQL, ()),),
                )
                stage = "interaction_decay_done"
            self._ensure_job_lease(
                run_date=effective_run_date,
                stage=stage,
                lease_lost=lease_lost,
            )

            if stage == "interaction_decay_done":
                threshold = config.forgetting_importance_threshold
                min_age_days = config.forgetting_min_age_days
                await self._job_repository.run_transactional_stage(
                    run_date=effective_run_date,
                    owner_token=owner_token,
                    lease_seconds=lease_seconds,
                    expected_stage="interaction_decay_done",
                    next_stage="low_value_cleanup_done",
                    actions=(
                        (
                            _DELETE_LOW_CONVERSATIONS_SQL,
                            (threshold, min_age_days),
                        ),
                        (
                            _DELETE_LOW_INTERACTIONS_SQL,
                            (threshold, min_age_days),
                        ),
                    ),
                )
                stage = "low_value_cleanup_done"
            self._ensure_job_lease(
                run_date=effective_run_date,
                stage=stage,
                lease_lost=lease_lost,
            )

            if stage == "low_value_cleanup_done":
                await self._fuzzify_and_cleanup_high_value_memories()
                await self._job_repository.advance_stage(
                    run_date=effective_run_date,
                    owner_token=owner_token,
                    lease_seconds=lease_seconds,
                    expected_stage="low_value_cleanup_done",
                    next_stage="conversation_fuzzify_done",
                )
                stage = "conversation_fuzzify_done"
            self._ensure_job_lease(
                run_date=effective_run_date,
                stage=stage,
                lease_lost=lease_lost,
            )

            if stage == "conversation_fuzzify_done":
                await self._fuzzify_and_cleanup_high_value_interaction_events()
                await self._job_repository.advance_stage(
                    run_date=effective_run_date,
                    owner_token=owner_token,
                    lease_seconds=lease_seconds,
                    expected_stage="conversation_fuzzify_done",
                    next_stage="completed",
                )
                stage = "completed"
            self._ensure_job_lease(
                run_date=effective_run_date,
                stage=stage,
                lease_lost=lease_lost,
            )

        except Exception as exc:
            try:
                await self._job_repository.mark_failure(
                    run_date=effective_run_date,
                    owner_token=owner_token,
                    error_code=type(exc).__name__,
                )
            except Exception:
                logger.exception(
                    "[KomariMemory] 每日忘却任务失败状态写入失败: run_date={}",
                    effective_run_date,
                )
            logger.exception(
                "[KomariMemory] 死神脚本执行失败: run_date={}",
                effective_run_date,
            )
            raise
        else:
            logger.info(
                "[KomariMemory] 死神脚本完成: run_date={} stage={}",
                effective_run_date,
                stage,
            )
            return True
        finally:
            stop_heartbeat.set()
            await heartbeat_task

    async def _renew_job_lease(
        self,
        *,
        run_date: date,
        owner_token: str,
        lease_seconds: int,
        stop: asyncio.Event,
        lease_lost: asyncio.Event,
    ) -> None:
        interval = max(1.0, lease_seconds / 3)
        consecutive_errors = 0
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
            except TimeoutError:
                pass
            else:
                return
            try:
                renewed = await self._job_repository.renew(
                    run_date=run_date,
                    owner_token=owner_token,
                    lease_seconds=lease_seconds,
                )
            except Exception:
                consecutive_errors += 1
                logger.exception(
                    "[KomariMemory] 每日忘却任务续租异常: run_date={} failures={}",
                    run_date,
                    consecutive_errors,
                )
                if consecutive_errors < 2:
                    continue
            else:
                if renewed:
                    consecutive_errors = 0
                    continue
            lease_lost.set()
            return

    @staticmethod
    def _ensure_job_lease(
        *,
        run_date: date,
        stage: ForgettingJobStage,
        lease_lost: asyncio.Event,
    ) -> None:
        if lease_lost.is_set() and stage != "completed":
            raise ForgettingJobLeaseLostError(run_date)

    async def _daily_decay(self) -> None:
        """每日衰减：所有记忆重要性按整数退一。"""
        async with self.pg_pool.acquire() as conn:
            await conn.execute(
                _CONVERSATION_DECAY_SQL,
            )
            logger.debug("[KomariMemory] 已按整数退一衰减所有记忆的重要性")

    async def _daily_decay_interaction_events(self) -> None:
        """每日衰减跨群互动事件记忆。"""
        async with self.pg_pool.acquire() as conn:
            await conn.execute(
                _INTERACTION_DECAY_SQL,
            )
            logger.debug("[KomariMemory] 已衰减跨群互动事件记忆的重要性")

    async def _delete_low_value_memories(self) -> int:
        """删除重要性=0的低价值记忆（初始评分≤配置阈值）。

        Returns:
            删除的记录数
        """
        config = self.config
        threshold = config.forgetting_importance_threshold
        min_age_days = config.forgetting_min_age_days
        async with self.pg_pool.acquire() as conn:
            result = await conn.execute(
                _DELETE_LOW_CONVERSATIONS_SQL,
                threshold,
                min_age_days,
            )
            deleted = result.split()[-1] if result else "0"
            logger.debug(
                "[KomariMemory] 删除低价值记忆: {} 条 (阈值: {}, 最小保留天数: {})",
                deleted,
                threshold,
                min_age_days,
            )
            return int(deleted)

    async def _delete_low_value_interaction_events(self) -> int:
        """删除低价值跨群互动事件记忆。"""
        config = self.config
        threshold = config.forgetting_importance_threshold
        min_age_days = config.forgetting_min_age_days
        async with self.pg_pool.acquire() as conn:
            result = await conn.execute(
                _DELETE_LOW_INTERACTIONS_SQL,
                threshold,
                min_age_days,
            )
        deleted = int(result.split()[-1]) if result else 0
        logger.debug("[KomariMemory] 删除低价值跨群互动事件: {} 条", deleted)
        return deleted

    async def _fuzzify_and_cleanup_high_value_memories(self) -> int:
        """处理重要性=0的高价值记忆（初始评分>配置阈值）。

        Returns:
            处理的记录数（删除+模糊化）
        """
        config = self.config
        threshold = config.forgetting_importance_threshold
        min_age_days = config.forgetting_min_age_days
        async with self.pg_pool.acquire() as conn:
            fuzzy_result = await conn.execute(
                """
                DELETE FROM komari_memory_conversations
                WHERE importance_current = 0
                  AND importance_initial > $1
                  AND is_fuzzy = TRUE
                  AND created_at <= NOW() - ($2 * INTERVAL '1 day')
                """,
                threshold,
                min_age_days,
            )
            deleted_fuzzy = int(fuzzy_result.split()[-1]) if fuzzy_result else 0

            rows = await conn.fetch(
                """
                SELECT id, summary
                FROM komari_memory_conversations
                WHERE importance_current = 0
                  AND importance_initial > $1
                  AND is_fuzzy = FALSE
                  AND created_at <= NOW() - ($2 * INTERVAL '1 day')
                """,
                threshold,
                min_age_days,
            )

        if not rows:
            logger.debug(
                "[KomariMemory] 高价值记忆处理: 删除 {} 条, 模糊化 0 条 (最小保留天数: {})",
                deleted_fuzzy,
                min_age_days,
            )
            return deleted_fuzzy

        concurrency = max(1, int(config.forgetting_fuzzify_concurrency))
        semaphore = asyncio.Semaphore(concurrency)
        invalid_records = 0

        async def _fuzzify_record(record: asyncpg.Record | dict[str, Any]) -> bool:
            nonlocal invalid_records
            parsed = _parse_fuzzify_record(record, "summary", "对话记忆")
            if parsed is None:
                invalid_records += 1
                return False

            record_id, summary = parsed
            async with semaphore:
                return await self._fuzzify_conversation(
                    record_id,
                    summary,
                )

        logger.debug(
            "[KomariMemory] 准备并发模糊化高价值记忆: {} 条 (并发上限: {})",
            len(rows),
            concurrency,
        )
        results = await asyncio.gather(
            *(_fuzzify_record(record) for record in rows),
            return_exceptions=True,
        )
        fuzzified_count = 0
        failed_count = invalid_records
        for record, result in zip(rows, results, strict=True):
            if isinstance(result, BaseException):
                failed_count += 1
                logger.opt(exception=result).error(
                    "[KomariMemory] 对话记忆模糊化任务异常，跳过当前记录: ID={}",
                    _safe_record_id_for_log(record),
                )
            elif result:
                fuzzified_count += 1

        if failed_count:
            raise FuzzifyBatchError(
                task_label="对话记忆",
                failed_count=failed_count,
            )

        logger.debug(
            "[KomariMemory] 高价值记忆处理: 删除 {} 条, 模糊化 {} 条 (最小保留天数: {}, 并发上限: {})",
            deleted_fuzzy,
            fuzzified_count,
            min_age_days,
            concurrency,
        )
        return deleted_fuzzy + fuzzified_count

    async def _fuzzify_and_cleanup_high_value_interaction_events(self) -> int:
        """处理高价值跨群互动事件的模糊化或二次删除。"""
        config = self.config
        threshold = config.forgetting_importance_threshold
        min_age_days = config.forgetting_min_age_days
        async with self.pg_pool.acquire() as conn:
            fuzzy_result = await conn.execute(
                """
                DELETE FROM komari_memory_interaction_history
                WHERE importance_current = 0
                  AND importance_initial > $1
                  AND is_fuzzy = TRUE
                  AND created_at <= NOW() - ($2 * INTERVAL '1 day')
                """,
                threshold,
                min_age_days,
            )
            deleted_fuzzy = int(fuzzy_result.split()[-1]) if fuzzy_result else 0
            rows = await conn.fetch(
                """
                SELECT id, event_summary
                FROM komari_memory_interaction_history
                WHERE importance_current = 0
                  AND importance_initial > $1
                  AND is_fuzzy = FALSE
                  AND created_at <= NOW() - ($2 * INTERVAL '1 day')
                """,
                threshold,
                min_age_days,
            )

        if not rows:
            return deleted_fuzzy

        concurrency = max(1, int(config.forgetting_fuzzify_concurrency))
        semaphore = asyncio.Semaphore(concurrency)
        invalid_records = 0

        async def _fuzzify_record(record: asyncpg.Record | dict[str, Any]) -> bool:
            nonlocal invalid_records
            parsed = _parse_fuzzify_record(record, "event_summary", "跨群互动事件")
            if parsed is None:
                invalid_records += 1
                return False

            record_id, summary = parsed
            async with semaphore:
                return await self._fuzzify_interaction_event(
                    record_id,
                    summary,
                )

        results = await asyncio.gather(
            *(_fuzzify_record(record) for record in rows),
            return_exceptions=True,
        )
        fuzzified_count = 0
        failed_count = invalid_records
        for record, result in zip(rows, results, strict=True):
            if isinstance(result, BaseException):
                failed_count += 1
                logger.opt(exception=result).error(
                    "[KomariMemory] 跨群互动事件模糊化任务异常，跳过当前记录: ID={}",
                    _safe_record_id_for_log(record),
                )
            elif result:
                fuzzified_count += 1
        if failed_count:
            raise FuzzifyBatchError(
                task_label="跨群互动事件",
                failed_count=failed_count,
            )
        logger.debug(
            "[KomariMemory] 高价值跨群互动事件处理: 删除 {} 条, 模糊化 {} 条",
            deleted_fuzzy,
            fuzzified_count,
        )
        return deleted_fuzzy + fuzzified_count

    async def _fuzzify_conversation(self, conv_id: int, original_summary: str) -> bool:
        """模糊化对话记忆并重置重要性。"""
        collector = agent_run_logger_plugin.create_collector(
            run_type="scheduled_summary",
            task_kind="forgetting_conversation",
            trace_id=f"memfuzzy-{conv_id}",
            input_data={
                "conversation_id": conv_id,
                "original_summary": original_summary,
            },
        )
        try:
            fuzzy_summary = await self._generate_fuzzy_summary(
                original_summary,
                conv_id,
                collector,
            )
        except asyncio.CancelledError as error:
            await agent_run_logger_plugin.finalize_collector(
                collector,
                status="cancelled",
                error=error,
                skip_if_no_calls=True,
            )
            raise
        except Exception as error:
            logger.exception(
                "[KomariMemory] 模糊化重试失败，删除记忆且不写入占位文本: ID={}",
                conv_id,
            )
            try:
                deleted = await self._delete_conversation_after_fuzzify_failure(
                    conv_id,
                    original_summary,
                )
            except asyncio.CancelledError as cleanup_error:
                await agent_run_logger_plugin.finalize_collector(
                    collector,
                    status="cancelled",
                    error=cleanup_error,
                    skip_if_no_calls=True,
                )
                raise
            except Exception as cleanup_error:
                await agent_run_logger_plugin.finalize_collector(
                    collector,
                    status="error",
                    error=cleanup_error,
                    skip_if_no_calls=True,
                )
                raise
            await agent_run_logger_plugin.finalize_collector(
                collector,
                status="error",
                output={"deleted_after_failure": deleted},
                error=error,
                skip_if_no_calls=True,
            )
            return deleted

        try:
            embedding = str(await self._embedding_plugin.embed(fuzzy_summary))
        except asyncio.CancelledError as error:
            await agent_run_logger_plugin.finalize_collector(
                collector,
                status="cancelled",
                error=error,
                skip_if_no_calls=True,
            )
            raise
        except Exception as error:
            embedding = None
            logger.exception(
                "[KomariMemory] 模糊摘要向量化失败，将发布新正文并删除旧向量: ID={}",
                conv_id,
            )
            if collector is not None:
                collector.add_error(
                    "forgetting_embedding",
                    type(error).__name__,
                    str(error),
                )

        try:
            updated = await self._publish_fuzzy_conversation(
                conv_id=conv_id,
                original_summary=original_summary,
                fuzzy_summary=fuzzy_summary,
                embedding=embedding,
            )
        except asyncio.CancelledError as error:
            await agent_run_logger_plugin.finalize_collector(
                collector,
                status="cancelled",
                error=error,
                skip_if_no_calls=True,
            )
            raise
        except Exception as error:
            await agent_run_logger_plugin.finalize_collector(
                collector,
                status="error",
                error=error,
                skip_if_no_calls=True,
            )
            raise
        if updated:
            logger.debug("[KomariMemory] 模糊化记忆: ID={}", conv_id)
        await agent_run_logger_plugin.finalize_collector(
            collector,
            status="success",
            output={
                "conversation_id": conv_id,
                "fuzzy_summary": fuzzy_summary,
                "embedding_created": embedding is not None,
                "cas_updated": updated,
            },
            skip_if_no_calls=True,
        )
        return updated

    @retry_async(max_attempts=3, base_delay=1.0)
    async def _generate_fuzzy_summary(
        self,
        original: str,
        conv_id: int,
        collector: AgentRunCollector | None = None,
    ) -> str:
        """生成模糊化总结，并只保留正文。"""
        config = self.config
        tag = (config.response_tag or "content").strip() or "content"
        prompt = (
            "请将下面的对话总结模糊化为一句简短的简体中文概要。\n"
            "要求：\n"
            "1. 只保留核心主题，删除具体细节、数量、时间、地点、称呼和原话。\n"
            "2. 输出必须是一句简短自然的简体中文，不要换行。\n"
            f"3. 最终只能输出 <{tag}>模糊化后的结果</{tag}>。\n"
            "4. 标签外不要输出任何解释、前缀、后缀、Markdown、代码块或引号。\n\n"
            + _render_bounded_memory_context(
                content=original,
                source_id=f"forgetting-conversation:{conv_id}",
            )
        )

        request_data = {
            "prompt": prompt,
            "model": config.llm_model_summary,
            "temperature": config.llm_temperature_summary,
            "max_tokens": min(config.llm_max_tokens_summary, 120),
            # 测试替身快照可能缺失新槽位字段，回退到与 Schema 默认值一致的显式值
            "request_api": getattr(
                config, "llm_request_api_summary", "chat_completions"
            ),
            "stream_enabled": getattr(config, "llm_stream_enabled_summary", False),
            "thinking_mode": config.llm_thinking_mode_summary,
            "reasoning_effort": config.llm_reasoning_effort_summary,
        }
        from komari_bot.plugins.agent_run_logger.diagnostic import (
            record_completion_call,
            record_failed_call,
        )

        try:
            if collector is None:
                content = await llm_provider.generate_text(
                    **request_data,
                    request_trace_id=f"memfuzzy-{conv_id}",
                    request_phase="forgetting_fuzzify",
                )
                completion = None
            else:
                completion = await llm_provider.generate_completion(
                    **request_data,
                    request_trace_id=f"memfuzzy-{conv_id}",
                    request_phase="forgetting_fuzzify",
                )
                content = completion.content
        except Exception as error:
            record_failed_call(
                collector,
                phase="forgetting_fuzzify",
                round_index=len(collector.calls) if collector is not None else 0,
                method="generate_completion",
                model=config.llm_model_summary,
                request=request_data,
                error=error,
            )
            raise

        if completion is not None:
            assert collector is not None
            record_completion_call(
                collector,
                phase="forgetting_fuzzify",
                round_index=len(collector.calls),
                method="generate_completion",
                model=config.llm_model_summary,
                request=request_data,
                completion=completion,
            )
        fuzzy_summary = _extract_tag_content(content, tag)
        if _is_invalid_fuzzy_summary(fuzzy_summary):
            msg = f"模糊化结果无效: ID={conv_id}"
            raise ValueError(msg)

        return fuzzy_summary

    async def _publish_fuzzy_conversation(
        self,
        *,
        conv_id: int,
        original_summary: str,
        fuzzy_summary: str,
        embedding: str | None,
    ) -> bool:
        """以旧正文和待模糊状态做 CAS，并在同一事务更新或清除向量。"""
        async with self.pg_pool.acquire() as conn, conn.transaction():
            updated_id = await conn.fetchval(
                """
                UPDATE komari_memory_conversations
                SET summary = $1,
                    is_fuzzy = TRUE,
                    importance_current = importance_initial
                WHERE id = $2
                  AND summary = $3
                  AND importance_current = 0
                  AND is_fuzzy = FALSE
                RETURNING id
                """,
                fuzzy_summary,
                conv_id,
                original_summary,
            )
            if updated_id is None:
                return False
            if embedding is None:
                await conn.execute(
                    """
                    DELETE FROM komari_memory_conversation_embeddings
                    WHERE conversation_id = $1
                    """,
                    conv_id,
                )
            else:
                await conn.execute(
                    """
                    INSERT INTO komari_memory_conversation_embeddings
                        (conversation_id, content_hash, embedding, embedding_dim)
                    VALUES ($1, $2, $3, vector_dims($3::vector))
                    ON CONFLICT (conversation_id) DO UPDATE SET
                        content_hash = EXCLUDED.content_hash,
                        embedding = EXCLUDED.embedding,
                        embedding_dim = EXCLUDED.embedding_dim,
                        embedded_at = CURRENT_TIMESTAMP
                    """,
                    conv_id,
                    _build_content_hash(fuzzy_summary),
                    embedding,
                )
        return True

    async def _delete_conversation_after_fuzzify_failure(
        self,
        conv_id: int,
        original_summary: str,
    ) -> bool:
        """模糊化重试失败后删除对话记忆。"""
        async with self.pg_pool.acquire() as conn:
            deleted_id = await conn.fetchval(
                """
                DELETE FROM komari_memory_conversations
                WHERE id = $1
                  AND summary = $2
                  AND importance_current = 0
                  AND is_fuzzy = FALSE
                RETURNING id
                """,
                conv_id,
                original_summary,
            )

        if deleted_id is not None:
            logger.info("[KomariMemory] 模糊化重试失败，已删除记忆 ID={}", conv_id)
        return deleted_id is not None

    async def _fuzzify_interaction_event(self, event_id: int, original_summary: str) -> bool:
        """模糊化跨群互动事件并重置重要性。"""
        collector = agent_run_logger_plugin.create_collector(
            run_type="scheduled_summary",
            task_kind="forgetting_interaction",
            trace_id=f"memfuzzy-interaction-{event_id}",
            input_data={
                "interaction_id": event_id,
                "original_summary": original_summary,
            },
        )
        try:
            fuzzy_summary = await self._generate_fuzzy_interaction_summary(
                original_summary,
                event_id,
                collector,
            )
        except asyncio.CancelledError as error:
            await agent_run_logger_plugin.finalize_collector(
                collector,
                status="cancelled",
                error=error,
                skip_if_no_calls=True,
            )
            raise
        except Exception as error:
            logger.exception(
                "[KomariMemory] 跨群互动事件模糊化重试失败，删除事件且不写入占位文本: ID={}",
                event_id,
            )
            try:
                deleted = await self._delete_interaction_after_fuzzify_failure(
                    event_id,
                    original_summary,
                )
            except asyncio.CancelledError as cleanup_error:
                await agent_run_logger_plugin.finalize_collector(
                    collector,
                    status="cancelled",
                    error=cleanup_error,
                    skip_if_no_calls=True,
                )
                raise
            except Exception as cleanup_error:
                await agent_run_logger_plugin.finalize_collector(
                    collector,
                    status="error",
                    error=cleanup_error,
                    skip_if_no_calls=True,
                )
                raise
            await agent_run_logger_plugin.finalize_collector(
                collector,
                status="error",
                output={"deleted_after_failure": deleted},
                error=error,
                skip_if_no_calls=True,
            )
            return deleted

        try:
            embedding = str(await self._embedding_plugin.embed(fuzzy_summary))
        except asyncio.CancelledError as error:
            await agent_run_logger_plugin.finalize_collector(
                collector,
                status="cancelled",
                error=error,
                skip_if_no_calls=True,
            )
            raise
        except Exception as error:
            embedding = None
            logger.exception(
                "[KomariMemory] 互动模糊摘要向量化失败，将发布新正文并删除旧向量: ID={}",
                event_id,
            )
            if collector is not None:
                collector.add_error(
                    "forgetting_interaction_embedding",
                    type(error).__name__,
                    str(error),
                )
        try:
            updated = await self._publish_fuzzy_interaction_event(
                event_id=event_id,
                original_summary=original_summary,
                fuzzy_summary=fuzzy_summary,
                embedding=embedding,
            )
        except asyncio.CancelledError as error:
            await agent_run_logger_plugin.finalize_collector(
                collector,
                status="cancelled",
                error=error,
                skip_if_no_calls=True,
            )
            raise
        except Exception as error:
            await agent_run_logger_plugin.finalize_collector(
                collector,
                status="error",
                error=error,
                skip_if_no_calls=True,
            )
            raise
        if updated:
            logger.debug("[KomariMemory] 模糊化跨群互动事件: ID={}", event_id)
        await agent_run_logger_plugin.finalize_collector(
            collector,
            status="success",
            output={
                "interaction_id": event_id,
                "fuzzy_summary": fuzzy_summary,
                "embedding_created": embedding is not None,
                "cas_updated": updated,
            },
            skip_if_no_calls=True,
        )
        return updated

    @retry_async(max_attempts=3, base_delay=1.0)
    async def _generate_fuzzy_interaction_summary(
        self,
        original: str,
        event_id: int,
        collector: AgentRunCollector | None = None,
    ) -> str:
        """生成跨群互动事件模糊化总结。"""
        config = self.config
        tag = (config.response_tag or "content").strip() or "content"
        prompt = (
            "请将下面的小鞠与某用户的长期互动事件记忆模糊化为一句简短的简体中文概要。\n"
            "要求：\n"
            "1. 保留关系基调、用户偏好倾向和互动模式。\n"
            "2. 淡化具体消息、时间线、可识别细节，不引入群号或群名。\n"
            f"3. 最终只能输出 <{tag}>模糊化后的结果</{tag}>。\n"
            "4. 标签外不要输出任何解释。\n\n"
            + _render_bounded_memory_context(
                content=original,
                source_id=f"forgetting-interaction:{event_id}",
            )
        )
        request_data = {
            "prompt": prompt,
            "model": config.llm_model_summary,
            "temperature": config.llm_temperature_summary,
            "max_tokens": min(config.llm_max_tokens_summary, 120),
            # 测试替身快照可能缺失新槽位字段，回退到与 Schema 默认值一致的显式值
            "request_api": getattr(
                config, "llm_request_api_summary", "chat_completions"
            ),
            "stream_enabled": getattr(config, "llm_stream_enabled_summary", False),
            "thinking_mode": config.llm_thinking_mode_summary,
            "reasoning_effort": config.llm_reasoning_effort_summary,
        }
        from komari_bot.plugins.agent_run_logger.diagnostic import (
            record_completion_call,
            record_failed_call,
        )

        try:
            if collector is None:
                content = await llm_provider.generate_text(
                    **request_data,
                    request_trace_id=f"memfuzzy-interaction-{event_id}",
                    request_phase="forgetting_interaction_fuzzify",
                )
                completion = None
            else:
                completion = await llm_provider.generate_completion(
                    **request_data,
                    request_trace_id=f"memfuzzy-interaction-{event_id}",
                    request_phase="forgetting_interaction_fuzzify",
                )
                content = completion.content
        except Exception as error:
            record_failed_call(
                collector,
                phase="forgetting_interaction_fuzzify",
                round_index=len(collector.calls) if collector is not None else 0,
                method="generate_completion",
                model=config.llm_model_summary,
                request=request_data,
                error=error,
            )
            raise

        if completion is not None:
            assert collector is not None
            record_completion_call(
                collector,
                phase="forgetting_interaction_fuzzify",
                round_index=len(collector.calls),
                method="generate_completion",
                model=config.llm_model_summary,
                request=request_data,
                completion=completion,
            )
        fuzzy_summary = _extract_tag_content(content, tag)
        if _is_invalid_fuzzy_summary(fuzzy_summary):
            msg = f"跨群互动事件模糊化结果无效: ID={event_id}"
            raise ValueError(msg)

        return fuzzy_summary

    async def _publish_fuzzy_interaction_event(
        self,
        *,
        event_id: int,
        original_summary: str,
        fuzzy_summary: str,
        embedding: str | None,
    ) -> bool:
        """以旧互动正文和待模糊状态做 CAS，并同步更新或清除向量。"""
        async with self.pg_pool.acquire() as conn, conn.transaction():
            updated_id = await conn.fetchval(
                """
                UPDATE komari_memory_interaction_history
                SET event_summary = $1,
                    is_fuzzy = TRUE,
                    importance_current = importance_initial
                WHERE id = $2
                  AND event_summary = $3
                  AND importance_current = 0
                  AND is_fuzzy = FALSE
                RETURNING id
                """,
                fuzzy_summary,
                event_id,
                original_summary,
            )
            if updated_id is None:
                return False
            if embedding is None:
                await conn.execute(
                    """
                    DELETE FROM komari_memory_interaction_embeddings
                    WHERE interaction_id = $1
                    """,
                    event_id,
                )
            else:
                await conn.execute(
                    """
                    INSERT INTO komari_memory_interaction_embeddings
                        (interaction_id, content_hash, embedding, embedding_dim)
                    VALUES ($1, $2, $3, vector_dims($3::vector))
                    ON CONFLICT (interaction_id) DO UPDATE SET
                        content_hash = EXCLUDED.content_hash,
                        embedding = EXCLUDED.embedding,
                        embedding_dim = EXCLUDED.embedding_dim,
                        embedded_at = CURRENT_TIMESTAMP
                    """,
                    event_id,
                    _build_content_hash(fuzzy_summary),
                    embedding,
                )
        return True

    async def _delete_interaction_after_fuzzify_failure(
        self,
        event_id: int,
        original_summary: str,
    ) -> bool:
        """模糊化重试失败后删除跨群互动事件。"""
        async with self.pg_pool.acquire() as conn:
            deleted_id = await conn.fetchval(
                """
                DELETE FROM komari_memory_interaction_history
                WHERE id = $1
                  AND event_summary = $2
                  AND importance_current = 0
                  AND is_fuzzy = FALSE
                RETURNING id
                """,
                event_id,
                original_summary,
            )

        if deleted_id is not None:
            logger.info(
                "[KomariMemory] 跨群互动事件模糊化重试失败，已删除事件 ID={}",
                event_id,
            )
        return deleted_id is not None
