"""group admission backfill：barrier 门禁与单事务存量转换（forward-only）

统一准入策略由 operator 显式提交到 ``komari_group_admission_config`` 之后，
本 revision 把 ``upgrade head`` 收敛到唯一 policy 事实，并在同一数据库
事务内完成存量业务数据的 cutover 转换：

门禁（fail-fast，任一命中即中止且 revision 停留 0011 可修复重跑）：

- 与 CLI 写命令共用同一把 ``pg_advisory_xact_lock``（键 =
  ``int.from_bytes(blake2b(b"komari_group_admission:cutover-gate",
  digest_size=8).digest(), signed=True)``），杜绝与 operator 命令并发；
- policy gate：``komari_group_admission_config`` 单行必须存在合法策略
  （错误文本含 closed code ``PHASE_OUT_OF_ORDER``），绝不静默生成缺省
  策略或合并旧 JSONB 名单；
- 相位 gate：gate.phase 必须已到 ``REDIS_EVIDENCE_CAPTURED``；业务存量
  全零的空库允许直通（fresh 自动放行链）。

单事务转换矩阵：

1. 放宽父表送达时间戳 CHECK 为互斥语义（计时事实只归属其状态），
   使无计时事实的 tombstone 形态可合法落库；
2. reply 履约：全部 ``NOT_STARTED`` 行不区分 policy 一律转为
   ``NOT_DELIVERED``（``not_delivered_at=NOW()``）、正文清空、子承诺
   payload 全 NULL、``idempotency_evidence_cleared_at=NOW()`` 作
   cutover-cancelled 凭证（下游 Redis 幂等证据由 finalize-redis 的
   隔离/释放统一处置）；带 proactive 预占身份的行按
   ``(reservation_id, group_id)`` 投影进 cleanup ledger（ON CONFLICT
   DO NOTHING）；``PENDING_CONFIRMATION`` / ``DELIVERED`` / 既有
   ``NOT_DELIVERED`` 行原样保留；
3. proposals：新增 admission 列后按当前 policy 对每行 group_id
   adjudicate——restricted 非终态行转 ``DEFERRED`` 并落 revision/at、
   双 token 清空；approving 且 knowledge 源 content 逐字一致 → 直接
   ``approved``（fact finalization）；源行存在但内容漂移 →
   ``execution_hold_code='KNOWLEDGE_SOURCE_CONFLICT'``；源缺失且
   admitted → 同 hold，源缺失且 restricted → DEFERRED；终态行不动；
4. memory jobs：``stage<>'completed'`` 行哨兵化 ``owner_token=''`` +
   ``lease_until=NOW()``；
5. announcements：processing → ``reconciliation_required`` +
   ``reconciliation_code='CUTOVER_PROGRESS_UNKNOWN'``，owner/lease 清空；
6. gate CAS → ``POSTGRES_BACKFILLED``。

downgrade 明确拒绝回退。本 revision 自包含，不导入 komari_bot。
"""
from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

from alembic import op
from sqlalchemy import text

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy import Connection

revision: str = "0012"
down_revision: str | Sequence[str] | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "komari_group_admission_config"
_GATE_TABLE = "komari_group_admission_gate"
_MARKER = "0012_TSK232_IS_IRREVERSIBLE"

#: 与 cutover CLI 写命令共用的稳定 advisory lock 键（设计契约字面量）。
_LOCK_KEY = int.from_bytes(
    hashlib.blake2b(b"komari_group_admission:cutover-gate", digest_size=8).digest(),
    signed=True,
)

_EVIDENCE_PHASE = "REDIS_EVIDENCE_CAPTURED"
_BACKFILLED_PHASE = "POSTGRES_BACKFILLED"

#: 会话级 fresh 安装标记（由 0001 fresh_marker 在同一次 upgrade 的同一
#: 连接上设置；跨会话续升的新会话不携带，语义见 0001 注释）。
_FRESH_GUC = "komari_tsk232.fresh_install"

