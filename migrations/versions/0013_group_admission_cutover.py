"""群聊准入 coordinated contract：终检矩阵通过后单事务物理收尾。

迁移 ID: 0013
父迁移: 0012

本 revision 是 expand-contract 序列的 contract 终点，在同一停机事务内：

1. 准入终检矩阵（任何命中即 fail-fast，错误只含 closed code 与聚合
   count，绝不输出群号、用户号、名单成员、正文或任何动态身份）：
   - ``POLICY_MISSING``　　　　　　　　统一准入策略行缺失；
   - ``POLICY_FINGERPRINT_DRIFT``　　　策略内容与 gate 指纹不符；
   - ``GATE_PHASE_INVALID``　　　　　　相位既非 ``REDIS_FINALIZED``
     且不满足 fresh 豁免（is_fresh 且双 ledger 全空且 Redis 摘要为空）；
   - ``RESERVATION_LEDGER_INCOMPLETE`` 清理底册存在未完成条目；
   - ``ATTESTATION_MISMATCH``　　　　　quarantine 底册聚合摘要重算与
     ``gate.redis_finalizer_digest`` 不符（与 finalize-redis 同一公式）；
   - ``REPLY_NOT_STARTED_RESIDUAL``　　仍处 NOT_STARTED 的回复父行；
   - ``REPLY_PAYLOAD_NOT_MINIMIZED``　 NOT_DELIVERED 父行残留正文；
   - ``REPLY_LEASE_RESIDUAL``　　　　　回复父行残留租约身份；
   - ``KNOWLEDGE_SOURCE_CONFLICT_NONZERO`` 冲突挂起提案未清零；
   - ``ANNOUNCEMENT_PROCESSING_RESIDUAL`` 公告仍处 processing；
   - ``MEMORY_JOB_LEASE_RESIDUAL``　　 memory job 租约未归还。
   held-state 安全非零形态（DEFERRED 提案、PENDING_CONFIRMATION 回复、
   DELIVERED 未完成承诺、reconciliation_required 公告、哨兵化 memory
   job）一律不阻断。
2. 回复履约门禁（沿袭轮 A 语义）：每条旧行必须已存在父镜像
   （``missing_backfill_count``），非终态行的子项集合必须完整且无重复
   （``commitment_mismatch_count``）；错误只报告聚合 count，绝不输出
   ``minimum_fulfillment_id`` 或正文。
3. tombstone 收尾：已清证据父行（``idempotency_evidence_cleared_at``）
   的 ``proactive_reply_confirmation`` 子承诺删除，其余子行保留；
   parent 行保留无正文墓碑。
4. 六个旧配置列一对一 ``RENAME COLUMN`` 为 ``reply_fulfillment_*``，
   并新增 ``reply_fulfillment_retry_max_seconds``（``DEFAULT 3600``）。
5. migration-only 物理删除：旧宽 outbox、legacy ``komari_plugin_configs``、
   cutover gate 与双 ledger（cleanup/quarantine）整体落下。

新父子表与强类型配置表一律保留。本 revision 自包含：不导入
komari_bot，不创建任何双读、双写、兼容别名或运行时 fallback；
downgrade 明确拒绝回退（``0013_TSK232_IS_IRREVERSIBLE``）。
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, NoReturn

from alembic import op
from sqlalchemy import text

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy import Connection


revision: str = "0013"
down_revision: str | Sequence[str] | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_GATE_TABLE = "komari_group_admission_gate"
_CONFIG_TABLE = "komari_group_admission_config"
_CLEANUP_LEDGER_TABLE = "komari_reply_reservation_cleanup_ledger"
_QUARANTINE_LEDGER_TABLE = "komari_admission_quarantine_ledger"

_PHASE_FINALIZED = "REDIS_FINALIZED"


def _fail(closed_code: str, count: int) -> NoReturn:
    """终检命中即失败：错误面只含 closed code 与聚合 count。"""
    msg = f"{closed_code} count={count}"
    raise RuntimeError(msg)


def _scalar_count(connection: Connection, sql: str) -> int:
    """执行单值 COUNT 投影并返回非负整数。"""
    value = connection.execute(text(sql)).scalar_one_or_none()
    return int(value or 0)


def _load_gate_row(connection: Connection) -> dict[str, object] | None:
    """读取 gate 单行的终检所需投影。"""
    row = connection.execute(
        text(
            f"SELECT phase, is_fresh, policy_fingerprint,"
            f" redis_evidence_digest, redis_finalizer_digest"
            f" FROM {_GATE_TABLE} WHERE id = 1"
        )
    ).first()
    if row is None:
        return None
    return {
        "phase": row[0],
        "is_fresh": bool(row[1]),
        "policy_fingerprint": row[2],
        "redis_evidence_digest": row[3],
        "redis_finalizer_digest": row[4],
    }


def _canonical_policy_digest(policy: object) -> str:
    """统一准入策略内容的 canonical JSON SHA-256（与 CLI oracle 同范式）。

    驱动对 JSONB 列可能返回 dict（SQLAlchemy asyncpg 编解码）或 JSON
    文本（裸 asyncpg），两种 wire 形态在此归一。
    """
    document = policy if isinstance(policy, dict) else json.loads(str(policy))
    canonical = json.dumps(document, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _require_policy_present(connection: Connection) -> object:
    """统一准入策略行必须存在，返回其 JSONB 内容（dict 或文本）。"""
    policy = connection.execute(
        text(f"SELECT policy FROM {_CONFIG_TABLE} WHERE id = 1")
    ).scalar_one_or_none()
    if policy is None:
        _fail("POLICY_MISSING", 1)
    return policy


def _quarantine_attestation(connection: Connection) -> tuple[str, int]:
    """按 entry_id 升序重算 quarantine 底册聚合摘要（与 finalize 同公式）。"""
    rows = connection.execute(
        text(
            f"SELECT key_family, payload_digest FROM {_QUARANTINE_LEDGER_TABLE}"
            f" ORDER BY entry_id"
        )
    ).all()
    hasher = hashlib.sha256()
    for key_family, payload_digest in rows:
        hasher.update(f"{key_family}\x1f{payload_digest}\n".encode())
    return hasher.hexdigest(), len(rows)


def _business_residual_count(connection: Connection) -> int:
    """聚合四族业务存量的未收敛行数（与 0012 同口径，两侧同步维护）。

    0013 执行时 0012 已完成转换：operator 库的 DEFERRED 提案 status 仍为
    非终态、计数非零；只有从未携带在途业务的库（fresh 或分步升级的
    空业务库）才可能全零。
    """
    total = 0
    total += int(
        connection.execute(
            text(
                "SELECT count(*) FROM komari_chat_reply_fulfillments"
                " WHERE delivery_state = 'NOT_STARTED'"
            )
        ).scalar_one()
    )
    total += int(
        connection.execute(
            text(
                "SELECT count(*) FROM komari_custom_proposals"
                " WHERE status IN ('publishing', 'voting', 'approving')"
            )
        ).scalar_one()
    )
    total += int(
        connection.execute(
            text(
                "SELECT count(*) FROM komari_announcement_dispatches"
                " WHERE status = 'processing'"
            )
        ).scalar_one()
    )
    total += int(
        connection.execute(
            text(
                "SELECT count(*) FROM komari_memory_jobs"
                " WHERE stage <> 'completed'"
            )
        ).scalar_one()
    )
    return total


def _require_phase_reachable(
    connection: Connection,
    gate: dict[str, object],
) -> bool:
    """相位必须为 REDIS_FINALIZED，或满足无存量豁免。

    豁免覆盖两类库：fresh 安装（is_fresh），以及从未进入 operator 流程
    且四族业务存量全零、双 ledger 与 evidence/finalizer 摘要全空的
    分步升级空业务库（0012 同口径直通后的镜像豁免；operator 路径必经
    capture-evidence，其 evidence 摘要非空，不可能落入本豁免）。

    返回是否走了豁免直通路径；豁免时调用方跳过其余全部验证。
    """
    if str(gate["phase"]) == _PHASE_FINALIZED:
        return False
    cleanup_total = _scalar_count(
        connection, f"SELECT COUNT(*) FROM {_CLEANUP_LEDGER_TABLE}"
    )
    _, quarantine_total = _quarantine_attestation(connection)
    fresh_exempt = (
        bool(gate["is_fresh"])
        and cleanup_total == 0
        and quarantine_total == 0
        and gate["redis_finalizer_digest"] is None
    )
    empty_exempt = (
        not fresh_exempt
        and gate["redis_evidence_digest"] is None
        and gate["redis_finalizer_digest"] is None
        and cleanup_total == 0
        and quarantine_total == 0
        and _business_residual_count(connection) == 0
    )
    if not (fresh_exempt or empty_exempt):
        _fail("GATE_PHASE_INVALID", 1)
    return True


def _require_no_residual(connection: Connection) -> None:
    """存量残留闭检：任一非零即阻断，错误只含 closed code 与聚合 count。"""
    residuals: tuple[tuple[str, str], ...] = (
        (
            "RESERVATION_LEDGER_INCOMPLETE",
            f"SELECT COUNT(*) FROM {_CLEANUP_LEDGER_TABLE}"
            " WHERE release_finished_at IS NULL",
        ),
        (
            "REPLY_NOT_STARTED_RESIDUAL",
            "SELECT COUNT(*) FROM komari_chat_reply_fulfillments"
            " WHERE delivery_state = 'NOT_STARTED'",
        ),
        (
            "REPLY_PAYLOAD_NOT_MINIMIZED",
            "SELECT COUNT(*) FROM komari_chat_reply_fulfillments"
            " WHERE delivery_state = 'NOT_DELIVERED'"
            " AND reply_content IS NOT NULL",
        ),
        (
            "REPLY_LEASE_RESIDUAL",
            "SELECT COUNT(*) FROM komari_chat_reply_fulfillments"
            " WHERE lease_owner IS NOT NULL OR lease_expires_at IS NOT NULL",
        ),
        (
            "KNOWLEDGE_SOURCE_CONFLICT_NONZERO",
            "SELECT COUNT(*) FROM komari_custom_proposals"
            " WHERE execution_hold_code = 'KNOWLEDGE_SOURCE_CONFLICT'",
        ),
        (
            "ANNOUNCEMENT_PROCESSING_RESIDUAL",
            "SELECT COUNT(*) FROM komari_announcement_dispatches"
            " WHERE status = 'processing'",
        ),
        (
            "MEMORY_JOB_LEASE_RESIDUAL",
            "SELECT COUNT(*) FROM komari_memory_jobs"
            " WHERE owner_token <> '' AND lease_until > NOW()",
        ),
    )
    for closed_code, sql in residuals:
        count = _scalar_count(connection, sql)
        if count:
            _fail(closed_code, count)


def _run_admission_terminal_checks(connection: Connection) -> None:
    """准入终检矩阵：相位/豁免 → policy → 认证 → 残留闭检。"""
    gate = _load_gate_row(connection)
    if gate is None:
        # gate 行缺失视同相位机不可追溯，按相位无效阻断。
        _fail("GATE_PHASE_INVALID", 1)
    fresh_exempt = _require_phase_reachable(connection, gate)
    if fresh_exempt:
        # fresh 豁免直通：空库自动放行链不经 policy/Redis 认证。
        return
    policy = _require_policy_present(connection)
    stored_fingerprint = gate["policy_fingerprint"]
    if (
        not isinstance(stored_fingerprint, str)
        or _canonical_policy_digest(policy) != stored_fingerprint
    ):
        _fail("POLICY_FINGERPRINT_DRIFT", 1)
    recomputed_digest, _ = _quarantine_attestation(connection)
    stored_digest = gate["redis_finalizer_digest"]
    if not isinstance(stored_digest, str) or stored_digest != recomputed_digest:
        _fail("ATTESTATION_MISMATCH", 1)
    _require_no_residual(connection)


def _lock_legacy_table(connection: Connection) -> None:
    """锁住旧宽表：先 ACCESS EXCLUSIVE 表锁，再行级 FOR UPDATE 快照。

    表锁阻止门禁校验与 DROP 之间任何并发写（含新插入），行级快照
    保证后续数量校验读到一致视图；两者在同一事务内生效。
    """
    connection.execute(
        text("LOCK TABLE komari_chat_reply_commit_outbox IN ACCESS EXCLUSIVE MODE")
    )
    connection.execute(
        text(
            "SELECT operation_id "
            "FROM komari_chat_reply_commit_outbox "
            "ORDER BY operation_id "
            "FOR UPDATE"
        )
    ).all()


def _require_no_conflict(connection: Connection, *, where: str, label: str) -> None:
    """旧表条件命中即失败：只报告聚合 count，绝不携带动态身份或正文。

    ``where`` 拼接到旧表的 WHERE 条件；任何命中都表示回填不完整或
    子项集合不完整，必须在改名与删除前中止（事务整体回滚）。
    """
    count = _scalar_count(
        connection,
        f"SELECT COUNT(*) FROM komari_chat_reply_commit_outbox WHERE {where}",
    )
    if count:
        msg = f"{label}_count={count}"
        raise RuntimeError(msg)


def _verify_backfill_complete(connection: Connection) -> None:
    """门禁：每条旧行都有父镜像，非终态行子项集合完整且无重复。"""
    # 旧行没有父镜像：0012 之后新增的旧行必须整体中止
    _require_no_conflict(
        connection,
        where=(
            "operation_id NOT IN ("
            "SELECT fulfillment_id FROM komari_chat_reply_fulfillments)"
        ),
        label="missing_backfill",
    )
    # 非终态行的子项数量/去重类型数必须等于适用承诺数
    _require_no_conflict(
        connection,
        where=(
            "status NOT IN ('COMPLETED', 'CANCELLED') "
            "AND ("
            "(SELECT COUNT(*) "
            " FROM komari_chat_reply_fulfillment_commitments AS child "
            " WHERE child.fulfillment_id "
            "     = komari_chat_reply_commit_outbox.operation_id) "
            "<> ((CASE "
            "        WHEN proactive_reservation_id IS NOT NULL THEN 1 "
            "        ELSE 0 "
            "    END) + 2 + (CASE "
            "        WHEN global_interaction_enabled THEN 1 "
            "        ELSE 0 "
            "    END)) "
            "OR (SELECT COUNT(DISTINCT child.commitment_type) "
            " FROM komari_chat_reply_fulfillment_commitments AS child "
            " WHERE child.fulfillment_id "
            "     = komari_chat_reply_commit_outbox.operation_id) "
            "<> (SELECT COUNT(*) "
            " FROM komari_chat_reply_fulfillment_commitments AS child "
            " WHERE child.fulfillment_id "
            "     = komari_chat_reply_commit_outbox.operation_id))"
        ),
        label="commitment_mismatch",
    )


def _finalize_tombstone_children(connection: Connection) -> None:
    """tombstone 收尾：已清证据父行的 proactive 子承诺删除，其余子行保留。

    0012 已把 cutover-cancelled 形态的父行落成无正文墓碑并标记
    ``idempotency_evidence_cleared_at``；其 proactive 预占承诺随预占
    释放一并消失，favorability/history 等其他承诺原样保留。
    """
    connection.execute(
        text(
            """
            DELETE FROM komari_chat_reply_fulfillment_commitments AS child
            USING komari_chat_reply_fulfillments AS parent
            WHERE child.fulfillment_id = parent.fulfillment_id
              AND child.commitment_type = 'proactive_reply_confirmation'
              AND parent.idempotency_evidence_cleared_at IS NOT NULL
            """
        )
    )


def _rename_config_columns(connection: Connection) -> None:
    """六个旧配置列一对一改名；值原样保留。

    PostgreSQL 的 ``RENAME COLUMN`` 是单动作语句，不支持逗号合并，
    必须逐条执行。
    """
    renames = (
        ("reply_commit_worker_interval_seconds", "reply_fulfillment_worker_interval_seconds"),
        ("reply_commit_batch_size", "reply_fulfillment_batch_size"),
        ("reply_commit_lease_seconds", "reply_fulfillment_lease_seconds"),
        ("reply_commit_max_attempts", "reply_fulfillment_max_attempts"),
        ("reply_commit_retry_base_seconds", "reply_fulfillment_retry_base_seconds"),
        ("reply_commit_tombstone_retention_days", "reply_fulfillment_tombstone_retention_days"),
    )
    for old_name, new_name in renames:
        connection.execute(
            text(
                f"ALTER TABLE komari_chat_config"
                f" RENAME COLUMN {old_name} TO {new_name}"
            )
        )


def _add_retry_max_column(connection: Connection) -> None:
    """新增退避上限配置列；存量行统一取默认 3600。"""
    connection.execute(
        text(
            "ALTER TABLE komari_chat_config "
            "ADD COLUMN reply_fulfillment_retry_max_seconds "
            "INTEGER NOT NULL DEFAULT 3600"
        )
    )


def _drop_migration_only_tables(connection: Connection) -> None:
    """migration-only 表物理落下：旧宽表、legacy 名单、gate 与双 ledger。"""
    connection.execute(text("DROP TABLE komari_chat_reply_commit_outbox"))
    connection.execute(text("DROP TABLE IF EXISTS komari_plugin_configs"))
    connection.execute(text(f"DROP TABLE IF EXISTS {_QUARANTINE_LEDGER_TABLE}"))
    connection.execute(text(f"DROP TABLE IF EXISTS {_CLEANUP_LEDGER_TABLE}"))
    connection.execute(text(f"DROP TABLE IF EXISTS {_GATE_TABLE}"))


def upgrade(name: str = "") -> None:
    if name:
        return

    connection = op.get_bind()
    # 准入终检矩阵在任何 DDL/DML 之前执行；fresh 豁免直通。
    _run_admission_terminal_checks(connection)
    _lock_legacy_table(connection)
    _verify_backfill_complete(connection)
    _finalize_tombstone_children(connection)
    _rename_config_columns(connection)
    _add_retry_max_column(connection)
    # 门禁通过后 migration-only 表整体物理删除；新父子表与配置表保留
    _drop_migration_only_tables(connection)


def downgrade(name: str = "") -> None:
    if name:
        return

    # 0013 为不可逆 coordinated contract：旧宽表、legacy 名单与 gate/
    # 双 ledger 已物理删除，改名/新增列与新父子模型、统一准入已全面
    # 接管生产路径，不提供任何回退别名或兼容入口。
    msg = (
        "0013_TSK232_IS_IRREVERSIBLE: 旧宽 outbox 与 legacy JSONB 名单已"
        "物理删除，回复履约与统一准入已整体接管，不允许回退"
    )
    raise RuntimeError(msg)
