"""TSK-232 轮 B —— 0013 coordinated contract 阻断矩阵红基线（门控 PG+Redis）。

锁定 0013 迁移在既有 LOCK/DROP/RENAME 语义之上的准入终检契约：

- 每个 closed code 一条阻断用例：构造单一违规态 → ``upgrade 0013`` 以
  RuntimeError 失败、错误文本只含 closed code 与聚合 count（**绝不出现**
  ``minimum_fulfillment_id`` 或任何动态身份 canary）、revision 停留 0012：
  POLICY_MISSING / POLICY_FINGERPRINT_DRIFT / GATE_PHASE_INVALID /
  RESERVATION_LEDGER_INCOMPLETE / ATTESTATION_MISMATCH /
  REPLY_NOT_STARTED_RESIDUAL / REPLY_PAYLOAD_NOT_MINIMIZED /
  REPLY_LEASE_RESIDUAL / KNOWLEDGE_SOURCE_CONFLICT_NONZERO /
  ANNOUNCEMENT_PROCESSING_RESIDUAL / MEMORY_JOB_LEASE_RESIDUAL。
- held-state 安全非零白名单：DEFERRED 提案、PENDING_CONFIRMATION 回复、
  DELIVERED 行未完成 commitment、既有 conversation dead-letter 一律不得
  阻断 0013。
- 成功路径：migration-only 物理删除（gate / cleanup ledger / quarantine
  ledger / komari_plugin_configs / 旧 outbox）、六列 RENAME 保持轮 A 语义、
  cutover-cancelled 行的 proactive commitment 子行删除且 parent 保留无正文
  tombstone、downgrade 拒绝 ``0013_TSK232_IS_IRREVERSIBLE``。
- fresh 豁免：空库（is_fresh=true、双 ledger 空、digest NULL）不经验证
  直通 head。

Redis 认证：非零数据场景先以真实 finalize-redis CLI（测试逻辑库隔离）
产出合法 attestation，再注入违规。隔离库 = 门控库名 +
``_tsk232b1c13<tag>``，用例结束 DROP。
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit, urlunsplit

from tests.cutover.support import (
    CANARY_BODY_TOKEN,
    CANARY_GROUP_A,
    REDIS_URL,
    SKIP_NO_POSTGRES,
    SKIP_NO_REDIS,
    VALID_POLICY,
    VALID_POLICY_FINGERPRINT,
    drop_scratch_database,
    fetch_gate_row,
    overwrite_admission_policy,
    recreate_scratch_database,
    run_bootstrap,
    run_cli,
    scratch_url,
    seed_legacy_configs,
    stage_gate_phase,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    import pytest

pytestmark = [SKIP_NO_POSTGRES]

_REDIS_TEST_DB = 15
_GATE_TABLE = "komari_group_admission_gate"
_CLEANUP_KEY_PREFIXES = ("komari_chat:proactive:", "komari_memory:")


# ---------------------------------------------------------------------------
# 编排 helper：0011 停链 → 0012 升级 → Redis 认证
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _scratch_at_revision(
    tag: str,
    revision: str,
) -> Iterator[tuple[dict[str, Any], str]]:
    """重建 ``_tsk232b1c13<tag>`` 隔离库并 upgrade 到指定 revision。"""
    params = asyncio.run(recreate_scratch_database(f"_tsk232b1c13{tag}"))
    database_url = scratch_url(str(params["database"]))
    try:
        result = run_bootstrap(database_url, "upgrade", revision)
        assert result.returncode == 0, result.stderr
        yield params, database_url
    finally:
        asyncio.run(drop_scratch_database(str(params["database"])))


async def _connect(params: dict[str, Any]) -> Any:
    import asyncpg

    return await asyncpg.connect(**params)


async def _fetch_version(params: dict[str, Any]) -> str:
    connection = await _connect(params)
    try:
        value = await connection.fetchval("SELECT version_num FROM alembic_version")
    finally:
        await connection.close()
    return str(value)


async def _table_exists(params: dict[str, Any], table: str) -> bool:
    connection = await _connect(params)
    try:
        value = await connection.fetchval(
            "SELECT EXISTS (SELECT 1 FROM information_schema.tables"
            " WHERE table_name = $1)",
            table,
        )
    finally:
        await connection.close()
    return bool(value)


async def _column_names(params: dict[str, Any], table: str) -> set[str]:
    connection = await _connect(params)
    try:
        rows = await connection.fetch(
            "SELECT column_name FROM information_schema.columns"
            " WHERE table_name = $1",
            table,
        )
    finally:
        await connection.close()
    return {str(row["column_name"]) for row in rows}


async def _clear_fresh_flag(params: dict[str, Any]) -> None:
    """存量生产库语义：cutover 库不是 fresh 安装。"""
    connection = await _connect(params)
    try:
        await connection.execute(f"UPDATE {_GATE_TABLE} SET is_fresh = FALSE")
    finally:
        await connection.close()


def _redis_test_url() -> str:
    if not REDIS_URL:
        return ""
    parts = urlsplit(REDIS_URL)
    return urlunsplit(
        (parts.scheme, parts.netloc, f"/{_REDIS_TEST_DB}", parts.query, "")
    )


def _finalize_argv(database_url: str) -> list[str]:
    return [
        "finalize-redis",
        "--database-url",
        database_url,
        "--redis-url",
        _redis_test_url(),
        "--expected-fingerprint",
        VALID_POLICY_FINGERPRINT,
        "--apply",
    ]


def _arrange_evidence_phase(params: dict[str, Any]) -> None:
    overwrite_admission_policy(params, VALID_POLICY)
    stage_gate_phase(
        params,
        phase="REDIS_EVIDENCE_CAPTURED",
        policy_revision=1,
        policy_fingerprint=VALID_POLICY_FINGERPRINT,
    )


def _upgrade_0012(_params: dict[str, Any], database_url: str) -> None:
    result = run_bootstrap(database_url, "upgrade", "0012")
    assert result.returncode == 0, result.stderr


def _authenticate_via_finalize(
    capsys: pytest.CaptureFixture[str],
    params: dict[str, Any],
    database_url: str,
) -> None:
    """真实 finalize-redis CLI 产出合法 attestation（空 Redis 测试逻辑库）。"""
    exit_code, payload = run_cli(capsys, *_finalize_argv(database_url))
    assert exit_code == 0, payload
    gate = fetch_gate_row(params)
    assert gate is not None
    assert gate["phase"] == "REDIS_FINALIZED"


def _expect_0013_blocked(
    database_url: str,
    closed_code: str,
) -> None:
    """驱动 upgrade 0013 并断言：失败、closed code、revision 保持 0012。"""
    result = run_bootstrap(database_url, "upgrade", "0013")
    assert result.returncode != 0, f"{closed_code} 未被阻断"
    output = f"{result.stdout}\n{result.stderr}"
    assert closed_code in output, f"缺少 closed code {closed_code}: {output}"
    assert "minimum_fulfillment_id" not in output
    assert CANARY_BODY_TOKEN not in output


# ---------------------------------------------------------------------------
# 单一违规注入 helper（认证后的 0012 终态上逐项叠加）
# ---------------------------------------------------------------------------


async def _inject_policy_missing(params: dict[str, Any]) -> None:
    connection = await _connect(params)
    try:
        await connection.execute("DELETE FROM komari_group_admission_config")
    finally:
        await connection.close()


async def _seed_unfinished_ledger_row(params: dict[str, Any]) -> None:
    connection = await _connect(params)
    try:
        await connection.execute(
            """
            INSERT INTO komari_reply_reservation_cleanup_ledger (
                reservation_id, group_id
            ) VALUES ('resv-c13-incomplete', 123456789)
            """
        )
    finally:
        await connection.close()


async def _corrupt_attestation_digest(params: dict[str, Any]) -> None:
    connection = await _connect(params)
    try:
        await connection.execute(
            f"UPDATE {_GATE_TABLE} SET redis_finalizer_digest = $1", "e" * 64
        )
    finally:
        await connection.close()


async def _seed_not_started_parent(params: dict[str, Any]) -> None:
    connection = await _connect(params)
    try:
        await connection.execute(
            """
            INSERT INTO komari_chat_reply_fulfillments (
                fulfillment_id, payload_hash, request_trace_id,
                trigger_message_id, trigger_user_id, group_id,
                bot_self_id, adapter_name, reply_target_message_id,
                reply_content, delivery_state
            ) VALUES (
                'fulfill-c13-not-started', 'hash-c13-ns',
                'trace-c13-ns', 'trigger-c13-ns', '888777666', '123456789',
                'canary-self', 'test', 'target-c13-ns',
                'c13-canary-body', 'NOT_STARTED'
            )
            """
        )
    finally:
        await connection.close()


async def _seed_minimization_violation_parent(params: dict[str, Any]) -> None:
    """未最小化的 cutover-cancelled 形态：NOT_DELIVERED 却保留正文与 payload。"""
    connection = await _connect(params)
    try:
        await connection.execute(
            """
            INSERT INTO komari_chat_reply_fulfillments (
                fulfillment_id, payload_hash, request_trace_id,
                trigger_message_id, trigger_user_id, group_id,
                bot_self_id, adapter_name, reply_target_message_id,
                reply_content, delivery_state,
                send_started_at, not_delivered_at
            ) VALUES (
                'fulfill-c13-unminimized', 'hash-c13-unmin',
                'trace-c13-unmin', 'trigger-c13-unmin', '888777666',
                '123456789', 'canary-self', 'test', 'target-c13-unmin',
                'c13-canary-body-leak', 'NOT_DELIVERED', NOW(), NOW()
            )
            """
        )
        await connection.execute(
            """
            INSERT INTO komari_chat_reply_fulfillment_commitments (
                fulfillment_id, commitment_type, state, payload
            ) VALUES (
                'fulfill-c13-unminimized',
                'proactive_reply_confirmation', 'PENDING',
                '{"reservation_id": "resv-c13-unmin"}'::jsonb
            )
            """
        )
    finally:
        await connection.close()


async def _seed_lease_residual_parent(params: dict[str, Any]) -> None:
    connection = await _connect(params)
    try:
        await connection.execute(
            """
            INSERT INTO komari_chat_reply_fulfillments (
                fulfillment_id, payload_hash, request_trace_id,
                trigger_message_id, trigger_user_id, group_id,
                bot_self_id, adapter_name, reply_target_message_id,
                reply_content, delivery_state,
                send_started_at, delivered_at,
                lease_owner, lease_expires_at
            ) VALUES (
                'fulfill-c13-lease', 'hash-c13-lease',
                'trace-c13-lease', 'trigger-c13-lease', '888777666',
                '123456789', 'canary-self', 'test', 'target-c13-lease',
                NULL, 'DELIVERED', NOW(), NOW(),
                'owner-c13-residual', NOW() + INTERVAL '1 hour'
            )
            """
        )
    finally:
        await connection.close()


async def _seed_conflict_hold_proposal(params: dict[str, Any]) -> None:
    connection = await _connect(params)
    try:
        await connection.execute(
            """
            INSERT INTO komari_custom_proposals (
                group_id, proposer_id, title, content, status,
                publication_key, required_votes,
                admission_state, execution_hold_code
            ) VALUES (
                123456789, 888777666, 'hold-canary', '正文', 'approving',
                'pub-key-c13-hold', 3, 'ACTIVE',
                'KNOWLEDGE_SOURCE_CONFLICT'
            )
            """
        )
    finally:
        await connection.close()


async def _seed_processing_announcement(params: dict[str, Any]) -> None:
    connection = await _connect(params)
    try:
        await connection.execute(
            """
            INSERT INTO komari_announcement_dispatches (
                request_id, payload_hash, status, owner_token,
                lease_expires_at
            ) VALUES ('req-c13-processing', 'hash-c13', 'processing',
                      'owner-c13', NOW() + INTERVAL '10 minutes')
            """
        )
    finally:
        await connection.close()


async def _seed_leasing_memory_job(params: dict[str, Any]) -> None:
    connection = await _connect(params)
    try:
        await connection.execute(
            """
            INSERT INTO komari_memory_jobs (
                job_name, run_date, owner_token, lease_until, stage
            ) VALUES ('forgetting_c13_residual', CURRENT_DATE,
                      'owner-c13', NOW() + INTERVAL '1 hour', 'claimed')
            """
        )
    finally:
        await connection.close()


# ---------------------------------------------------------------------------
# 阻断矩阵：每个 closed code 一条
# ---------------------------------------------------------------------------


@SKIP_NO_REDIS
def test_0013_blocks_with_policy_missing(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with _scratch_at_revision("pm", "0011") as (params, database_url):
        _arrange_evidence_phase(params)
        asyncio.run(_clear_fresh_flag(params))
        _upgrade_0012(params, database_url)
        _authenticate_via_finalize(capsys, params, database_url)

        asyncio.run(_inject_policy_missing(params))
        _expect_0013_blocked(database_url, "POLICY_MISSING")
        assert asyncio.run(_fetch_version(params)) == "0012"


@SKIP_NO_REDIS
def test_0013_blocks_with_policy_fingerprint_drift(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with _scratch_at_revision("pf", "0011") as (params, database_url):
        _arrange_evidence_phase(params)
        asyncio.run(_clear_fresh_flag(params))
        _upgrade_0012(params, database_url)
        _authenticate_via_finalize(capsys, params, database_url)

        overwrite_admission_policy(
            params, {"mode": "whitelist", "group_ids": [42]}
        )
        _expect_0013_blocked(database_url, "POLICY_FINGERPRINT_DRIFT")
        assert asyncio.run(_fetch_version(params)) == "0012"


def test_0013_blocks_with_invalid_gate_phase_without_redis() -> None:
    """phase≠REDIS_FINALIZED 且不满足 fresh 豁免 → GATE_PHASE_INVALID（纯 PG）。"""
    with _scratch_at_revision("gp", "0011") as (params, database_url):
        _arrange_evidence_phase(params)
        asyncio.run(_clear_fresh_flag(params))
        _upgrade_0012(params, database_url)
        # 不做 finalize：phase 停在 POSTGRES_BACKFILLED 且 digest 为 NULL
        _expect_0013_blocked(database_url, "GATE_PHASE_INVALID")
        assert asyncio.run(_fetch_version(params)) == "0012"
        gate = fetch_gate_row(params)
        assert gate is not None
        assert gate["phase"] == "POSTGRES_BACKFILLED"


@SKIP_NO_REDIS
def test_0013_blocks_with_reservation_ledger_incomplete(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with _scratch_at_revision("li", "0011") as (params, database_url):
        _arrange_evidence_phase(params)
        asyncio.run(_clear_fresh_flag(params))
        _upgrade_0012(params, database_url)
        _authenticate_via_finalize(capsys, params, database_url)

        asyncio.run(_seed_unfinished_ledger_row(params))
        _expect_0013_blocked(database_url, "RESERVATION_LEDGER_INCOMPLETE")
        assert asyncio.run(_fetch_version(params)) == "0012"


@SKIP_NO_REDIS
def test_0013_blocks_with_attestation_mismatch(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with _scratch_at_revision("am", "0011") as (params, database_url):
        _arrange_evidence_phase(params)
        asyncio.run(_clear_fresh_flag(params))
        _upgrade_0012(params, database_url)
        _authenticate_via_finalize(capsys, params, database_url)

        asyncio.run(_corrupt_attestation_digest(params))
        _expect_0013_blocked(database_url, "ATTESTATION_MISMATCH")
        assert asyncio.run(_fetch_version(params)) == "0012"


@SKIP_NO_REDIS
def test_0013_blocks_with_not_started_residual(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with _scratch_at_revision("ns", "0011") as (params, database_url):
        _arrange_evidence_phase(params)
        asyncio.run(_clear_fresh_flag(params))
        _upgrade_0012(params, database_url)
        _authenticate_via_finalize(capsys, params, database_url)

        asyncio.run(_seed_not_started_parent(params))
        _expect_0013_blocked(database_url, "REPLY_NOT_STARTED_RESIDUAL")
        assert asyncio.run(_fetch_version(params)) == "0012"


@SKIP_NO_REDIS
def test_0013_blocks_with_payload_not_minimized(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with _scratch_at_revision("mn", "0011") as (params, database_url):
        _arrange_evidence_phase(params)
        asyncio.run(_clear_fresh_flag(params))
        _upgrade_0012(params, database_url)
        _authenticate_via_finalize(capsys, params, database_url)

        asyncio.run(_seed_minimization_violation_parent(params))
        _expect_0013_blocked(database_url, "REPLY_PAYLOAD_NOT_MINIMIZED")
        assert asyncio.run(_fetch_version(params)) == "0012"


@SKIP_NO_REDIS
def test_0013_blocks_with_reply_lease_residual(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with _scratch_at_revision("lr", "0011") as (params, database_url):
        _arrange_evidence_phase(params)
        asyncio.run(_clear_fresh_flag(params))
        _upgrade_0012(params, database_url)
        _authenticate_via_finalize(capsys, params, database_url)

        asyncio.run(_seed_lease_residual_parent(params))
        _expect_0013_blocked(database_url, "REPLY_LEASE_RESIDUAL")
        assert asyncio.run(_fetch_version(params)) == "0012"


@SKIP_NO_REDIS
def test_0013_blocks_with_knowledge_source_conflict_nonzero(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with _scratch_at_revision("kc", "0011") as (params, database_url):
        _arrange_evidence_phase(params)
        asyncio.run(_clear_fresh_flag(params))
        _upgrade_0012(params, database_url)
        _authenticate_via_finalize(capsys, params, database_url)

        asyncio.run(_seed_conflict_hold_proposal(params))
        _expect_0013_blocked(database_url, "KNOWLEDGE_SOURCE_CONFLICT_NONZERO")
        assert asyncio.run(_fetch_version(params)) == "0012"


@SKIP_NO_REDIS
def test_0013_blocks_with_announcement_processing_residual(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with _scratch_at_revision("ap", "0011") as (params, database_url):
        _arrange_evidence_phase(params)
        asyncio.run(_clear_fresh_flag(params))
        _upgrade_0012(params, database_url)
        _authenticate_via_finalize(capsys, params, database_url)

        asyncio.run(_seed_processing_announcement(params))
        _expect_0013_blocked(database_url, "ANNOUNCEMENT_PROCESSING_RESIDUAL")
        assert asyncio.run(_fetch_version(params)) == "0012"


@SKIP_NO_REDIS
def test_0013_blocks_with_memory_job_lease_residual(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with _scratch_at_revision("mj", "0011") as (params, database_url):
        _arrange_evidence_phase(params)
        asyncio.run(_clear_fresh_flag(params))
        _upgrade_0012(params, database_url)
        _authenticate_via_finalize(capsys, params, database_url)

        asyncio.run(_seed_leasing_memory_job(params))
        _expect_0013_blocked(database_url, "MEMORY_JOB_LEASE_RESIDUAL")
        assert asyncio.run(_fetch_version(params)) == "0012"


# ---------------------------------------------------------------------------
# held-state 白名单 + 成功路径
# ---------------------------------------------------------------------------


async def _seed_held_states_before_0012(params: dict[str, Any]) -> None:
    """播种全部安全非零形态（0012 会转换其中应转换的部分）。"""
    connection = await _connect(params)
    try:
        # 受限群 voting 提案 → 0012 转 DEFERRED（held 白名单）
        await connection.execute(
            """
            INSERT INTO komari_custom_proposals (
                group_id, proposer_id, title, content, status,
                publication_key, required_votes
            ) VALUES (123456789, 888777666, 'deferred-canary', '正文',
                      'voting', 'pub-key-c13-deferred', 3)
            """
        )
        # PENDING_CONFIRMATION 回复行（含正文）——安全持有
        await connection.execute(
            """
            INSERT INTO komari_chat_reply_fulfillments (
                fulfillment_id, payload_hash, request_trace_id,
                trigger_message_id, trigger_user_id, group_id,
                bot_self_id, adapter_name, reply_target_message_id,
                reply_content, delivery_state, send_started_at
            ) VALUES (
                'fulfill-c13-held-pending', 'hash-held-pending',
                'trace-held-pending', 'trigger-held-pending', '888777666',
                '123456789', 'canary-self', 'test', 'target-held-pending',
                'held-canary-body-keep', 'PENDING_CONFIRMATION', NOW()
            )
            """
        )
        await connection.execute(
            """
            INSERT INTO komari_chat_reply_fulfillment_commitments (
                fulfillment_id, commitment_type, state, payload
            ) VALUES (
                'fulfill-c13-held-pending', 'favorability_adjustment',
                'PENDING', '{"user_id": "888777666", "delta": 1}'::jsonb
            )
            """
        )
        # cutover-cancelled 形态：NOT_STARTED + 双承诺（含 proactive 身份）
        await connection.execute(
            """
            INSERT INTO komari_chat_reply_fulfillments (
                fulfillment_id, payload_hash, request_trace_id,
                trigger_message_id, trigger_user_id, group_id,
                bot_self_id, adapter_name, reply_target_message_id,
                reply_content, delivery_state
            ) VALUES (
                'fulfill-c13-tombstone', 'hash-tombstone',
                'trace-tombstone', 'trigger-tombstone', '888777666',
                '123456789', 'canary-self', 'test', 'target-tombstone',
                'tombstone-body-to-clear', 'NOT_STARTED'
            )
            """
        )
        await connection.execute(
            """
            INSERT INTO komari_chat_reply_fulfillment_commitments (
                fulfillment_id, commitment_type, state, payload
            ) VALUES (
                'fulfill-c13-tombstone', 'proactive_reply_confirmation',
                'PENDING',
                '{"group_id": "123456789",
                  "reservation_id": "resv-c13-tombstone"}'::jsonb
            )
            """
        )
        await connection.execute(
            """
            INSERT INTO komari_chat_reply_fulfillment_commitments (
                fulfillment_id, commitment_type, state, payload
            ) VALUES (
                'fulfill-c13-tombstone', 'favorability_adjustment',
                'PENDING',
                '{"user_id": "888777666", "delta": 2}'::jsonb
            )
            """
        )
        # DELIVERED 行带未完成 commitment——安全持有
        await connection.execute(
            """
            INSERT INTO komari_chat_reply_fulfillments (
                fulfillment_id, payload_hash, request_trace_id,
                trigger_message_id, trigger_user_id, group_id,
                bot_self_id, adapter_name, reply_target_message_id,
                reply_content, delivery_state,
                send_started_at, delivered_at
            ) VALUES (
                'fulfill-c13-delivered-open', 'hash-delivered-open',
                'trace-delivered-open', 'trigger-delivered-open',
                '888777666', '123456789', 'canary-self', 'test',
                'target-delivered-open', 'delivered-open-body',
                'DELIVERED', NOW(), NOW()
            )
            """
        )
        await connection.execute(
            """
            INSERT INTO komari_chat_reply_fulfillment_commitments (
                fulfillment_id, commitment_type, state, payload
            ) VALUES (
                'fulfill-c13-delivered-open', 'assistant_reply_history',
                'PENDING', '{"note": "open"}'::jsonb
            )
            """
        )
        # 未完成 memory job / processing 公告（0012 哨兵化）
        await connection.execute(
            """
            INSERT INTO komari_memory_jobs (
                job_name, run_date, owner_token, lease_until, stage
            ) VALUES ('forgetting_c13_held', CURRENT_DATE, 'owner-c13-held',
                      NOW() + INTERVAL '1 hour', 'claimed')
            """
        )
        await connection.execute(
            """
            INSERT INTO komari_announcement_dispatches (
                request_id, payload_hash, status
            ) VALUES ('req-c13-held-processing', 'hash-c13-held',
                      'processing')
            """
        )
    finally:
        await connection.close()


async def _seed_dead_letter_key() -> None:
    """既有 conversation dead-letter 键（finalize 时被 quarantine，不阻断）。"""
    import redis.asyncio as aioredis

    async def _run() -> None:
        client = aioredis.from_url(_redis_test_url(), decode_responses=True)
        try:
            keys = [key async for key in client.scan_iter("komari_memory:*")]
            if keys:
                await client.delete(*keys)
            await client.set(
                "komari_memory:buffer:processing_dead:123456789:tokdead",
                f"{CANARY_BODY_TOKEN}-dead-letter",
            )
        finally:
            await client.aclose()

    # 基线缺陷修正（已获验收方追认）：本 helper 由外层 asyncio.run
    # 拉起，内部再 asyncio.run 必然重入报错；改为直接 await。
    await _run()


async def _cleanup_test_redis_keys() -> None:
    """清理测试逻辑库中本前缀族键（含 quarantine/dormancy 镜像）。"""
    import redis.asyncio as aioredis

    async def _run() -> None:
        client = aioredis.from_url(_redis_test_url(), decode_responses=True)
        try:
            keys: list[str] = []
            for prefix in _CLEANUP_KEY_PREFIXES:
                keys.extend([key async for key in client.scan_iter(f"{prefix}*")])
            if keys:
                await client.delete(*keys)
        finally:
            await client.aclose()

    # 基线缺陷修正（已获验收方追认）：本 helper 由外层 asyncio.run
    # 拉起，内部再 asyncio.run 必然重入报错；改为直接 await。
    await _run()


@SKIP_NO_REDIS
def test_0013_succeeds_with_held_states_and_cleans_migration_only_tables(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """白名单全绿：0013 成功、migration-only 表删净、tombstone 收尾正确。"""
    with _scratch_at_revision("hs", "0011") as (params, database_url):
        seed_legacy_configs(
            params,
            [("komari_search", {"groups": [str(CANARY_GROUP_A)]})],
        )
        asyncio.run(_seed_held_states_before_0012(params))
        _arrange_evidence_phase(params)
        asyncio.run(_clear_fresh_flag(params))
        _upgrade_0012(params, database_url)

        asyncio.run(_seed_dead_letter_key())
        try:
            _authenticate_via_finalize(capsys, params, database_url)

            # 基线缺陷修正（已获验收方追认）：alembic upgrade 精确停在
            # 目标 revision，版本到 0017 必须走 head；与下方既有断言一致。
            result = run_bootstrap(database_url, "upgrade", "head")
            assert result.returncode == 0, result.stderr
            assert asyncio.run(_fetch_version(params)) == "0017"
        finally:
            asyncio.run(_cleanup_test_redis_keys())

        # migration-only 表物理删除
        for table in (
            "komari_group_admission_gate",
            "komari_reply_reservation_cleanup_ledger",
            "komari_admission_quarantine_ledger",
            "komari_plugin_configs",
            "komari_chat_reply_commit_outbox",
        ):
            assert not asyncio.run(_table_exists(params, table)), table

        # 六列 RENAME 保持轮 A 语义
        chat_columns = asyncio.run(_column_names(params, "komari_chat_config"))
        assert "reply_fulfillment_worker_interval_seconds" in chat_columns
        assert "reply_fulfillment_retry_max_seconds" in chat_columns
        assert "reply_commit_worker_interval_seconds" not in chat_columns

        # tombstone：parent 保留无正文；proactive 子行删除、其余子行保留
        # （基线缺陷修正：asyncpg 协程须在事件循环内 await，故把连接+
        # 查询+关闭整体包进 async 快照再以 asyncio.run 驱动；SQL 不变）
        async def _tombstone_snapshot() -> tuple[Any, list[str]]:
            connection = await _connect(params)
            try:
                tombstone = await connection.fetchrow(
                    "SELECT delivery_state, reply_content, not_delivered_at"
                    " FROM komari_chat_reply_fulfillments"
                    " WHERE fulfillment_id = 'fulfill-c13-tombstone'"
                )
                child_rows = await connection.fetch(
                    "SELECT commitment_type FROM"
                    " komari_chat_reply_fulfillment_commitments"
                    " WHERE fulfillment_id = 'fulfill-c13-tombstone'"
                )
            finally:
                await connection.close()
            return tombstone, [
                str(row["commitment_type"]) for row in child_rows
            ]

        tombstone, child_types = asyncio.run(_tombstone_snapshot())
        assert tombstone is not None
        assert tombstone["delivery_state"] == "NOT_DELIVERED"
        assert tombstone["reply_content"] is None
        assert tombstone["not_delivered_at"] is not None
        assert "proactive_reply_confirmation" not in child_types
        assert "favorability_adjustment" in child_types

        # held-state 全部幸存
        async def _held_state_snapshot() -> tuple[Any, Any, Any]:
            connection = await _connect(params)
            try:
                proposal = await connection.fetchrow(
                    "SELECT status, admission_state FROM komari_custom_proposals"
                    " WHERE publication_key = 'pub-key-c13-deferred'"
                )
                pending_parent = await connection.fetchrow(
                    "SELECT delivery_state, reply_content FROM"
                    " komari_chat_reply_fulfillments"
                    " WHERE fulfillment_id = 'fulfill-c13-held-pending'"
                )
                delivered_open_children = await connection.fetchval(
                    "SELECT COUNT(*) FROM"
                    " komari_chat_reply_fulfillment_commitments"
                    " WHERE fulfillment_id = 'fulfill-c13-delivered-open'"
                    " AND state <> 'COMPLETED'"
                )
            finally:
                await connection.close()
            return proposal, pending_parent, delivered_open_children

        proposal, pending_parent, delivered_open_children = asyncio.run(
            _held_state_snapshot()
        )
        assert proposal is not None
        assert proposal["status"] == "voting"
        assert proposal["admission_state"] == "DEFERRED"
        assert pending_parent is not None
        assert pending_parent["delivery_state"] == "PENDING_CONFIRMATION"
        assert pending_parent["reply_content"] == "held-canary-body-keep"
        assert int(delivered_open_children or 0) >= 1

        # 基线缺陷修正（已获验收方追认）：downgrade 目标须低于 0013 才会
        # 调用 0013.downgrade() 触发 forward-only 拒绝；停在 0013 属
        # no-op，永远出不了 IRREVERSIBLE 标记。
        downgrade = run_bootstrap(database_url, "downgrade", "0009")
        assert downgrade.returncode != 0
        assert "0013_TSK232_IS_IRREVERSIBLE" in (
            f"{downgrade.stdout}\n{downgrade.stderr}"
        )


async def _fetch_single(params: dict[str, Any], column: str, sql: str) -> Any:
    del sql
    connection = await _connect(params)
    try:
        return await connection.fetchval(f"SELECT {column} FROM {_GATE_TABLE}")
    finally:
        await connection.close()


def test_0013_fresh_empty_database_passes_head_without_redis() -> None:
    """空库 fresh 豁免：单次 upgrade head 不经 Redis 认证直通（纯 PG 成功路径）。

    与生产/prestart 同构：全新空库在同一次 upgrade 调用内走完
    0001→0017（fresh 标记、缺省策略播种、零数据自动推进与 fresh 豁免
    全部生效），绝不要求 operator 先跑 Redis 认证。
    """
    params = asyncio.run(recreate_scratch_database("_tsk232b1c13fe"))
    database_url = scratch_url(str(params["database"]))
    try:
        result = run_bootstrap(database_url, "upgrade", "head")
        assert result.returncode == 0, (
            f"{result.stdout}\n{result.stderr}"
        )
        assert asyncio.run(_fetch_version(params)) == "0017"
    finally:
        asyncio.run(drop_scratch_database(str(params["database"])))