_RELAX_TIMESTAMP_CHECK_SQL = """
ALTER TABLE komari_chat_reply_fulfillments
ADD CONSTRAINT ck_reply_fulfillment_delivery_timestamps CHECK (
    (delivery_state = 'NOT_STARTED'
        AND send_started_at IS NULL
        AND delivered_at IS NULL
        AND not_delivered_at IS NULL)
    OR (delivery_state = 'PENDING_CONFIRMATION'
        AND delivered_at IS NULL
        AND not_delivered_at IS NULL)
    OR (delivery_state = 'DELIVERED'
        AND not_delivered_at IS NULL)
    OR (delivery_state = 'NOT_DELIVERED'
        AND delivered_at IS NULL)
)
"""


def _policy_gate(connection: Connection) -> tuple[str, list[str]]:
    """读取并校验单行准入策略；返回 ``(mode, group_ids 文本名单)``。

    策略行缺失或形态非法时以含 closed code ``PHASE_OUT_OF_ORDER`` 的
    错误文本中止升级（operator 需先显式提交策略再重跑）。
    """
    row = connection.execute(
        text(
            f"SELECT policy FROM {_TABLE} WHERE id = 1 "
            "AND policy IS NOT NULL AND jsonb_typeof(policy) = 'object'"
        )
    ).first()
    if row is None:
        msg = (
            f"{_MARKER}: 群聊准入策略未显式提交（PHASE_OUT_OF_ORDER），"
            "无法继续 head 升级；绝不静默生成缺省策略或合并旧名单"
        )
        raise RuntimeError(msg)
    policy = row[0]
    mode = policy.get("mode") if isinstance(policy, dict) else None
    group_ids = policy.get("group_ids") if isinstance(policy, dict) else None
    if mode not in ("blacklist", "whitelist") or not isinstance(group_ids, list):
        msg = f"{_MARKER}: 准入策略形态非法（PHASE_OUT_OF_ORDER），中止升级"
        raise RuntimeError(msg)
    return str(mode), [str(g) for g in group_ids]


def _business_residual_count(connection: Connection) -> int:
    """聚合四族业务存量的未收敛行数（任一非零即要求 evidence 相位）。"""
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


def _phase_gate(connection: Connection) -> str:
    """相位前置，返回执行路径裁定（fail-fast 或三选一）：

    - ``evidence``：operator 路径，gate 已在 evidence 相位；
    - ``clean_slate``：业务存量全零且非 fresh 安装会话——空库直通，
      自动推进到 POSTGRES_BACKFILLED；
    - ``fresh_install``：全新空库的同一次 ``upgrade head`` 运行（0001
      已设会话 fresh 标记）——保持 gate 停留 EXPANDED，cutover 相位机
      留给安装后的 operator 流程；跨会话续升的新会话不带该标记。
    """
    gate_row = connection.execute(
        text(f"SELECT phase FROM {_GATE_TABLE} WHERE id = 1")
    ).first()
    if gate_row is None:
        msg = (
            f"{_MARKER}: cutover gate 行缺失（GATE_MISSING），中止升级"
        )
        raise RuntimeError(msg)
    phase = str(gate_row[0])
    if phase == _EVIDENCE_PHASE:
        return "evidence"
    if _business_residual_count(connection) > 0:
        msg = (
            f"{_MARKER}: gate 相位未到 {_EVIDENCE_PHASE} 而业务存量非零"
            f"（PHASE_OUT_OF_ORDER，当前 phase={phase}），中止升级"
        )
        raise RuntimeError(msg)
    fresh_install = connection.execute(
        text(f"SELECT current_setting('{_FRESH_GUC}', true)")
    ).scalar_one_or_none()
    return "clean_slate" if fresh_install != "1" else "fresh_install"


def _add_proposal_admission_columns(connection: Connection) -> None:
    """proposals 新增统一准入四列；存量行默认 ACTIVE。"""
    connection.execute(
        text(
            "ALTER TABLE komari_custom_proposals "
            "ADD COLUMN admission_state TEXT NOT NULL DEFAULT 'ACTIVE'"
        )
    )
    connection.execute(
        text(
            "ALTER TABLE komari_custom_proposals "
            "ADD COLUMN execution_hold_code TEXT"
        )
    )
    connection.execute(
        text(
            "ALTER TABLE komari_custom_proposals "
            "ADD COLUMN admission_deferred_revision INTEGER"
        )
    )
    connection.execute(
        text(
            "ALTER TABLE komari_custom_proposals "
            "ADD COLUMN admission_deferred_at TIMESTAMPTZ"
        )
    )


