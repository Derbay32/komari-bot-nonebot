"""TSK-232 轮 B —— 0012 backfill 各业务模块转换矩阵红基线（门控 DB）。

锁定 0012 数据面在 reply 之外的业务转换契约（单个 PG 事务 + advisory
xact lock 同键；失败整事务回滚、revision 停留 0011 可重跑）：

- proposal：按当前 policy 对每行 group_id adjudicate（restricted=非
  BUSINESS）。restricted 非终态行（publishing/voting/approving）转
  ``admission_state='DEFERRED'`` 并落 revision/at、双 token 清空；
  admitted 行保持 ACTIVE 且 token 不动。approving 特判：knowledge 源
  身份一致 → 直接 ``approved`` + ``approved_at``（fact finalization）；
  源身份冲突 → ``execution_hold_code='KNOWLEDGE_SOURCE_CONFLICT'``；
  源缺失且 restricted → DEFERRED。终态行一律不动。
- memory jobs：``stage<>'completed'`` 行哨兵化 ``owner_token=''`` +
  ``lease_until=NOW()``；completed 行不动。
- announcement：processing → ``reconciliation_required`` +
  ``reconciliation_code='CUTOVER_PROGRESS_UNKNOWN'``，owner/lease 清空。
- gate：operator 路径 CAS 自 REDIS_EVIDENCE_CAPTURED →
  POSTGRES_BACKFILLED；业务存量全零时空库自动直通；相位不足拒绝
  （PHASE_OUT_OF_ORDER）；policy 缺失失败后 revision 保持 0011，
  修复重跑成功。

隔离库 = 门控库名 + ``_tsk232b1mod``（中途停链重放 0011→0012），用例
结束 DROP；直连 SQL 仅用于前置编排与结果断言。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import asyncpg

from tests.cutover.support import (
    CANARY_GROUP_A,
    CANARY_GROUP_C,
    DEFAULT_SEEDED_POLICY,
    DEFAULT_SEEDED_POLICY_FINGERPRINT,
    SKIP_NO_POSTGRES,
    VALID_POLICY,
    VALID_POLICY_FINGERPRINT,
    cutover_scratch_database,
    drop_scratch_database,
    fetch_admission_config,
    fetch_gate_row,
    overwrite_admission_policy,
    recreate_scratch_database,
    run_bootstrap,
    scratch_url,
    stage_gate_phase,
)

pytestmark = [SKIP_NO_POSTGRES]

if TYPE_CHECKING:
    from collections.abc import Iterator


# ---------------------------------------------------------------------------
# 隔离库与前置编排 helper
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _scratch_at_revision(
    revision: str,
) -> Iterator[tuple[dict[str, Any], str]]:
    """重建 ``_tsk232b1mod`` 隔离库并 upgrade 到指定 revision。"""
    params = asyncio.run(recreate_scratch_database("_tsk232b1mod"))
    database_url = scratch_url(str(params["database"]))
    try:
        result = run_bootstrap(database_url, "upgrade", revision)
        assert result.returncode == 0, result.stderr
        yield params, database_url
    finally:
        asyncio.run(drop_scratch_database(str(params["database"])))


async def _connect(params: dict[str, Any]) -> Any:
    return await asyncpg.connect(**params)


async def _fetch_version(params: dict[str, Any]) -> str:
    connection = await _connect(params)
    try:
        value = await connection.fetchval("SELECT version_num FROM alembic_version")
    finally:
        await connection.close()
    return str(value)


def _arrange_policy_and_evidence_phase(params: dict[str, Any]) -> None:
    """operator 路径前置：VALID_POLICY 内容 + gate 相位 REDIS_EVIDENCE_CAPTURED。"""
    overwrite_admission_policy(params, VALID_POLICY)
    stage_gate_phase(
        params,
        phase="REDIS_EVIDENCE_CAPTURED",
        policy_revision=1,
        policy_fingerprint=VALID_POLICY_FINGERPRINT,
    )


async def _seed_proposal_matrix(
    params: dict[str, Any],
) -> dict[str, int]:
    """播种提案/知识源矩阵并返回语义键 → proposal id。

    - ``restricted_publishing`` / ``restricted_voting``：受限群非终态行；
    - ``admitted_voting``：获准群投票行（token 保留对照）；
    - ``approving_consistent``：受限群 approving，knowledge 源内容一致；
    - ``approving_conflict``：获准群 approving，knowledge 源内容漂移；
    - ``approving_missing_source``：受限群 approving，knowledge_id 悬空；
    - ``terminal_approved`` / ``terminal_failed``：终态对照行。
    """
    knowledge_ids: dict[str, int] = {}
    proposal_ids: dict[str, int] = {}
    connection = await _connect(params)
    try:
        for key, category, content in (
            ("consistent", "general", "knowledge-canary-consistent-body"),
            ("conflict", "general", "knowledge-canary-drifted-body"),
        ):
            knowledge_ids[key] = int(
                await connection.fetchval(
                    """
                    INSERT INTO komari_knowledge (category, content)
                    VALUES ($1, $2) RETURNING id
                    """,
                    category,
                    content,
                )
            )
        matrix: list[tuple[str, int, str, str | None, str | None]] = [
            (
                "restricted_publishing",
                CANARY_GROUP_A,
                "publishing",
                "pub-token-restricted",
                None,
            ),
            (
                "restricted_voting",
                CANARY_GROUP_A,
                "voting",
                "approval-token-voting",
                None,
            ),
            (
                "admitted_voting",
                CANARY_GROUP_C,
                "voting",
                "approval-token-admitted",
                None,
            ),
            (
                "approving_consistent",
                CANARY_GROUP_A,
                "approving",
                None,
                str(knowledge_ids["consistent"]),
            ),
            (
                "approving_conflict",
                CANARY_GROUP_C,
                "approving",
                None,
                str(knowledge_ids["conflict"]),
            ),
            ("approving_missing_source", CANARY_GROUP_A, "approving", None, "999999"),
            (
                "terminal_approved",
                CANARY_GROUP_A,
                "approved",
                None,
                str(knowledge_ids["consistent"]),
            ),
            ("terminal_failed", CANARY_GROUP_C, "failed", None, None),
        ]
        for index, (key, group_id, status, approval_token, kid) in enumerate(matrix, 1):
            body = (
                "knowledge-canary-consistent-body"
                if key == "approving_consistent"
                else f"proposal-canary-body-{index}"
            )
            proposal_ids[key] = int(
                await connection.fetchval(
                    """
                    INSERT INTO komari_custom_proposals (
                        group_id, proposer_id, title, content, status,
                        publication_key, publication_token, approval_token,
                        required_votes, approved_at, knowledge_id
                    ) VALUES ($1, 888777666, $2, $3, $4, $5, $6, $7, 3, $8, $9)
                    RETURNING id
                    """,
                    group_id,
                    f"proposal-canary-title-{index}",
                    body,
                    status,
                    f"pub-key-b1mod-{index}",
                    f"pub-token-{index}" if status == "publishing" else None,
                    approval_token,
                    datetime(2026, 8, 1, tzinfo=UTC) if status == "approved" else None,
                    int(kid) if kid is not None else None,
                )
            )
        return proposal_ids
    finally:
        await connection.close()


async def _fetch_proposal(
    params: dict[str, Any],
    proposal_id: int,
) -> dict[str, Any]:
    connection = await _connect(params)
    try:
        row = await connection.fetchrow(
            """
            SELECT status, admission_state, execution_hold_code,
                   admission_deferred_revision, admission_deferred_at,
                   publication_token, approval_token, approved_at
            FROM komari_custom_proposals WHERE id = $1
            """,
            proposal_id,
        )
    finally:
        await connection.close()
    assert row is not None, f"提案缺失: {proposal_id}"
    return dict(row)


async def _seed_memory_and_announcements(params: dict[str, Any]) -> None:
    """播种 memory job（未完成/已完成）与公告（processing/完成）对照行。"""
    connection = await _connect(params)
    try:
        await connection.execute(
            """
            INSERT INTO komari_memory_jobs (
                job_name, run_date, owner_token, lease_until, stage
            ) VALUES ('forgetting_canary_open', CURRENT_DATE,
                      'owner-canary-open', NOW() + INTERVAL '1 hour',
                      'claimed')
            """
        )
        await connection.execute(
            """
            INSERT INTO komari_memory_jobs (
                job_name, run_date, owner_token, lease_until, stage,
                completed_at
            ) VALUES ('forgetting_canary_done', CURRENT_DATE,
                      'owner-canary-done', NOW() + INTERVAL '1 hour',
                      'completed', NOW())
            """
        )
        await connection.execute(
            """
            INSERT INTO komari_announcement_dispatches (
                request_id, payload_hash, status, owner_token,
                lease_expires_at
            ) VALUES ('req-canary-proc-1', 'hash-proc-1', 'processing',
                      'owner-canary-proc', NOW() + INTERVAL '10 minutes')
            """
        )
        await connection.execute(
            """
            INSERT INTO komari_announcement_dispatches (
                request_id, payload_hash, status
            ) VALUES ('req-canary-completed-1', 'hash-completed-1',
                      'completed')
            """
        )
    finally:
        await connection.close()


async def _fetch_memory_jobs(params: dict[str, Any]) -> list[dict[str, Any]]:
    connection = await _connect(params)
    try:
        rows = await connection.fetch(
            "SELECT job_name, owner_token, lease_until, stage"
            " FROM komari_memory_jobs ORDER BY job_name"
        )
    finally:
        await connection.close()
    return [dict(row) for row in rows]


async def _fetch_announcements(
    params: dict[str, Any],
) -> list[dict[str, Any]]:
    connection = await _connect(params)
    try:
        rows = await connection.fetch(
            "SELECT request_id, status, reconciliation_code, owner_token,"
            " lease_expires_at FROM komari_announcement_dispatches"
            " ORDER BY request_id"
        )
    finally:
        await connection.close()
    return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# proposal 转换矩阵
# ---------------------------------------------------------------------------


def test_0012_proposal_matrix_defers_and_finalizes() -> None:
    """restricted 非终态 DEFERRED、admitted ACTIVE、approving 特判三向收敛。"""
    with _scratch_at_revision("0011") as (params, database_url):
        ids = asyncio.run(_seed_proposal_matrix(params))
        _arrange_policy_and_evidence_phase(params)

        result = run_bootstrap(database_url, "upgrade", "0012")
        assert result.returncode == 0, result.stderr

        # 受限群 publishing/voting：DEFERRED + revision/at + 双 token 清空
        for key in ("restricted_publishing", "restricted_voting"):
            row = asyncio.run(_fetch_proposal(params, ids[key]))
            assert row["status"] == (
                "publishing" if key.endswith("publishing") else "voting"
            )
            assert row["admission_state"] == "DEFERRED"
            assert row["execution_hold_code"] is None
            assert row["admission_deferred_revision"] == 1
            assert row["admission_deferred_at"] is not None
            assert row["publication_token"] is None
            assert row["approval_token"] is None

        # 获准群 voting：保持 ACTIVE、token 不动
        admitted = asyncio.run(_fetch_proposal(params, ids["admitted_voting"]))
        assert admitted["status"] == "voting"
        assert admitted["admission_state"] == "ACTIVE"
        assert admitted["execution_hold_code"] is None
        assert admitted["approval_token"] == "approval-token-admitted"

        # approving + 知识源一致：fact finalization 直接收敛为 approved
        consistent = asyncio.run(_fetch_proposal(params, ids["approving_consistent"]))
        assert consistent["status"] == "approved"
        assert consistent["approved_at"] is not None

        # approving + 知识源身份冲突：closed hold，不阻断迁移本身
        conflict = asyncio.run(_fetch_proposal(params, ids["approving_conflict"]))
        assert conflict["execution_hold_code"] == "KNOWLEDGE_SOURCE_CONFLICT"

        # approving + 源缺失且受限：DEFERRED 休眠
        missing = asyncio.run(_fetch_proposal(params, ids["approving_missing_source"]))
        assert missing["status"] == "approving"
        assert missing["admission_state"] == "DEFERRED"
        assert missing["publication_token"] is None

        # 终态行不动
        approved = asyncio.run(_fetch_proposal(params, ids["terminal_approved"]))
        assert approved["status"] == "approved"
        assert approved["approved_at"] is not None
        failed = asyncio.run(_fetch_proposal(params, ids["terminal_failed"]))
        assert failed["status"] == "failed"
        assert failed["admission_state"] == "ACTIVE"

        gate = fetch_gate_row(params)
        assert gate is not None
        assert gate["phase"] == "POSTGRES_BACKFILLED"


# ---------------------------------------------------------------------------
# memory jobs 与 announcement 哨兵
# ---------------------------------------------------------------------------


def test_0012_memory_job_and_announcement_sentinels() -> None:
    """memory 未完成行哨兵化；announcement processing 转 reconciliation。"""
    with _scratch_at_revision("0011") as (params, database_url):
        asyncio.run(_seed_memory_and_announcements(params))
        _arrange_policy_and_evidence_phase(params)

        result = run_bootstrap(database_url, "upgrade", "0012")
        assert result.returncode == 0, result.stderr

        jobs = {
            str(row["job_name"]): row for row in asyncio.run(_fetch_memory_jobs(params))
        }
        open_job = jobs["forgetting_canary_open"]
        assert open_job["stage"] == "claimed"
        assert open_job["owner_token"] == ""
        assert open_job["lease_until"] <= datetime.now(UTC)
        done_job = jobs["forgetting_canary_done"]
        assert done_job["owner_token"] == "owner-canary-done"

        announcements = {
            str(row["request_id"]): row
            for row in asyncio.run(_fetch_announcements(params))
        }
        processing = announcements["req-canary-proc-1"]
        assert processing["status"] == "reconciliation_required"
        assert processing["reconciliation_code"] == "CUTOVER_PROGRESS_UNKNOWN"
        assert processing["owner_token"] is None
        assert processing["lease_expires_at"] is None
        completed = announcements["req-canary-completed-1"]
        assert completed["status"] == "completed"


# ---------------------------------------------------------------------------
# gate 相位前置 / 空库直通 / policy 缺失崩溃语义
# ---------------------------------------------------------------------------


async def _seed_single_proposal(params: dict[str, Any]) -> None:
    connection = await _connect(params)
    try:
        await connection.execute(
            """
            INSERT INTO komari_custom_proposals (
                group_id, proposer_id, title, content, status,
                publication_key, required_votes
            ) VALUES (123456789, 888777666, 'phase-canary', '正文', 'voting',
                      'pub-key-phase-canary', 3)
            """
        )
    finally:
        await connection.close()


async def _delete_admission_config(params: dict[str, Any]) -> None:
    connection = await _connect(params)
    try:
        await connection.execute("DELETE FROM komari_group_admission_config")
    finally:
        await connection.close()


async def _insert_default_policy_row(params: dict[str, Any]) -> None:
    connection = await _connect(params)
    try:
        await connection.execute(
            """
            INSERT INTO komari_group_admission_config (
                id, revision, updated_at, policy
            ) VALUES (1, 1, NOW(), CAST($1 AS JSONB))
            """,
            json.dumps(DEFAULT_SEEDED_POLICY),
        )
    finally:
        await connection.close()


def test_0012_rejects_operator_upgrade_before_evidence_phase() -> None:
    """业务存量非零且 phase=POLICY_PREPARED：拒绝 PHASE_OUT_OF_ORDER、停在 0011。"""
    with _scratch_at_revision("0011") as (params, database_url):
        asyncio.run(_seed_single_proposal(params))
        overwrite_admission_policy(params, VALID_POLICY)
        stage_gate_phase(
            params,
            phase="POLICY_PREPARED",
            policy_revision=1,
            policy_fingerprint=VALID_POLICY_FINGERPRINT,
        )

        result = run_bootstrap(database_url, "upgrade", "0012")
        assert result.returncode != 0
        output = f"{result.stdout}\n{result.stderr}"
        assert "PHASE_OUT_OF_ORDER" in output
        assert asyncio.run(_fetch_version(params)) == "0011"

        # operator 补齐 evidence 相位后同一命令重跑成功
        _arrange_policy_and_evidence_phase(params)
        retry = run_bootstrap(database_url, "upgrade", "0012")
        assert retry.returncode == 0, retry.stderr
        gate = fetch_gate_row(params)
        assert gate is not None
        assert gate["phase"] == "POSTGRES_BACKFILLED"


def test_empty_database_upgrade_advances_gate_to_postgres_backfilled() -> None:
    """空业务库直通：无 operator 相位也自动推进 POSTGRES_BACKFILLED。"""
    with _scratch_at_revision("0011") as (params, database_url):
        config = fetch_admission_config(params)
        assert config is not None
        assert config["revision"] == 1

        result = run_bootstrap(database_url, "upgrade", "0012")
        assert result.returncode == 0, result.stderr

        assert asyncio.run(_fetch_version(params)) == "0012"
        gate = fetch_gate_row(params)
        assert gate is not None
        assert gate["phase"] == "POSTGRES_BACKFILLED"
        assert gate["is_fresh"] is True


def test_full_head_upgrade_on_empty_database_succeeds() -> None:
    """全新空库一次 upgrade head 必须成功收敛到 0020（fresh 自动放行链）。"""
    with cutover_scratch_database("mod", revision="head") as (
        params,
        _database_url,
    ):
        assert asyncio.run(_fetch_version(params)) == "0020"


def test_policy_missing_failure_keeps_0011_then_repairs() -> None:
    """policy 缺失被 barrier 拒绝：revision 停 0011；修复后重跑成功。"""
    with _scratch_at_revision("0011") as (params, database_url):
        asyncio.run(_seed_single_proposal(params))
        asyncio.run(_delete_admission_config(params))

        result = run_bootstrap(database_url, "upgrade", "0012")
        assert result.returncode != 0
        output = f"{result.stdout}\n{result.stderr}"
        assert "PHASE_OUT_OF_ORDER" in output
        assert asyncio.run(_fetch_version(params)) == "0011"

        asyncio.run(_insert_default_policy_row(params))
        stage_gate_phase(
            params,
            phase="REDIS_EVIDENCE_CAPTURED",
            policy_revision=1,
            policy_fingerprint=DEFAULT_SEEDED_POLICY_FINGERPRINT,
        )
        retry = run_bootstrap(database_url, "upgrade", "0012")
        assert retry.returncode == 0, retry.stderr
        assert asyncio.run(_fetch_version(params)) == "0012"
