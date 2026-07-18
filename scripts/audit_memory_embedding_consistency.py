"""只读检查 Komari Memory 正文、content_hash 与向量维度的一致性。"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any

from komari_bot.common.database_config import load_database_config_from_env
from komari_bot.common.postgres import create_postgres_pool

if TYPE_CHECKING:
    from collections.abc import AsyncIterable, Iterable

    import asyncpg


@dataclass(slots=True)
class TableAuditResult:
    """单张正文表的只读核对结果，不包含正文。"""

    scanned: int = 0
    hash_mismatches: int = 0
    dimension_mismatches: int = 0
    unindexed: int = 0
    hash_mismatch_ids: list[int] = field(default_factory=list)
    dimension_mismatch_ids: list[int] = field(default_factory=list)
    unindexed_ids: list[int] = field(default_factory=list)

    @property
    def inconsistent(self) -> int:
        return self.hash_mismatches + self.dimension_mismatches


def audit_rows(
    rows: Iterable[dict[str, Any]],
    *,
    sample_limit: int,
) -> TableAuditResult:
    """核对已提取的元数据行；输出只保留记录 ID 和计数。"""
    result = TableAuditResult()
    for row in rows:
        result.scanned += 1
        record_id = int(row["record_id"])
        content = str(row["content"])
        content_hash = row.get("content_hash")
        if content_hash is None:
            result.unindexed += 1
            _append_sample(result.unindexed_ids, record_id, sample_limit)
            continue

        expected_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        if str(content_hash) != expected_hash:
            result.hash_mismatches += 1
            _append_sample(result.hash_mismatch_ids, record_id, sample_limit)

        stored_dimension = row.get("stored_dimension")
        actual_dimension = row.get("actual_dimension")
        if (
            stored_dimension is None
            or actual_dimension is None
            or int(stored_dimension) != int(actual_dimension)
        ):
            result.dimension_mismatches += 1
            _append_sample(result.dimension_mismatch_ids, record_id, sample_limit)
    return result


def _append_sample(target: list[int], record_id: int, sample_limit: int) -> None:
    if len(target) < sample_limit:
        target.append(record_id)


async def _audit_cursor(
    rows: AsyncIterable[Any],
    *,
    sample_limit: int,
) -> TableAuditResult:
    materialized: list[dict[str, Any]] = []
    result = TableAuditResult()
    async for row in rows:
        materialized.append(dict(row))
        if len(materialized) < 500:
            continue
        _merge_result(result, audit_rows(materialized, sample_limit=sample_limit), sample_limit)
        materialized.clear()
    if materialized:
        _merge_result(result, audit_rows(materialized, sample_limit=sample_limit), sample_limit)
    return result


def _merge_result(
    target: TableAuditResult,
    source: TableAuditResult,
    sample_limit: int,
) -> None:
    target.scanned += source.scanned
    target.hash_mismatches += source.hash_mismatches
    target.dimension_mismatches += source.dimension_mismatches
    target.unindexed += source.unindexed
    for target_ids, source_ids in (
        (target.hash_mismatch_ids, source.hash_mismatch_ids),
        (target.dimension_mismatch_ids, source.dimension_mismatch_ids),
        (target.unindexed_ids, source.unindexed_ids),
    ):
        remaining = max(0, sample_limit - len(target_ids))
        target_ids.extend(source_ids[:remaining])


async def _audit_table(
    pool: asyncpg.Pool,
    *,
    query: str,
    sample_limit: int,
) -> TableAuditResult:
    async with pool.acquire() as conn, conn.transaction():
        cursor = conn.cursor(query, prefetch=500)
        return await _audit_cursor(cursor, sample_limit=sample_limit)


async def run(*, sample_limit: int) -> dict[str, object]:
    """执行只读核对并返回可序列化报告。"""
    database_config = load_database_config_from_env()
    pool = await create_postgres_pool(database_config, command_timeout=60)
    try:
        conversations = await _audit_table(
            pool,
            query="""
                SELECT
                    c.id AS record_id,
                    c.summary AS content,
                    e.content_hash,
                    e.embedding_dim AS stored_dimension,
                    vector_dims(e.embedding) AS actual_dimension
                FROM komari_memory_conversations c
                LEFT JOIN komari_memory_conversation_embeddings e
                    ON e.conversation_id = c.id
                ORDER BY c.id
            """,
            sample_limit=sample_limit,
        )
        interactions = await _audit_table(
            pool,
            query="""
                SELECT
                    h.id AS record_id,
                    h.event_summary AS content,
                    e.content_hash,
                    e.embedding_dim AS stored_dimension,
                    vector_dims(e.embedding) AS actual_dimension
                FROM komari_memory_interaction_history h
                LEFT JOIN komari_memory_interaction_embeddings e
                    ON e.interaction_id = h.id
                ORDER BY h.id
            """,
            sample_limit=sample_limit,
        )
    finally:
        await pool.close()

    return {
        "read_only": True,
        "conversation": {
            **asdict(conversations),
            "inconsistent": conversations.inconsistent,
        },
        "interaction": {
            **asdict(interactions),
            "inconsistent": interactions.inconsistent,
        },
        "total_inconsistent": conversations.inconsistent + interactions.inconsistent,
        "total_unindexed": conversations.unindexed + interactions.unindexed,
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="只读检查记忆正文、content_hash 与向量维度的一致性",
    )
    parser.add_argument(
        "--sample-limit",
        type=int,
        default=100,
        help="每类异常最多输出多少个记录 ID（默认 100）",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    sample_limit = max(0, int(args.sample_limit))
    report = asyncio.run(run(sample_limit=sample_limit))
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))  # noqa: T201
    total_inconsistent = report["total_inconsistent"]
    return 1 if isinstance(total_inconsistent, int) and total_inconsistent > 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