def _relax_delivery_timestamp_check(connection: Connection) -> None:
    """放宽父表送达时间戳 CHECK：允许无计时事实的履约行。

    cutover 后履约行的计时事实（send_started_at / delivered_at /
    not_delivered_at）允许缺席；仅保留"事实只归属其状态"的互斥语义：
    ``delivered_at`` 只能出现在 DELIVERED，``not_delivered_at`` 只能
    出现在 NOT_DELIVERED，NOT_STARTED 行保持三项全空。tombstone 转换
    依赖本语义，故必须在同一迁移内先于数据转换执行。
    """
    connection.execute(
        text("ALTER TABLE komari_chat_reply_fulfillments DROP CONSTRAINT"
             " ck_reply_fulfillment_delivery_timestamps")
    )
    connection.execute(text(_RELAX_TIMESTAMP_CHECK_SQL))


def _convert_reply_fulfillments(connection: Connection) -> None:
    """reply 存量最小化转换与 cleanup ledger 投影（顺序不可反转）。

    先投影 ledger（依赖 NOT_STARTED 判据），再清子 payload，最后改写
    父行——保证既有 NOT_DELIVERED 行的子 payload 不被误清。
    """
    connection.execute(
        text(
            """
            INSERT INTO komari_reply_reservation_cleanup_ledger
                (reservation_id, group_id)
            SELECT DISTINCT
                child.payload->>'reservation_id',
                parent.group_id::bigint
            FROM komari_chat_reply_fulfillments AS parent
            JOIN komari_chat_reply_fulfillment_commitments AS child
              ON child.fulfillment_id = parent.fulfillment_id
             AND child.commitment_type = 'proactive_reply_confirmation'
             AND child.payload ? 'reservation_id'
            WHERE parent.delivery_state = 'NOT_STARTED'
            ON CONFLICT (reservation_id) DO NOTHING
            """
        )
    )
    connection.execute(
        text(
            """
            UPDATE komari_chat_reply_fulfillment_commitments AS child
            SET payload = NULL
            WHERE EXISTS (
                SELECT 1 FROM komari_chat_reply_fulfillments AS parent
                WHERE parent.fulfillment_id = child.fulfillment_id
                  AND parent.delivery_state = 'NOT_STARTED'
            )
            """
        )
    )
    connection.execute(
        text(
            """
            UPDATE komari_chat_reply_fulfillments
            SET delivery_state = 'NOT_DELIVERED',
                not_delivered_at = NOW(),
                reply_content = NULL,
                idempotency_evidence_cleared_at = NOW()
            WHERE delivery_state = 'NOT_STARTED'
            """
        )
    )


def _restricted_predicate(mode: str, group_ids: list[str]) -> str:
    """按 policy mode 构造"group_id 受限"SQL 判据。

    ``blacklist``：名单内的群受限；``whitelist``：名单外的群受限。
    """
    members = ", ".join(
        "'" + g.replace("'", "''") + "'" for g in group_ids
    ) or "NULL"
    listed = f"parent.group_id::text IN ({members})"
    return listed if mode == "blacklist" else f"NOT ({listed})"


def _convert_proposals(
    connection: Connection,
    mode: str,
    group_ids: list[str],
) -> None:
    """proposal 按 policy adjudicate：finalization/hold/DEFERRED 三向收敛。"""
    restricted = _restricted_predicate(mode, group_ids)
    # 1) approving + 源行存在且 content 逐字一致 → fact finalization
    connection.execute(
        text(
            """
            UPDATE komari_custom_proposals AS parent
            SET status = 'approved', approved_at = NOW()
            WHERE parent.status = 'approving'
              AND parent.knowledge_id IS NOT NULL
              AND EXISTS (
                  SELECT 1 FROM komari_knowledge AS source
                  WHERE source.id = parent.knowledge_id
                    AND source.content = parent.content
              )
            """
        )
    )
    # 2) approving + 源行存在但内容漂移 → closed hold
    connection.execute(
        text(
            """
            UPDATE komari_custom_proposals AS parent
            SET execution_hold_code = 'KNOWLEDGE_SOURCE_CONFLICT'
            WHERE parent.status = 'approving'
              AND parent.knowledge_id IS NOT NULL
              AND EXISTS (
                  SELECT 1 FROM komari_knowledge AS source
                  WHERE source.id = parent.knowledge_id
                    AND source.content <> parent.content
              )
            """
        )
    )
    # 3) approving + 源缺失 + admitted → closed hold（受限群走 DEFERRED）
    connection.execute(
        text(
            f"""
            UPDATE komari_custom_proposals AS parent
            SET execution_hold_code = 'KNOWLEDGE_SOURCE_CONFLICT'
            WHERE parent.status = 'approving'
              AND parent.knowledge_id IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1 FROM komari_knowledge AS source
                  WHERE source.id = parent.knowledge_id
              )
              AND NOT {restricted}
            """
        )
    )
    # 4) restricted 非终态 → DEFERRED 休眠（revision/at 凭证 + token 清空）
    connection.execute(
        text(
            f"""
            UPDATE komari_custom_proposals AS parent
            SET admission_state = 'DEFERRED',
                admission_deferred_revision = (
                    SELECT revision FROM {_TABLE} WHERE id = 1
                ),
                admission_deferred_at = NOW(),
                publication_token = NULL,
                approval_token = NULL
            WHERE parent.status IN ('publishing', 'voting', 'approving')
              AND {restricted}
            """
        )
    )


def _sentinelize_memory_jobs(connection: Connection) -> None:
    """memory 未完成 job 哨兵化：归还租约，交由运行时重新认领。"""
    connection.execute(
        text(
            """
            UPDATE komari_memory_jobs
            SET owner_token = '', lease_until = NOW()
            WHERE stage <> 'completed'
            """
        )
    )


def _quarantine_processing_announcements(connection: Connection) -> None:
    """公告 processing 行转 reconciliation_required 并清 owner/lease。"""
    connection.execute(
        text(
            "UPDATE komari_announcement_dispatches "
            "SET status = 'reconciliation_required', "
            "    reconciliation_code = 'CUTOVER_PROGRESS_UNKNOWN', "
            "    owner_token = NULL, lease_expires_at = NULL "
            "WHERE status = 'processing'"
        )
    )


def _advance_gate_phase(connection: Connection, *, from_evidence: bool) -> None:
    """gate CAS 推进到 POSTGRES_BACKFILLED（只前进，不回退）。"""
    allowed = (
        f"('{_EVIDENCE_PHASE}')" if from_evidence else "('EXPANDED', 'POLICY_PREPARED')"
    )
    result = connection.execute(
        text(
            f"UPDATE {_GATE_TABLE} SET phase = '{_BACKFILLED_PHASE}', "
            f"phase_updated_at = NOW() WHERE id = 1 AND phase IN {allowed}"
        )
    )
    if result.rowcount != 1:
        msg = f"{_MARKER}: gate 相位 CAS 未命中（PHASE_OUT_OF_ORDER），中止升级"
        raise RuntimeError(msg)


def upgrade(name: str = "") -> None:
    if name:
        return
    connection = op.get_bind()
    connection.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _LOCK_KEY})
    path = _phase_gate(connection)
    policy: tuple[str, list[str]] | None = None
    if path != "fresh_install":
        policy = _policy_gate(connection)
    # schema 演进对全部路径一致执行（fresh 空库同样收敛到目标形态）。
    _add_proposal_admission_columns(connection)
    connection.execute(
        text(
            "ALTER TABLE komari_announcement_dispatches "
            "ADD COLUMN reconciliation_code TEXT"
        )
    )
    _relax_delivery_timestamp_check(connection)
    if path == "fresh_install":
        # 全新空库的同一次 upgrade 运行：业务面本就为空，gate 相位保持
        # EXPANDED 留给安装后的 operator cutover 流程，不做数据转换。
        return
    _convert_reply_fulfillments(connection)
    assert policy is not None  # 非 fresh 路径必已装载策略
    _convert_proposals(connection, *policy)
    _sentinelize_memory_jobs(connection)
    _quarantine_processing_announcements(connection)
    _advance_gate_phase(connection, from_evidence=(path == "evidence"))


def downgrade(name: str = "") -> None:
    if name:
        return
    msg = f"{_MARKER}: 群聊准入 backfill 为 forward-only barrier，不允许回退"
    raise RuntimeError(msg)
